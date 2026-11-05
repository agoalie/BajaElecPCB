#!/usr/bin/env python3
"""Run KiCad's ERC and DRC on a board and report the results.

Usage:
    pcbcheck.py BOARD [--strict] [-o OUTPUT_DIR]

Prints a Markdown summary and writes the full erc.json and drc.json to OUTPUT_DIR
(default: pcbdiff-out/BOARD/checks). With --strict, exits 1 if either check finds
an error-level violation or the PCB doesn't match the schematic.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys

from pcbdiff import find_cli, project_files


def run_check(cli, kind, source, report, extra):
    command = [cli, kind[0], kind[1], "--format", "json", "--severity-all", "--units", "mm",
               "-o", report, *extra, source]
    done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if done.returncode or not os.path.exists(report):
        sys.exit(f"{kind[1].upper()} did not run:\n{done.stderr.decode(errors='replace')}")
    with open(report, encoding="utf-8") as f:
        return json.load(f)


def count(violations):
    return collections.Counter(item.get("severity", "error") for item in violations)


def describe(name, violations):
    tally = count(violations)
    line = f"| {name} | {tally['error']} | {tally['warning']} |"
    return line, tally["error"]


def main():
    parser = argparse.ArgumentParser(description="Run ERC and DRC on a KiCad board.")
    parser.add_argument("board", help="folder holding the .kicad_pro")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any error-level violation is found")
    parser.add_argument("-o", "--output", help="report folder")
    args = parser.parse_args()

    files = project_files(args.board)
    if not files:
        sys.exit(f"No .kicad_pro in {args.board}")
    stem, pcb, sch = files
    name = os.path.basename(os.path.abspath(args.board))
    out = args.output or os.path.join(os.path.dirname(os.path.abspath(args.board)), "pcbdiff-out", name, "checks")
    os.makedirs(out, exist_ok=True)
    cli = find_cli()

    erc = run_check(cli, ("sch", "erc"), sch, os.path.join(out, "erc.json"), [])
    drc = run_check(cli, ("pcb", "drc"), pcb, os.path.join(out, "drc.json"), ["--schematic-parity"])
    erc_items = [v for sheet in erc.get("sheets", []) for v in sheet.get("violations", [])]

    rows = [describe("ERC", erc_items),
            describe("DRC", drc.get("violations", [])),
            describe("Unconnected items", drc.get("unconnected_items", [])),
            describe("Schematic parity", drc.get("schematic_parity", []))]
    print(f"### {name} checks\n\n| Check | Errors | Warnings |\n|---|---|---|")
    print("\n".join(line for line, _ in rows))
    print(f"\nFull reports: {out}")
    if args.strict and sum(errors for _, errors in rows):
        sys.exit(1)


if __name__ == "__main__":
    main()
