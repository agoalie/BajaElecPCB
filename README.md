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

1. Update first: `git switch main && git pull`
2. Branch: `git switch -c rpm/move-connector`
3. Make your change in KiCad and save.
4. See what changed: `python3 tools/pcbdiff.py Baja2027_RPM --open`
5. Commit and push: `git add Baja2027_RPM && git commit -m "RPM: move J5 to the board edge" && git push -u origin HEAD`
6. Open a pull request. The checks below run on it automatically.

**Only one person should edit a given `.kicad_pcb` or `.kicad_sch` at a time.** Git can't
merge these files. If two people change the same one, Git reports a conflict, and one of
them has to redo their work on top of the other's version. Say who's working on which
board before you start. Separate schematic sheets can be edited by different people.

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

### Run ERC and DRC: `tools/pcbcheck.py`

```sh
python3 tools/pcbcheck.py Baja2027_RPM
```

Prints violation counts and checks that the PCB matches the schematic. Add `--strict` to
exit with an error if any error-level violation is found.

### Export for JLCPCB: `tools/kicad_jlc_export.py`

```sh
python3 tools/kicad_jlc_export.py Baja2027_RPM
```

Writes the gerber and drill zip, BOM, and CPL to `<board>/jlc_export/`. Rotation fixes
found in JLCPCB's preview go in `<board>/jlc_export.json`.

### Pull request checks

Each pull request that changes a board runs ERC, DRC, and the visual diff on GitHub. Open
the PR's **Checks** tab and then **PCB checks** to see the results. Download the `pcbdiff`
artifact and open `index.html` to view the diff. Violations are reported but don't block
merging; many boards carry intentional ones.

KiCad's project window also has built-in Git support (commit, push, pull) under the
right-click menu in the file tree, if you'd rather not use the command line.

## Adding a board

1. Create the project in its own top-level folder, such as `Baja2027_DAQ/`, with exactly
   one `.kicad_pro`.
2. Keep project-specific footprints in `<board>/Library.pretty` and 3D models in
   `<board>/3dmodels`, referenced through `${KIPRJMOD}`, so the board works on any computer.
3. Add it to the table above.

## What's tracked

**Tracked:** schematics, PCB, project and rule files, local libraries, part 3D models, and
fabrication outputs.

**Ignored:** KiCad backups and local history, per-user `.kicad_prl` files, caches, and
whole-board STEP exports. Regenerate STEP files with **File → Export → STEP**. GitHub
rejects any file over 100 MB, so keep part models small.

## Shared library

Some footprints and 3D models come from the team library on SharePoint, through the
`SHAREPOINT_LIB` path variable in KiCad (**Preferences → Configure Paths**). Point it at
your synced copy of `2027/Design/Electrical/KiCAD`. Without it, those parts' 3D models
won't appear, but the schematic, layout, and these tools still work.
