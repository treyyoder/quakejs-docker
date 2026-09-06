#!/usr/bin/env python3
"""Prepare your own retail Quake 3 pak0.pk3 for this server.

The image ships the Quake 3 demo content - four maps, six bots - because the
retail assets are licensed and cannot be redistributed. Anyone running this
server supplies their own copy, and it is never baked into the image. The
point-release paks (pak1-pak8) are freely redistributable and already bundled,
so pak0.pk3 is the only file needed.

This trims a copy of yours and writes it somewhere you can serve or upload:

    python tools/add-retail.py "C:/Program Files/Quake III Arena/baseq3"
    python tools/add-retail.py ~/.q3a/baseq3/pak0.pk3 -o /srv/www/pak0.pk3

Then get it to the server either way round:

    upload it   console -> Maps -> Game assets
    or link it  EXTRA_PAKS="https://your.host/pak0.pk3#sha256=..."

The soundtrack and the intro cinematics are left out by default, because the
browser client cannot carry the whole file - see TRIMMABLE below. Pass --full to
keep them if only the dedicated server matters to you.
"""

import argparse
import hashlib
import pathlib
import re
import shutil
import sys
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "dist" / "pak0.pk3"
# The demo pak0 holds four maps and six bots. Anything genuinely retail clears
# these by a wide margin, so they separate the two without hard-coding a size
# or a checksum that a re-release could change.
MIN_RETAIL_MAPS = 20
MIN_RETAIL_BOTS = 20
BOT_NAME_RE = re.compile(r'^\s*name\s+"?([A-Za-z0-9_ ]+?)"?\s*$', re.MULTILINE)

# Retail pak0 is 457 MB and the browser client cannot carry that. It downloads
# every base pak before the game starts and holds them in memory, and
# emscripten's MEMFS turns a file's bytes into a plain JS array - one element
# per byte - whenever the file is resized. Half a billion elements is several
# gigabytes and the load dies with "RangeError: Invalid array length".
#
# Nearly two thirds of the file is the soundtrack and the intro cinematics,
# which a browser deathmatch server never plays. Dropping them leaves the maps,
# models, textures, bots and gameplay sounds untouched at about a third of the
# size. The cost is no in-game music and no id logo movie.
TRIMMABLE = ("music/", "video/")


def survey(pk3):
    """Maps and bot definitions a pak provides."""
    with zipfile.ZipFile(pk3) as archive:
        names = archive.namelist()
        maps = sorted({pathlib.PurePath(n).stem.lower() for n in names
                       if n.lower().startswith("maps/") and n.lower().endswith(".bsp")})
        bots = set()
        for entry in names:
            if entry.lower().endswith("scripts/bots.txt"):
                bots |= set(BOT_NAME_RE.findall(
                    archive.read(entry).decode("latin-1")))
    return maps, sorted(bots)


def trimmable_bytes(pk3):
    """How much of a pak is content the browser client will never play."""
    with zipfile.ZipFile(pk3) as archive:
        return sum(i.compress_size for i in archive.infolist()
                   if i.filename.lower().startswith(TRIMMABLE))


def write_slim(source, target):
    """Copy a pak, leaving out the music and cinematics."""
    kept = dropped = 0
    with zipfile.ZipFile(source) as src:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
            for info in src.infolist():
                if info.filename.lower().startswith(TRIMMABLE):
                    dropped += 1
                    continue
                # Passing the ZipInfo through keeps each entry's original
                # compression method and timestamp.
                out.writestr(info, src.read(info.filename))
                kept += 1
    return kept, dropped


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=pathlib.Path,
                        help="your retail baseq3 directory, or pak0.pk3 itself")
    parser.add_argument("-o", "--output", type=pathlib.Path, default=DEFAULT_OUT,
                        help=f"where to write it (default {DEFAULT_OUT})")
    parser.add_argument("--full", action="store_true",
                        help="keep the music and cinematics; the browser client "
                             "will not be able to load the result")
    parser.add_argument("--force", action="store_true",
                        help="proceed even if the file does not look retail")
    args = parser.parse_args()

    source = args.source
    if source.is_dir():
        source = source / "pak0.pk3"
    if not source.is_file():
        sys.exit(f"not a file: {source}")

    try:
        maps, bots = survey(source)
    except (zipfile.BadZipFile, OSError) as exc:
        sys.exit(f"{source}: {type(exc).__name__}: {exc}")

    size = source.stat().st_size
    print(f"{source}")
    print(f"  {size / 1048576:.0f} MiB, {len(maps)} maps, {len(bots)} bots")

    if len(maps) < MIN_RETAIL_MAPS or len(bots) < MIN_RETAIL_BOTS:
        message = (f"this looks like the demo pak0, not retail "
                   f"(expected at least {MIN_RETAIL_MAPS} maps and "
                   f"{MIN_RETAIL_BOTS} bots)")
        if not args.force:
            sys.exit(f"\nrefusing: {message}\nUse --force to proceed anyway.")
        print(f"\nwarning: {message}")

    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.full:
        print("\nkeeping the music and cinematics (--full): the dedicated server")
        print("will be fine, but the browser client cannot load a pak this large.")
        shutil.copyfile(source, output)
    else:
        trim = trimmable_bytes(source)
        print(f"\nleaving out the music and cinematics ({trim / 1048576:.0f} MiB, "
              f"{100 * trim / size:.0f}% of the file), which a browser")
        print("never plays. Pass --full to keep them. This takes a minute...")
        kept, dropped = write_slim(source, output)
        print(f"  kept {kept} files, dropped {dropped}")

    final = output.stat().st_size
    sha = digest(output)
    print(f"\nwrote {output}")
    print(f"  {final / 1048576:.0f} MiB"
          + ("" if args.full else f", down from {size / 1048576:.0f} MiB"))
    print(f"  sha256 {sha}")
    print("\nGet it to the server either way round:")
    print("  upload   console -> Maps -> Game assets")
    print("  or link  serve the file, then set")
    print(f'           EXTRA_PAKS="https://your.host/pak0.pk3#sha256={sha}"')
    print("\nIt is licensed content, so keep it out of any image you publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
