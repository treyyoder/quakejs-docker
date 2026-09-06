#!/usr/bin/env python3
"""Report shaders a map references but that nothing in the bundle provides.

A map built against textures you do not have renders with placeholder surfaces, so
this compares each BSP's shader table against the map's own pk3 and the base paks
before the map is added.

Read the count as a smell, not a verdict. Quake 3's own stock maps reference shaders
that ship in no pak at all - measured against a full retail install, q3dm2 and q3dm7
each have five, q3dm17 three - and they render correctly, because the engine quietly
falls back to a default. A handful of misses is normal; a large number means the map
was built against a texture set this server does not have.

Base paks are the pak*.pk3 a running container has unpacked into
/quakejs/base/baseq3 (docker cp them out, or point at any directory of paks).

    python tools/check-map.py --base ./base mymap.pk3 [more.pk3 ...]
"""

import argparse
import pathlib
import struct
import sys
import zipfile

# Shaders the engine resolves internally rather than from an asset.
BUILTIN_PREFIXES = ("textures/common/", "textures/nodraw")
BUILTIN_NAMES = {"noshader"}
IMAGE_EXTS = (".jpg", ".tga", ".png")


def bsp_shader_names(data):
    if data[:4] != b"IBSP":
        raise ValueError("not an IBSP file")
    offset, length = struct.unpack_from("<ii", data, 8 + 1 * 8)  # lump 1 = textures
    return [data[offset + i * 72: offset + i * 72 + 64].split(b"\0")[0].decode("latin-1")
            for i in range(length // 72)]


def index(pk3_paths):
    """Return (asset names, shader names) provided by a set of pk3s."""
    files, shaders = set(), set()
    for path in pk3_paths:
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"warning: skipping {path}: {exc}", file=sys.stderr)
            continue
        for name in archive.namelist():
            low = name.lower()
            files.add(low)
            if low.endswith(IMAGE_EXTS):
                files.add(low.rsplit(".", 1)[0])
            if low.startswith("scripts/") and low.endswith(".shader"):
                try:
                    text = archive.read(name).decode("latin-1")
                except (OSError, zipfile.BadZipFile):
                    continue
                for line in text.splitlines():
                    stripped = line.strip()
                    if (stripped and not stripped.startswith(("//", "{", "}"))
                            and line[:1] not in (" ", "\t") and " " not in stripped):
                        shaders.add(stripped.lower())
    return files, shaders


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pk3", nargs="+", type=pathlib.Path)
    ap.add_argument("--base", type=pathlib.Path, required=True,
                    help="directory holding the base pak*.pk3 files")
    ap.add_argument("--max-missing", type=int, default=0,
                    help="exit non-zero if any map exceeds this many missing shaders")
    args = ap.parse_args()

    base_files, base_shaders = index(sorted(args.base.glob("*.pk3")))
    if not base_files:
        sys.exit(f"no pak files found in {args.base}")

    worst = 0
    for pk3 in args.pk3:
        own_files, own_shaders = index([pk3])
        archive = zipfile.ZipFile(pk3)
        for entry in (n for n in archive.namelist() if n.lower().endswith(".bsp")):
            names = bsp_shader_names(archive.read(entry))
            missing = []
            for name in names:
                low = name.lower()
                if low in BUILTIN_NAMES or low.startswith(BUILTIN_PREFIXES):
                    continue
                if low in own_shaders or low in base_shaders:
                    continue
                if any(low + ext in own_files or low + ext in base_files
                       for ext in IMAGE_EXTS + ("",)):
                    continue
                missing.append(name)
            worst = max(worst, len(missing))
            print(f"{pk3.name}: {entry} - {len(names)} shaders, {len(missing)} missing")
            for name in missing:
                print(f"    {name}")

    if worst > args.max_missing:
        sys.exit(f"\n{worst} missing shaders exceeds --max-missing {args.max_missing}")


if __name__ == "__main__":
    main()
