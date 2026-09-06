#!/usr/bin/env python3
"""Materialise the served asset tree from the image, keeping console installs.

The served directory is usually a volume so maps installed through the admin
console survive redeploys. A plain volume would also freeze the bundled assets at
whatever the first deploy shipped, so instead the image copy is authoritative for
everything it ships and anything extra found in the volume is preserved and
re-indexed. The manifest is rebuilt from both on every start.

    python3 sync-assets.py <image-assets-dir> <served-dir>
"""

import json
import pathlib
import shutil
import sys
import zlib


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    source, served = (pathlib.Path(a) for a in sys.argv[1:3])
    if not source.is_dir():
        raise SystemExit(f"[assets] missing image assets at {source}")
    served.mkdir(parents=True, exist_ok=True)

    shipped = set()
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = served / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        shipped.add(relative.as_posix())
        if not target.exists() or target.stat().st_size != item.stat().st_size:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)

    manifest = json.loads((source / "manifest.json").read_text())
    indexed = {entry["name"] for entry in manifest}

    extras = 0
    for pk3 in sorted(served.rglob("*.pk3")):
        # Index every pak present that the manifest does not already list, no
        # matter how it got here. Maps kept out of version control are still in
        # the build context locally, so they must be picked up the same way a
        # console-installed map in the volume is.
        # "<game>/<crc>-<name>.pk3" as written by the console and add-map.py
        parent = pk3.parent.name
        stem = pk3.name.split("-", 1)[-1] if "-" in pk3.name else pk3.name
        name = f"{parent}/{stem}"
        if name in indexed:
            continue
        data = pk3.read_bytes()
        manifest.append({"name": name, "compressed": len(data),
                         "checksum": zlib.crc32(data) & 0xFFFFFFFF})
        indexed.add(name)
        extras += 1

    (served / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"[assets] {len(shipped)} bundled, {extras} indexed on the fly, "
          f"{len(manifest)} manifest entries", flush=True)


if __name__ == "__main__":
    main()
