#!/usr/bin/env python3
"""Export JLCPCB PCBA files (gerber/drill zip, BOM, CPL) from any KiCad project.

Usage:
    kicad_jlc_export.py [PROJECT] [-o OUTPUT_DIR] [--config FILE] [--no-gerbers]

PROJECT is a project folder or .kicad_pro file (default: current folder).
Files go to PROJECT/jlc_export/ unless -o is given. Exits 1 if a check fails.
Tested with KiCad 10. Needs only the Python standard library.

Mid X/Mid Y are the centre of each part's soldered pads, not the footprint origin.
JLCPCB's 0 degrees differs per part, so rotations can't be worked out from KiCad
alone: fix them in JLCPCB's preview once, then record them in jlc_export.json.

Optional per-project settings live in PROJECT/jlc_export.json. Every key is
optional. This is where fixes found in JLCPCB's preview are kept:

    {
      "lcsc_fields": ["LCSC PART #"],        // default: detect from schematic
      "exclude": ["J5"],                     // not assembled by JLCPCB
      "rotation_by_package": {"^SOT-23": 180},  // regex on the Package column
      "rotation_by_ref": {"U2": 270},        // replaces any package rule
      "offset_by_ref": {"J5": [0, 3.81]}     // mm, added to CPL Mid X, Mid Y
    }

(JSON doesn't allow comments; they're only here for explanation.)
Rotations are degrees added to KiCad's rotation.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

GERBER_LAYERS = ["F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]
SEXP_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
MAX_REFS_PER_BOM_ROW = 200
MOVED_NOTE_MM = 0.1


@dataclass
class Project:
    name: str
    folder: Path
    pcb: Path
    sch: Path


@dataclass
class Config:
    lcsc_fields: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    rotation_by_package: dict = field(default_factory=dict)
    rotation_by_ref: dict = field(default_factory=dict)
    offset_by_ref: dict = field(default_factory=dict)


@dataclass
class Pad:
    x: float
    y: float
    width: float
    height: float


@dataclass
class Footprint:
    ref: str
    lib_id: str
    value: str
    fields: dict
    rotation: float
    bottom: bool
    pads: list  # soldered pads in the footprint's own stored frame
    attrs: list = field(default_factory=list)


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.notes = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def note(self, msg):
        self.notes.append(msg)

    def show(self):
        for label, items in (("NOTE", self.notes), ("WARNING", self.warnings), ("ERROR", self.errors)):
            for item in items:
                print(f"{label}: {item}")


# ---- project and tool discovery ----

def find_project(path_arg: str) -> Project:
    path = Path(path_arg).expanduser().resolve()
    if path.is_dir():
        found = sorted(path.glob("*.kicad_pro"))
        if len(found) != 1:
            sys.exit(f"Expected one .kicad_pro in {path}, found {len(found)}. Pass the file directly.")
        path = found[0]
    if path.suffix != ".kicad_pro" or not path.exists():
        sys.exit(f"Not a KiCad project: {path}")
    project = Project(path.stem, path.parent, path.with_suffix(".kicad_pcb"), path.with_suffix(".kicad_sch"))
    for needed in (project.pcb, project.sch):
        if not needed.exists():
            sys.exit(f"Missing {needed}")
    return project


def find_kicad_cli(explicit: str | None) -> str:
    candidates = [explicit, os.environ.get("KICAD_CLI"), shutil.which("kicad-cli"),
                  "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"]
    candidates += sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\kicad-cli.exe"), reverse=True)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit("kicad-cli not found. Pass --kicad-cli PATH or set KICAD_CLI.")


def load_config(project: Project, config_arg: str | None) -> Config:
    path = Path(config_arg).expanduser() if config_arg else project.folder / "jlc_export.json"
    if not path.exists():
        if config_arg:
            sys.exit(f"Config not found: {path}")
        return Config()
    try:
        config = Config(**json.loads(path.read_text()))
        for pattern in config.rotation_by_package:
            re.compile(pattern)
        for ref, offset in config.offset_by_ref.items():
            if len(offset) != 2:
                raise ValueError(f"offset for {ref} needs [x, y]")
            float(offset[0]), float(offset[1])
        for degrees in [*config.rotation_by_package.values(), *config.rotation_by_ref.values()]:
            float(degrees)
    except (json.JSONDecodeError, TypeError, ValueError, re.error) as exc:
        sys.exit(f"Bad config {path}: {exc}")
    return config


def run_cli(cli: str, *args) -> None:
    result = subprocess.run([cli, *map(str, args)], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"kicad-cli {' '.join(map(str, args[:3]))} failed:\n{result.stdout}\n{result.stderr}")


def cli_supports(cli: str, option: str, *command) -> bool:
    result = subprocess.run([cli, *command, "--help"], capture_output=True, text=True)
    return option in result.stdout


# ---- board parsing ----

def parse_sexp(text: str) -> list:
    stack = [[]]
    for token in SEXP_TOKEN.findall(text):
        if token == "(":
            stack.append([])
        elif token == ")":
            node = stack.pop()
            stack[-1].append(node)
        elif token.startswith('"'):
            stack[-1].append(re.sub(r"\\(.)", r"\1", token[1:-1]))
        else:
            stack[-1].append(token)
    return stack[0][0]


def children(node: list, name: str) -> list:
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def soldered_pads(fp_node: list, rotation: float) -> list:
    """Return the copper pads, leaving out NPTH holes and paste-only pads."""
    pads = []
    for pad in children(fp_node, "pad"):
        layers = children(pad, "layers")
        on_copper = bool(layers) and any(name.endswith(".Cu") for name in layers[0][1:])
        if pad[2] == "np_thru_hole" or not on_copper:
            continue
        at = children(pad, "at")[0]
        size = children(pad, "size")[0]
        angle = math.radians((float(at[3]) if len(at) > 3 else 0.0) - rotation)
        cos, sin = abs(math.cos(angle)), abs(math.sin(angle))
        width, height = float(size[1]), float(size[2])
        pads.append(Pad(float(at[1]), float(at[2]), width * cos + height * sin, width * sin + height * cos))
    return pads


def read_board(pcb: Path) -> tuple:
    """Return ({ref: Footprint}, [copper layer names])."""
    root = parse_sexp(pcb.read_text(encoding="utf-8"))
    footprints = {}
    for node in children(root, "footprint"):
        props = {p[1]: p[2] for p in children(node, "property") if len(p) > 2}
        ref = props.get("Reference", "")
        if ref:
            at = children(node, "at")[0]
            rotation = float(at[3]) if len(at) > 3 else 0.0
            attrs = children(node, "attr")
            footprints[ref] = Footprint(ref, node[1], props.get("Value", ""), props, rotation,
                                        children(node, "layer")[0][1] == "B.Cu", soldered_pads(node, rotation),
                                        attrs[0][1:] if attrs else [])
    if not footprints:
        sys.exit(f"No footprints on {pcb}. Run Update PCB from Schematic and save first.")
    layers = children(root, "layers")[0][1:]
    copper = [entry[1] for entry in layers if entry[1].endswith(".Cu")]
    return footprints, copper


def pad_centre(footprint: Footprint) -> tuple:
    """Return the centre of the soldered pads' bounding box in the footprint's frame."""
    if not footprint.pads:
        return (0.0, 0.0)
    left = min(p.x - p.width / 2 for p in footprint.pads)
    right = max(p.x + p.width / 2 for p in footprint.pads)
    top = min(p.y - p.height / 2 for p in footprint.pads)
    bottom = max(p.y + p.height / 2 for p in footprint.pads)
    return ((left + right) / 2, (top + bottom) / 2)


def cpl_offset(footprint: Footprint, x: float, y: float) -> tuple:
    """Turn a point in the footprint's frame into an offset from its origin, in CPL axes (Y up)."""
    angle = math.radians(footprint.rotation)
    board_x = x * math.cos(angle) + y * math.sin(angle)
    board_y = -x * math.sin(angle) + y * math.cos(angle)
    return (board_x, -board_y)


