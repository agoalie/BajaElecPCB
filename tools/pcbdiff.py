#!/usr/bin/env python3
"""Build a visual diff of a KiCad board between two versions.

Usage:
    pcbdiff.py BOARD                 HEAD vs. your working folder
    pcbdiff.py BOARD REV             REV vs. your working folder
    pcbdiff.py BOARD REV1 REV2       REV1 vs. REV2
    pcbdiff.py BOARD REV --open      also open the result in your browser

BOARD is a folder holding a .kicad_pro. REV is anything git understands, such as
main, HEAD~3, or a commit hash. Results go to pcbdiff-out/BOARD/ (change with -o):
index.html compares every schematic sheet and PCB layer, and summary.md lists the
component, net, and placement changes.

Tested with KiCad 10. Needs only the Python standard library and kicad-cli. Set
KICAD_CLI if kicad-cli isn't on PATH or in KiCad's default install location.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import webbrowser

CLI_LOCATIONS = [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
    r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
]
EXTRA_LAYERS = ["F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "F.Paste", "B.Paste", "F.Fab", "B.Fab"]
CROP_MARGIN_MM = 5
MOVE_TOLERANCE_MM = 0.001


def find_cli():
    found = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if found:
        return found
    for path in CLI_LOCATIONS:
        if os.path.exists(path):
            return path
    sys.exit("kicad-cli not found. Install KiCad or set KICAD_CLI to its path.")


def run(*cmd):
    done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if done.returncode:
        sys.exit(f"Command failed: {' '.join(cmd)}\n{done.stderr.decode(errors='replace')}")
    return done.stdout


def extract_rev(rev, board, dest):
    """Unpack BOARD as of REV into dest. Returns the folder, or None if it didn't exist."""
    git_dir = os.path.dirname(os.path.abspath(board))
    name = os.path.basename(os.path.abspath(board))
    done = subprocess.run(["git", "-C", git_dir, "archive", "--format=tar", rev, "--", name],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if done.returncode:
        known = subprocess.run(["git", "-C", git_dir, "rev-parse", "--verify", "--quiet", rev + "^{commit}"],
                               stdout=subprocess.DEVNULL)
        if known.returncode:
            sys.exit(f"Unknown git revision: {rev}")
        return None
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        tar.extractall(dest)
    return os.path.join(dest, name)


def project_files(folder):
    """Returns (stem, pcb path, schematic path) for the project in folder, or None."""
    if not folder:
        return None
    projects = glob.glob(os.path.join(glob.escape(folder), "*.kicad_pro"))
    if not projects:
        return None
    if len(projects) > 1:
        sys.exit(f"{folder} holds more than one .kicad_pro; each board needs its own folder.")
    stem = os.path.splitext(projects[0])[0]
    return os.path.basename(stem), stem + ".kicad_pcb", stem + ".kicad_sch"


def read_layers(pcb_path):
    """Returns [(canonical name, display name)] for the copper and useful non-copper layers."""
    with open(pcb_path, encoding="utf-8") as f:
        text = f.read()
    block = re.search(r"\(layers\s*\n(.*?)\n\s*\)", text, re.S).group(1)
    table = re.findall(r'\(\d+ "([^"]+)" (\w+)(?: "([^"]+)")?\)', block)
    copper = [(name, label or name) for name, kind, label in table if kind != "user"]
    others = {name: label or name for name, kind, label in table if kind == "user"}
    return copper + [(name, others[name]) for name in EXTRA_LAYERS if name in others]


def render_pcb(cli, pcb_path, stem, out):
    layers = read_layers(pcb_path)
    run(cli, "pcb", "export", "svg", "--mode-multi", "--black-and-white", "--exclude-drawing-sheet",
        "--page-size-mode", "1", "-l", ",".join(name for name, _ in layers), "--cl", "Edge.Cuts",
        "-o", out, pcb_path)
    run(cli, "pcb", "export", "svg", "--mode-single", "--black-and-white", "--exclude-drawing-sheet",
        "--page-size-mode", "1", "-l", "Edge.Cuts", "-o", os.path.join(out, "_outline.svg"), pcb_path)
    rendered = {}
    for name, label in layers:
        for candidate in (label, name):
            path = os.path.join(out, f"{stem}-{candidate.replace('.', '_')}.svg")
            if os.path.exists(path):
                rendered[label] = path
                break
        else:
            sys.exit(f"kicad-cli did not write an SVG for layer {name}.")
    return rendered


def render_schematic(cli, sch_path, stem, out):
    run(cli, "sch", "export", "svg", "--black-and-white", "--exclude-drawing-sheet",
        "--no-background-color", "-o", out, sch_path)
    rendered = {}
    for path in sorted(glob.glob(os.path.join(glob.escape(out), "*.svg"))):
        sheet = os.path.basename(path)[:-4]
        label = "Root sheet" if sheet == stem else sheet[len(stem) + 1:]
        rendered[label] = path
    return rendered


def drawing_bbox(svg_path):
    """Bounding box of everything drawn in a KiCad SVG, in its own coordinates."""
    with open(svg_path, encoding="utf-8") as f:
        text = f.read()
    xs, ys = [], []
    for path in re.findall(r'\bd="([^"]+)"', text):
        for command, args in re.findall(r"([MLAZ])([^MLAZ]*)", path):
            numbers = [float(n) for n in re.findall(r"-?[\d.]+", args)]
            if command == "A":
                numbers = numbers[-2:]
            xs += numbers[0::2]
            ys += numbers[1::2]
    for x, y, r in re.findall(r'<circle cx="([-\d.]+)" cy="([-\d.]+)" r="([\d.]+)"', text):
        xs += [float(x) - float(r), float(x) + float(r)]
        ys += [float(y) - float(r), float(y) + float(r)]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def svg_size(text):
    box = re.search(r'viewBox="\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)', text)
    return float(box.group(1)), float(box.group(2))


def fingerprint(text):
    """Hash an SVG, ignoring the export date and filename KiCad writes into it."""
    return hashlib.sha256(re.sub(r"<title>.*?</title>", "", text, flags=re.S).encode()).hexdigest()


def crop(text, box):
    x0, y0, x1, y1 = box
    view = f'width="{x1 - x0:.4f}mm" height="{y1 - y0:.4f}mm" viewBox="{x0:.4f} {y0:.4f} {x1 - x0:.4f} {y1 - y0:.4f}"'
    return re.sub(r'width="[^"]*"\s+height="[^"]*"\s+viewBox="[^"]*"', view, text, count=1)


def read_netlist(cli, sch_path, tmp):
    """Returns ({ref: {value, footprint}}, {net name: set of 'ref.pin'})."""
    out = os.path.join(tmp, "netlist.net")
    run(cli, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", out, sch_path)
    with open(out, encoding="utf-8") as f:
        text = f.read()
    parts = {}
    for chunk in re.split(r"\(comp\s+", text)[1:]:
        ref = re.search(r'\(ref "([^"]*)"\)', chunk)
        value = re.search(r'\(value "([^"]*)"\)', chunk)
        footprint = re.search(r'\(footprint "([^"]*)"\)', chunk)
        if ref:
            parts[ref.group(1)] = {"value": value.group(1) if value else "",
                                   "footprint": footprint.group(1) if footprint else ""}
    nets = {}
    for chunk in re.split(r"\(net\s+(?=\(code)", text)[1:]:
        name = re.search(r'\(name "([^"]*)"\)', chunk)
        if name:
            nodes = re.findall(r'\(node\s+\(ref "([^"]*)"\)\s+\(pin "([^"]*)"\)', chunk)
            nets[name.group(1)] = {f"{ref}.{pin}" for ref, pin in nodes}
    return parts, nets


def read_placements(pcb_path):
    """Returns {ref: (x, y, rotation, layer)} for every footprint on the board."""
    with open(pcb_path, encoding="utf-8") as f:
        text = f.read()
    placed = {}
    for chunk in re.split(r'(?m)^\s*\(footprint "', text)[1:]:
        layer = re.search(r'\(layer "([^"]+)"\)', chunk)
        at = re.search(r"\(at (-?[\d.]+) (-?[\d.]+)(?: (-?[\d.]+))?\)", chunk)
        ref = re.search(r'\(property "Reference" "([^"]*)"', chunk)
        if layer and at and ref:
            placed[ref.group(1)] = (float(at.group(1)), float(at.group(2)),
                                    float(at.group(3) or 0), layer.group(1))
    return placed


def diff_parts(old, new):
    rows = []
    for ref in sorted(set(old) | set(new), key=natural_key):
        if ref not in old:
            rows.append(["added", ref, f"{new[ref]['value']} ({new[ref]['footprint']})"])
        elif ref not in new:
            rows.append(["removed", ref, f"{old[ref]['value']} ({old[ref]['footprint']})"])
        else:
            for field in ("value", "footprint"):
                if old[ref][field] != new[ref][field]:
                    rows.append(["changed", ref, f"{field}: {old[ref][field]} → {new[ref][field]}"])
    return rows


def diff_nets(old, new):
    rows = []
    for net in sorted(set(old) | set(new), key=natural_key):
        if net not in old:
            rows.append(["added", net, ", ".join(sorted(new[net], key=natural_key))])
        elif net not in new:
            rows.append(["removed", net, ", ".join(sorted(old[net], key=natural_key))])
        elif old[net] != new[net]:
            details = [f"+{n}" for n in sorted(new[net] - old[net], key=natural_key)]
            details += [f"-{n}" for n in sorted(old[net] - new[net], key=natural_key)]
            rows.append(["changed", net, ", ".join(details)])
    return rows


def diff_placements(old, new):
    rows = []
    for ref in sorted(set(old) | set(new), key=natural_key):
        if ref not in old or ref not in new:
            rows.append(["added" if ref in new else "removed", ref, (new.get(ref) or old[ref])[3]])
            continue
        x0, y0, r0, l0 = old[ref]
        x1, y1, r1, l1 = new[ref]
        details = []
        if abs(x1 - x0) > MOVE_TOLERANCE_MM or abs(y1 - y0) > MOVE_TOLERANCE_MM:
            details.append(f"moved {x1 - x0:+.3f}, {y1 - y0:+.3f} mm")
        if abs(r1 - r0) > MOVE_TOLERANCE_MM:
            details.append(f"rotated {r0:g}° → {r1:g}°")
        if l0 != l1:
            details.append(f"{l0} → {l1}")
        if details:
            rows.append(["changed", ref, ", ".join(details)])
    return rows


def natural_key(text):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def side_data(cli, folder, side, tmp):
    """Renders one version of the board. Returns None if it has no KiCad project."""
    files = project_files(folder)
    if not files:
        return None
    stem, pcb, sch = files
    data = {"sch": {}, "pcb": {}, "parts": {}, "nets": {}, "placed": {}, "bbox": None}
    if os.path.exists(sch):
        data["sch"] = render_schematic(cli, sch, stem, os.path.join(tmp, side, "sch"))
        data["parts"], data["nets"] = read_netlist(cli, sch, os.path.join(tmp, side))
    if os.path.exists(pcb):
        out = os.path.join(tmp, side, "pcb")
        os.makedirs(out, exist_ok=True)
        data["pcb"] = render_pcb(cli, pcb, stem, out)
        data["placed"] = read_placements(pcb)
        data["bbox"] = drawing_bbox(os.path.join(out, "_outline.svg"))
    return data


def publish(kind, base, head, box, out):
    """Copies rendered SVGs into out and returns one viewer entry per sheet or layer."""
    items = []
    labels = list(head) + [label for label in base if label not in head]
    for index, label in enumerate(labels):
        entry = {"label": label, "kind": kind, "base": None, "head": None}
        prints = {}
        # A board crops to its outline; a schematic sheet crops to what's drawn on it.
        sheet_box = box or merged_box(*[drawing_bbox(rendered[label]) for rendered in (base, head) if label in rendered])
        for side, rendered in (("base", base), ("head", head)):
            if label not in rendered:
                continue
            with open(rendered[label], encoding="utf-8") as f:
                text = f.read()
            prints[side] = fingerprint(text)
            if sheet_box:
                text = crop(text, sheet_box)
            entry["size"] = svg_size(text)
            name = f"{side}/{kind}{index}.svg"
            with open(os.path.join(out, name), "w", encoding="utf-8") as f:
                f.write(text)
            entry[side] = name
        entry["changed"] = prints.get("base") != prints.get("head")
        items.append(entry)
    return items


def merged_box(*boxes):
    boxes = [box for box in boxes if box]
    if not boxes:
        return None
    return (min(b[0] for b in boxes) - CROP_MARGIN_MM, min(b[1] for b in boxes) - CROP_MARGIN_MM,
            max(b[2] for b in boxes) + CROP_MARGIN_MM, max(b[3] for b in boxes) + CROP_MARGIN_MM)


def summary_markdown(board, base_name, head_name, items, tables):
    lines = [f"### {board}: `{base_name}` → `{head_name}`", ""]
    changed = [item["label"] for item in items if item["changed"]]
    lines.append("**Drawings changed:** " + (", ".join(changed) if changed else "none"))
    for title, rows in tables:
        lines += ["", f"**{title}:** {len(rows)}" if rows else f"**{title}:** none"]
        if rows:
            lines += ["", "| Change | Item | Details |", "|---|---|---|"]
            lines += [f"| {kind} | {name} | {detail.replace('|', '/')} |" for kind, name, detail in rows]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Visual diff of a KiCad board between two versions.")
    parser.add_argument("board", help="folder holding the .kicad_pro")
    parser.add_argument("base", nargs="?", default="HEAD", help="old version (default: HEAD)")
    parser.add_argument("head", nargs="?", help="new version (default: your working folder)")
    parser.add_argument("-o", "--output", help="output folder (default: pcbdiff-out/BOARD)")
    parser.add_argument("--open", action="store_true", help="open the result in your browser")
    args = parser.parse_args()

    board = os.path.normpath(args.board)
    name = os.path.basename(os.path.abspath(board))
    if not os.path.isdir(board) and not args.head:
        sys.exit(f"No such board folder: {board}")
    cli = find_cli()
    out = args.output or os.path.join(os.path.dirname(os.path.abspath(board)), "pcbdiff-out", name)

    with tempfile.TemporaryDirectory() as tmp:
        base_dir = extract_rev(args.base, board, os.path.join(tmp, "src-base"))
        head_dir = extract_rev(args.head, board, os.path.join(tmp, "src-head")) if args.head else board
        base = side_data(cli, base_dir, "base", tmp)
        head = side_data(cli, head_dir, "head", tmp)
        if not base and not head:
            sys.exit(f"Neither version of {board} holds a .kicad_pro.")
        empty = {"sch": {}, "pcb": {}, "parts": {}, "nets": {}, "placed": {}, "bbox": None}
        base, head = base or empty, head or empty

        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(os.path.join(out, "base"))
        os.makedirs(os.path.join(out, "head"))
        box = merged_box(base["bbox"], head["bbox"])
        items = publish("sch", base["sch"], head["sch"], None, out)
        items += publish("pcb", base["pcb"], head["pcb"], box, out)

    tables = [("Components", diff_parts(base["parts"], head["parts"])),
              ("Nets", diff_nets(base["nets"], head["nets"])),
              ("Placement", diff_placements(base["placed"], head["placed"]))]
    head_name = args.head or "working folder"
    summary = summary_markdown(name, args.base, head_name, items, tables)
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(summary)

    template = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pcbdiff_viewer.html")
    with open(template, encoding="utf-8") as f:
        page = f.read()
    data = {"board": name, "base": args.base, "head": head_name, "items": items,
            "tables": [{"title": title, "rows": rows} for title, rows in tables]}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = page.replace("__TITLE__", html.escape(name)).replace("__DATA__", payload)
    index = os.path.join(out, "index.html")
    with open(index, "w", encoding="utf-8") as f:
        f.write(page)

    print(summary)
    print(f"Open {index}")
    if args.open:
        webbrowser.open("file://" + os.path.abspath(index))


if __name__ == "__main__":
    main()
