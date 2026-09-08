"""Command line entry point."""

import argparse
import os
import sys

from .project import Project, ProjectError, load_profiles


def build_parser():
    ap = argparse.ArgumentParser(
        prog="gexedit",
        description="Map editor for the Gex GBC disassemblies (gex2gbc, gex3gbc).",
        epilog="With no options it opens the GUI. The repo is auto-detected as gex2 or "
               "gex3 from the map table its source contains.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", nargs="?", default=".",
                    help="path to a disassembly repo (default: the current directory)")
    ap.add_argument("--game", help="force a profile instead of detecting one")
    ap.add_argument("--games", action="store_true", help="list known games and exit")
    ap.add_argument("--list", action="store_true", help="list the repo's maps and exit")
    ap.add_argument("--render", metavar="MAP", help="render a map to PNG and exit")
    ap.add_argument("-o", "--output", default="map.png")
    ap.add_argument("--scale", type=int, default=1)
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--region", help="cx,cy,cw,ch in cells")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.games:
        for name, prof in sorted(load_profiles().items()):
            print("%-6s %-40s blocks %dx%d tiles, manifest %s"
                  % (name, prof.get("title", ""), prof["block_tiles"],
                     prof["block_tiles"], prof["manifest"]["source"]))
        return 0

    try:
        project = Project(args.repo, args.game)
    except ProjectError as e:
        sys.exit("error: %s" % e)

    if args.list:
        print("%s - %s" % (project.profile.get("title", project.game), project.root))
        print("%d maps, blocks are %dx%d tiles\n"
              % (len(project.maps), project.block_tiles, project.block_tiles))
        for m in project.maps:
            print("  $%02x  %-42s %4dx%-4d %s"
                  % (m.id, m.name, m.width, m.height, m.level or ""))
        return 0

    if args.render:
        from .render import MapView
        info = next((m for m in project.maps if m.name == args.render), None)
        if info is None:
            sys.exit("error: %s has no map called %s (try --list)"
                     % (project.game, args.render))
        region = tuple(int(v) for v in args.region.split(",")) if args.region else None
        img = MapView(project, info).render(region=region, scale=args.scale,
                                            grid=args.grid)
        img.save(args.output)
        print("%s %dx%d cells -> %s (%dx%d px)"
              % (info.name, info.width, info.height, args.output, img.width, img.height))
        return 0

    try:
        from .app import run
    except ImportError as e:
        sys.exit(_gui_import_help(e))
    return run(project)


def _gui_import_help(exc):
    """Say which dependency is actually missing.

    Everything the GUI needs is optional to the rest of the tool, so this is the one
    place a user meets a broken install - it is worth naming the package and the fix
    rather than reporting a bare ImportError.
    """
    text = str(exc)
    if "gexedit.app" in text:
        return "the GUI module is missing from this install (%s)." % text

    hints = [
        ("tkinter", "Tk is not installed for this Python.\n"
                    "  Debian/Ubuntu/WSL: sudo apt install python3-tk\n"
                    "  Windows/macOS:     it ships with the python.org build"),
        ("_imaging", "Pillow is installed but its compiled part is missing, which\n"
                     "usually means a half-replaced install.\n"
                     "  Debian/Ubuntu/WSL: sudo apt install --reinstall python3-pil "
                     "python3-pil.imagetk\n"
                     "  or in a venv:      pip install --force-reinstall pillow"),
        ("ImageTk", "Pillow is missing its Tk bridge.\n"
                    "  Debian/Ubuntu/WSL: sudo apt install python3-pil.imagetk\n"
                    "  or in a venv:      pip install --force-reinstall pillow"),
        ("PIL", "Pillow is not installed.  pip install pillow"),
        ("numpy", "numpy is not installed.  pip install numpy"),
    ]
    for needle, advice in hints:
        if needle in text:
            return "cannot start the GUI: %s\n\n%s\n\n" \
                   "--list and --render need none of this and still work." % (text, advice)
    return "cannot start the GUI: %s\n\n" \
           "--list and --render may still work." % text