# ---- LCSC field handling ----

def is_part_field(name: str) -> bool:
    lower = name.lower()
    if "offset" in lower or "rotation" in lower:
        return False
    return "lcsc" in lower or ("jlc" in lower and "part" in lower)


def detect_lcsc_fields(project: Project) -> list:
    names = set()
    for sch in project.folder.glob("*.kicad_sch"):
        names.update(n for n in re.findall(r'\(property "([^"]+)"', sch.read_text(encoding="utf-8")) if is_part_field(n))
    return sorted(names)


def resolve_lcsc(ref: str, values: dict, fields: list, report: Report, where: str) -> str:
    found = {values.get(f, "").strip() for f in fields} - {""}
    if len(found) > 1:
        report.error(f"{ref}: conflicting LCSC numbers in {where}: {', '.join(sorted(found))}")
    return sorted(found)[0] if found else ""


# ---- exports ----

def natural_key(ref: str) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", ref)]


def read_symbols(cli: str, project: Project, fields: list, workdir: Path) -> dict:
    """Return {ref: row} for every symbol that goes on the BOM, ungrouped."""
    columns = ["Reference", "Value", "Footprint"] + fields
    out = workdir / "symbols.csv"
    run_cli(cli, "sch", "export", "bom", project.sch, "--fields", ",".join(columns),
            "--labels", ",".join(columns), "--ref-range-delimiter", "", "--exclude-dnp", "-o", out)
    symbols = {}
    with out.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for ref in row["Reference"].split(","):
                symbols[ref.strip()] = row
    return symbols


