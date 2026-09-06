#!/usr/bin/env python3
"""Add .pk3 files to the bundled assets so the client and server can load them.

Assets are served from include/assets/ and discovered through manifest.json, where
each entry is keyed by the unsigned CRC32 the QuakeJS content pipeline computes.
Files are stored as <checksum>-<name>.pk3 to match that lookup.

    python tools/add-map.py mymap.pk3 [more.pk3 ...] [--game baseq3]
"""

import argparse
import json
import pathlib
import shutil
import sys
import zlib

ASSETS = pathlib.Path(__file__).resolve().parent.parent / "include" / "assets"


def checksum(path):
    return zlib.crc32(path.read_bytes()) & 0xFFFFFFFF


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pk3", nargs="+", type=pathlib.Path)
    ap.add_argument("--game", default="baseq3", help="mod directory (default: baseq3)")
    args = ap.parse_args()

    manifest_path = ASSETS / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    by_name = {e["name"]: e for e in manifest}

    for src in args.pk3:
        if not src.is_file():
            sys.exit(f"not a file: {src}")
        crc = checksum(src)
        name = f"{args.game}/{src.name}"
        dest = ASSETS / args.game / f"{crc}-{src.name}"

        stale = [p for p in (ASSETS / args.game).glob(f"*-{src.name}") if p != dest]
        for old in stale:
            old.unlink()
            print(f"removed stale {old.name}")

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

        entry = {"name": name, "compressed": dest.stat().st_size, "checksum": crc}
        if name in by_name:
            by_name[name].update(entry)
            action = "updated"
        else:
            manifest.append(entry)
            by_name[name] = entry
            action = "added"
        print(f"{action} {name} (crc32 {crc}, {entry['compressed']} bytes)")

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
