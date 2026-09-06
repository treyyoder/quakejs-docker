#!/usr/bin/env python3
"""Fetch pak files named in EXTRA_PAKS into the served asset tree.

Assets you are licensed to use but not to redistribute - retail Quake 3 content,
above all - should not be baked into an image that gets pushed to a registry.
This pulls them at start from somewhere you control, so the image stays free of
them and the deployment supplies them.

    EXTRA_PAKS="https://files.example.com/q3/pak0.pk3"

Entries are separated by whitespace, newlines or commas, and each may carry:

    a name, when the URL does not end in the filename you want
        pak0.pk3=https://example.com/download?id=7

    a SHA-256, checked before the file is accepted
        https://example.com/pak0.pk3#sha256=1a2b3c...

Downloads land in the served tree, which is normally a volume, so a file is
fetched once and then reused. Without a SHA-256 an already-present file is left
alone; with one, a file that no longer matches is fetched again.

A pak that fails to arrive is reported and skipped rather than being fatal: the
server falls back to whatever the bundled manifest provides, which for retail
pak0 means the demo content it shipped with.

    python3 fetch-paks.py <served-dir>
"""

import hashlib
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}\.pk3$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TIMEOUT = 60
CHUNK = 1 << 20
# No pak is anywhere near this; a host that keeps sending past it is not
# serving a pak, and the served tree is a volume it must not be allowed to fill.
MAX_BYTES = 1 << 30
UA = "quakejs-docker"


def parse(raw):
    """EXTRA_PAKS -> [(filename, url, sha256 or None)]."""
    wanted = []
    for entry in re.split(r"[\s,]+", raw.strip()):
        if not entry:
            continue
        digest = None
        url = entry
        if "#" in url:
            url, fragment = url.split("#", 1)
            fragment = fragment.lower()
            if fragment.startswith("sha256="):
                fragment = fragment[len("sha256="):]
            if SHA_RE.match(fragment):
                digest = fragment
            else:
                print(f"[paks] ignoring unreadable checksum on {url}", flush=True)
        name = None
        # "name=url", but only when the left side is a filename rather than the
        # scheme of a URL that happens to contain an equals sign.
        if "=" in url and "://" not in url.split("=", 1)[0]:
            name, url = url.split("=", 1)
        if not name:
            name = pathlib.PurePosixPath(urllib.parse.urlparse(url).path).name
        wanted.append((name, url, digest))
    return wanted


def copy_capped(source, dest, limit=MAX_BYTES):
    """Copy source to the open file dest a chunk at a time, refusing to go
    past limit. Returns the size copied."""
    size = 0
    while True:
        chunk = source.read(CHUNK)
        if not chunk:
            return size
        size += len(chunk)
        if size > limit:
            raise ValueError(f"larger than the {limit >> 20} MiB limit; refusing to fill the volume")
        dest.write(chunk)


def stream(url, dest, limit=MAX_BYTES):
    """Download to a file. Never held in memory: a base pak runs to hundreds of
    megabytes, and buffering one would spike a small host at start. Capped,
    and refused up front when the host announces more than the cap."""
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        announced = response.headers.get("Content-Length")
        if announced and announced.isdigit() and int(announced) > limit:
            raise ValueError(f"{int(announced) >> 20} MiB announced, over the {limit >> 20} MiB limit")
        with dest.open("wb") as handle:
            return copy_capped(response, handle, limit)


def sums(path):
    """(sha256, crc32) in one pass over the file."""
    sha = hashlib.sha256()
    crc = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            sha.update(chunk)
            crc = zlib.crc32(chunk, crc)
    return sha.hexdigest(), crc & 0xFFFFFFFF


def current(target_dir, name):
    """An already-fetched copy of this pak, whatever checksum it carries."""
    return sorted(target_dir.glob(f"*-{name}"))


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    served = pathlib.Path(sys.argv[1])
    raw = os.environ.get("EXTRA_PAKS", "")
    if not raw.strip():
        return 0

    game = os.environ.get("FS_GAME", "baseq3")
    target_dir = served / game
    target_dir.mkdir(parents=True, exist_ok=True)

    # EXTRA_PAKS is set by whoever runs the container, not by a request, so an
    # internal address here is a deliberate choice - pulling a licensed pak0 off
    # a LAN file server, say - not an SSRF. So the scheme is checked but the
    # address is not; the public-only guard lives on the lvlworld path
    # (qadmin/assets._assert_public_url), where the URLs come from untrusted
    # pages and their redirects.
    for name, url, digest in parse(raw):
        if not NAME_RE.match(name):
            print(f"[paks] refusing suspicious name {name!r} from {url}", flush=True)
            continue
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            print(f"[paks] refusing non-http url for {name}: {url}", flush=True)
            continue

        existing = current(target_dir, name)
        if existing:
            if not digest:
                print(f"[paks] {name} already present, leaving it", flush=True)
                continue
            if sums(existing[0])[0] == digest:
                print(f"[paks] {name} already present and matches its checksum",
                      flush=True)
                continue
            print(f"[paks] {name} does not match its checksum, fetching again",
                  flush=True)

        # Not named .pk3, so a start interrupted mid-download cannot leave a
        # half-written file for the manifest to index.
        staging = target_dir / f".fetching-{name}.part"
        try:
            print(f"[paks] fetching {name} from {url}", flush=True)
            size = stream(url, staging)
            sha, crc = sums(staging)
            if digest and sha != digest:
                raise ValueError(f"checksum mismatch, got {sha}")
            # Reading the central directory is enough; testzip() would CRC every
            # one of a few thousand entries and stall startup for no more proof.
            with zipfile.ZipFile(staging) as archive:
                archive.namelist()
            for stale in current(target_dir, name):
                stale.unlink()
            staging.replace(target_dir / f"{crc}-{name}")
        except (urllib.error.URLError, OSError, ValueError,
                zipfile.BadZipFile) as exc:
            print(f"[paks] could not install {name}: {type(exc).__name__}: {exc}",
                  flush=True)
            try:
                staging.unlink()
            except OSError:
                pass
            continue
        print(f"[paks] installed {crc}-{name} "
              f"({size / 1048576:.0f} MiB, sha256 {sha[:12]}...)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