def write_bom(path: Path, symbols: dict, fields: list, report: Report) -> dict:
    """Write the grouped JLCPCB BOM and return {ref: lcsc}."""
    groups, parts = {}, {}
    for ref, row in symbols.items():
        lcsc = resolve_lcsc(ref, row, fields, report, "schematic")
        parts[ref] = lcsc
        if not lcsc:
            report.warn(f"{ref} ({row['Value']}): no LCSC number, JLCPCB won't place it")
        elif not re.fullmatch(r"C\d+", lcsc):
            report.error(f"{ref}: '{lcsc}' is not a valid LCSC number")
        footprint = row["Footprint"].split(":")[-1]
        groups.setdefault((row["Value"], footprint, lcsc), []).append(ref)
    rows = sorted(groups.items(), key=lambda item: natural_key(min(item[1], key=natural_key)))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for (value, footprint, lcsc), refs in rows:
            refs = sorted(refs, key=natural_key)
            for start in range(0, len(refs), MAX_REFS_PER_BOM_ROW):
                writer.writerow([value, ",".join(refs[start:start + MAX_REFS_PER_BOM_ROW]), footprint, lcsc])
    return parts


def rotation_fix(ref: str, package: str, config: Config) -> float:
    if ref in config.rotation_by_ref:
        return float(config.rotation_by_ref[ref])
    for pattern, degrees in config.rotation_by_package.items():
        if re.search(pattern, package):
            return float(degrees)
    return 0.0


def write_cpl(path: Path, cli: str, project: Project, parts: dict, board: dict, config: Config,
              workdir: Path, report: Report) -> tuple:
    """Write the JLCPCB CPL for BOM parts and return (placed refs, refs left out, {ref: (x, y)})."""
    raw = workdir / "pos.csv"
    run_cli(cli, "pcb", "export", "pos", project.pcb, "--format", "csv", "--units", "mm",
            "--side", "both", "--exclude-dnp", "-o", raw)
    with raw.open(newline="", encoding="utf-8") as handle:
        rows = sorted(csv.DictReader(handle), key=lambda row: natural_key(row["Ref"]))
    placed, left_out, turned, moved, positions = set(), [], {}, [], {}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for row in rows:
            ref = row["Ref"]
            if ref not in parts:
                left_out.append(ref)
                continue
            if ref in placed:
                report.error(f"{ref}: used by more than one footprint on the board")
                continue
            footprint = board[ref]
            dx, dy = cpl_offset(footprint, *pad_centre(footprint))
            if math.hypot(dx, dy) > MOVED_NOTE_MM:
                moved.append(f"{ref} {math.hypot(dx, dy):.2f} mm")
            extra_x, extra_y = map(float, config.offset_by_ref.get(ref, (0, 0)))
            x, y = float(row["PosX"]) + dx + extra_x, float(row["PosY"]) + dy + extra_y

            layer = "Top" if row["Side"].lower() == "top" else "Bottom"
            # JLCPCB counts a bottom part's rotation looking at the bottom of the board.
            rotation = float(row["Rot"]) if layer == "Top" else 180.0 - float(row["Rot"])
            turn = rotation_fix(ref, row["Package"], config)
            if turn % 360:
                turned.setdefault(int(turn % 360), []).append(ref)
            writer.writerow([ref, f"{x:.4f}", f"{y:.4f}", layer, f"{(rotation + turn) % 360:g}"])
            placed.add(ref)
            positions[ref] = (x, y)
    for turn, refs in sorted(turned.items()):
        report.note(f"Rotation fix +{turn}: {', '.join(refs)}")
    if moved:
        report.note(f"Placed at pad centre, away from the footprint origin: {', '.join(moved)}")
    return placed, left_out, positions


