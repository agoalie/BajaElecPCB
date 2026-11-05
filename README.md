# BajaElecPCB

Every PCB the Queen's Baja electrical team designs this year, one folder per board.

| Board | What it is |
|---|---|
| [`Baja2027_RPM`](Baja2027_RPM) | RPM sensor board |

All boards use KiCad 10. A file saved in KiCad 10 won't open in an older version.

## Getting started

```sh
git clone https://github.com/agoalie/BajaElecPCB.git
```

Clone it somewhere outside OneDrive or SharePoint. Sync tools can corrupt a `.git` folder.

## Daily workflow

Each board gets its own branch while it's being designed, such as `daq`. Small fixes to a
finished board can use a short branch, such as `rpm/move-connector`.

1. Update first: `git switch main && git pull`
2. Branch: `git switch -c daq`
3. Make your change in KiCad and save.
4. See what changed: `python3 tools/pcbdiff.py Baja2027_DAQ --open`
5. Commit and push: `git add Baja2027_DAQ && git commit -m "DAQ: add CAN transceiver" && git push -u origin HEAD`
6. Open a pull request early, with a title like `WIP: DAQ board`. Before asking for a
   review, run ERC and DRC in KiCad, with DRC's option to test the PCB against the schematic.
7. Merge at each milestone: once the schematic has been reviewed, and again when the layout
   has been reviewed and is ready to order.

**Only one person should edit a given `.kicad_pcb` or `.kicad_sch` at a time.** Git can't
merge these files. If two people change the same one, Git reports a conflict, and one of
them has to redo their work on top of the other's version. Say who's working on which
board before you start. Separate schematic sheets can be edited by different people.

**A modified `.kicad_pcb` doesn't always mean the design changed.** KiCad can reorder a
file's contents when it saves, which shows up as hundreds of changed lines. Run
`pcbdiff.py` to see whether anything real changed.

## Tagging what you order

When a board goes to fabrication, export it, merge to `main`, and tag that commit:

```sh
git switch main && git pull
git tag rpm-v1.1
git push origin rpm-v1.1
```

Name tags `<board>-v<version>` and use the same version on the silkscreen. Use a new version
for each physically different board, and don't move a tag once that board has been ordered.
To see what has changed since an order, run `python3 tools/pcbdiff.py Baja2027_RPM rpm-v1.0 --open`.

## Tools

All of these need Python 3 and KiCad, and nothing else.

### See a visual diff: `tools/pcbdiff.py`

```sh
python3 tools/pcbdiff.py Baja2027_RPM                # last commit vs. your working folder
python3 tools/pcbdiff.py Baja2027_RPM main --open    # main vs. your work, opened in the browser
python3 tools/pcbdiff.py Baja2027_RPM HEAD~3 HEAD    # any two versions
```

This builds `pcbdiff-out/<board>/index.html`, which has:

- **Every schematic sheet and PCB layer**, with changed ones marked by a dot.
- **Three views:** overlay (red removed, green added), side by side, and a swipe slider.
  Scroll to zoom and drag to pan.
- **A change list:** components added, removed, or edited, net connections that changed,
  and footprints that moved.

### Export for JLCPCB: `tools/kicad_jlc_export.py`

```sh
python3 tools/kicad_jlc_export.py Baja2027_RPM
```

Writes the gerber and drill zip, BOM, and CPL to `<board>/jlc_export/`. Rotation fixes
found in JLCPCB's preview go in `<board>/jlc_export.json`. This is the only fabrication
output to commit, so re-run it and commit the result before ordering. Don't commit exports
from KiCad's plot or BOM windows, because they go stale.

If you'd rather not use the command line, [GitHub Desktop](https://desktop.github.com/)
works with this repo. Add `BajaElecPCB` as a local repository. KiCad's built-in Git menu
may not appear here, because each project sits one folder below the repo root.

## Adding a board

1. Create the project in its own top-level folder, such as `Baja2027_DAQ/`, with exactly
   one `.kicad_pro`.
2. Keep project-specific footprints in `<board>/Library.pretty` and 3D models in
   `<board>/3dmodels`, referenced through `${KIPRJMOD}`, so the board works on any computer.
3. Open and save it in KiCad 10 once, then commit before making real changes. This stops
   a later save from reordering the whole file, which matters most for a board started
   elsewhere or in an older version.
4. Add it to the table above.

## What's tracked

**Tracked:** schematics, PCB, project and rule files, local libraries, part 3D models, and
`jlc_export/`.

**Ignored:** KiCad backups and local history, per-user `.kicad_prl` files, caches, and
whole-board STEP exports. Regenerate STEP files with **File → Export → STEP**. GitHub
rejects any file over 100 MB, so keep part models small.

## Shared library

Some footprints and 3D models come from the team library on SharePoint, through the
`SHAREPOINT_LIB` path variable in KiCad (**Preferences → Configure Paths**). Point it at
your synced copy of `2027/Design/Electrical/KiCAD`. Without it, those parts' 3D models
won't appear, but the schematic, layout, and these tools still work.