def write_gerber_zip(path: Path, cli: str, project: Project, copper: list, workdir: Path) -> Path:
    """Plot gerbers and drills, zip them, and return the folder they were plotted to."""
    plot_dir = workdir / "plot"
    plot_dir.mkdir()
    zone_flag = ["--check-zones"] if cli_supports(cli, "--check-zones", "pcb", "export", "gerbers") else []
    run_cli(cli, "pcb", "export", "gerbers", project.pcb, "-o", f"{plot_dir}{os.sep}",
            "--layers", ",".join(copper + GERBER_LAYERS), "--no-x2", "--no-netlist",
            "--subtract-soldermask", *zone_flag)
    run_cli(cli, "pcb", "export", "drill", project.pcb, "-o", f"{plot_dir}{os.sep}", "--format", "excellon",
            "--drill-origin", "absolute", "--excellon-units", "mm", "--excellon-zeros-format", "decimal",
            "--excellon-oval-format", "alternate", "--excellon-separate-th")
    files = sorted(p for p in plot_dir.iterdir() if p.suffix != ".gbrjob")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, file.name)
    return plot_dir


# ---- checks ----

def check_locks(project: Project, report: Report) -> None:
    if list(project.folder.glob(f"~{glob.escape(project.name)}.kicad_*.lck")):
        report.warn("Project looks open in KiCad. This script reads saved files, so save first.")


def check_sync(symbols: dict, parts: dict, board: dict, fields: list, report: Report) -> None:
    for ref, row in symbols.items():
        footprint = board.get(ref)
        if footprint is None:
            continue
        if footprint.value != row["Value"]:
            report.error(f"{ref}: value is '{row['Value']}' in schematic, '{footprint.value}' on board")
        if row["Footprint"] and footprint.lib_id != row["Footprint"]:
            report.error(f"{ref}: footprint is '{row['Footprint']}' in schematic, '{footprint.lib_id}' on board")
        board_lcsc = resolve_lcsc(ref, footprint.fields, fields, report, "board")
        if board_lcsc != parts[ref]:
            report.error(f"{ref}: LCSC is '{parts[ref]}' in schematic, '{board_lcsc}' on board. Run Update PCB from Schematic")


def check_refs_match(parts: dict, placed: set, report: Report) -> None:
    missing = sorted(set(parts) - placed, key=natural_key)
    if missing:
        report.error(f"On the BOM but missing from the board's position data: {', '.join(missing)}")
    if not any(parts.values()):
        report.error("No part has an LCSC number, so JLCPCB would place nothing")


def check_left_out(left_out: list, board: dict, config: Config, report: Report) -> None:
    """Footprints left out of the CPL must have a reason, or a sheet or symbol has gone missing."""
    allowed, unexplained = [], []
    for ref in left_out:
        attrs = board[ref].attrs if ref in board else []
        known = ref in config.exclude or "**" in ref or "exclude_from_bom" in attrs or "board_only" in attrs
        (allowed if known else unexplained).append(ref)
    if allowed:
        report.note(f"Left out of the CPL (not on the BOM): {', '.join(allowed)}")
    if unexplained:
        report.error(f"On the board but not on the BOM: {', '.join(unexplained)}. Check for a missing sheet or a DNP "
                     "mismatch, or list them under exclude in jlc_export.json")


def check_config_refs(config: Config, board: dict, report: Report) -> None:
    named = set(config.exclude) | set(config.rotation_by_ref) | set(config.offset_by_ref)
    unknown = sorted(named - set(board), key=natural_key)
    if unknown:
        report.warn(f"jlc_export.json names parts that aren't on the board: {', '.join(unknown)}")


def outline_segments(edge_gerber: str) -> list:
    """Return the ((x1, y1), (x2, y2)) strokes of a plotted Edge.Cuts layer, in mm."""
    segments, current = [], None
    for x, y, op in re.findall(r"X(-?\d+)Y(-?\d+)(?:I-?\d+J-?\d+)?D0([12])\*", edge_gerber):
        point = (round(int(x) / 1e6, 3), round(int(y) / 1e6, 3))
        if op == "1" and current is not None:
            segments.append((current, point))
        current = point
    return segments


def check_plots(plot_dir: Path, copper: list, positions: dict, report: Report) -> None:
    """Check the zip holds a complete board: copper layers, drills, a closed outline, parts inside it."""
    gerbers = [p for p in plot_dir.iterdir() if p.suffix not in (".drl", ".gbrjob")]
    if len(gerbers) != len(copper) + len(GERBER_LAYERS):
        report.error(f"Expected {len(copper) + len(GERBER_LAYERS)} gerbers, plotted {len(gerbers)}")
    if not list(plot_dir.glob("*-PTH.drl")):
        report.error("No plated drill file was written")
    edges = list(plot_dir.glob("*.gm1"))
    text = edges[0].read_text(encoding="utf-8") if edges else ""
    segments = outline_segments(text)
    if not segments:
        report.error("The board has no outline on Edge.Cuts")
        return
    ends = {}
    for start, end in segments:
        for point in (start, end):
            ends[point] = ends.get(point, 0) + 1
    if any(count % 2 for count in ends.values()) and "G36" not in text:
        report.error("The board outline on Edge.Cuts isn't closed")
    xs, ys = [p[0] for p in ends], [p[1] for p in ends]
    outside = [ref for ref, (x, y) in positions.items() if not (min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys))]
    if outside:
        report.warn(f"Placed outside the board outline: {', '.join(sorted(outside, key=natural_key))}")


# ---- main ----

def main() -> int:
    parser = argparse.ArgumentParser(description="Export JLCPCB PCBA files from a KiCad project.")
    parser.add_argument("project", nargs="?", default=".", help="project folder or .kicad_pro (default: current folder)")
    parser.add_argument("-o", "--output", help="output folder (default: PROJECT/jlc_export)")
    parser.add_argument("--config", help="settings file (default: PROJECT/jlc_export.json)")
    parser.add_argument("--kicad-cli", help="path to kicad-cli")
    parser.add_argument("--no-gerbers", action="store_true", help="only write the BOM and CPL")
    args = parser.parse_args()

    project = find_project(args.project)
    cli = find_kicad_cli(args.kicad_cli)
    config = load_config(project, args.config)
    output = Path(args.output).expanduser() if args.output else project.folder / "jlc_export"
    output.mkdir(parents=True, exist_ok=True)
    report = Report()

    check_locks(project, report)
    fields = config.lcsc_fields or detect_lcsc_fields(project)
    if not fields:
        report.warn("No LCSC field found in the schematic. Set lcsc_fields in jlc_export.json if it has another name.")
    report.note(f"LCSC fields: {', '.join(fields) or 'none'}")

    board, copper = read_board(project.pcb)
    bom_path = output / f"{project.name}_BOM.csv"
    cpl_path = output / f"{project.name}_CPL.csv"
    zip_path = output / f"{project.name}_gerbers.zip"

    with tempfile.TemporaryDirectory() as temp:
        workdir = Path(temp)
        symbols = read_symbols(cli, project, fields, workdir)
        for ref in config.exclude:
            symbols.pop(ref, None)
        parts = write_bom(bom_path, symbols, fields, report)
        placed, left_out, positions = write_cpl(cpl_path, cli, project, parts, board, config, workdir, report)
        check_sync(symbols, parts, board, fields, report)
        check_refs_match(parts, placed, report)
        check_left_out(left_out, board, config, report)
        check_config_refs(config, board, report)
        print(f"BOM: {bom_path} ({len(parts)} parts)")
        print(f"CPL: {cpl_path} ({len(placed)} placements)")
        if not args.no_gerbers:
            plot_dir = write_gerber_zip(zip_path, cli, project, copper, workdir)
            check_plots(plot_dir, copper, positions, report)
            print(f"Gerbers: {zip_path} ({sum(1 for p in plot_dir.iterdir() if p.suffix != '.gbrjob')} files)")
        elif zip_path.exists():
            report.warn(f"{zip_path.name} was not regenerated and may not match this BOM and CPL")

    report.show()
    if report.errors:
        print(f"\nFAILED: {len(report.errors)} error(s). Fix these before uploading.")
        return 1
    print("\nChecks passed. Upload the files, then check parts' rotation and position in JLCPCB's preview.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
