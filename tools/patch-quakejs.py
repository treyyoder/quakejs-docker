#!/usr/bin/env python3
"""Patch the QuakeJS server and browser client so supplied base paks work.

Two independent fixes, both needed before a server can use assets its owner
supplied rather than the bundled demo content. Applied at image build time to
/quakejs/build/ioq3ded.js and /var/www/html/ioquake3.js.

1. Installers that have nothing to do
   -----------------------------------
   Both binaries carry a hardcoded list of "installers": self-extracting
   archives they download and unpack to produce the base paks. The demo
   installer exists purely to produce `baseq3/pak0.pk3`, and the checksum it
   validates against is the demo file's. On a server whose owner supplied retail
   assets the bootstrapper therefore sees a pak0 it does not recognise, decides
   the installer is out of date, and downloads the demo over the top of it -
   every single start.

   An installer whose paks the manifest already carries is now skipped. Nothing
   is skipped unless the manifest genuinely ships those files, so the demo path
   is untouched when no supplied assets are present.

   An installer the manifest does not list at all is skipped too. The image
   ships no game content it has no right to redistribute, so the point release
   is simply absent unless an operator supplies it - an ordinary state, not an
   error. The line this replaces called a `callback` that was never in scope,
   turning a missing installer into a ReferenceError pointing nowhere near the
   cause.

2. Truncating a large file
   ------------------------
   emscripten's MEMFS keeps a written file as a Uint8Array, but any resize first
   converts it with `Array.prototype.slice` into a plain JS array holding one
   element per byte. Truncating to zero is what `writeFile` does before every
   write, so overwriting an existing pak copies the whole thing into an array of
   hundreds of millions of elements purely to discard it. That needs several
   gigabytes and throws `RangeError: Invalid array length`.

   Truncating to zero now just drops the contents. Without this, the first write
   of a large pak succeeds and every later one fails - so a server breaks for
   returning players the moment its assets change, and only clearing browser
   storage recovers it.

    python3 tools/patch-quakejs.py <file.js> [more.js ...]
"""

import pathlib
import sys

# --- 1. installers -----------------------------------------------------------
INSTALLER_MARKER = "installer.paks[s].dest"
INSTALLER_ANCHOR = "var installer = SYSC.installers[i];"
BROKEN_ERROR = ("return callback(new Error('Failed to find \"' + installer.name "
                "+ '\" in manifest'));")
FIXED_ERROR = "continue;   // not offered by this manifest, so nothing to install"
# `var asset;` carries its value across loop iterations - a bare var declaration
# does not reset it - so once an installer can be skipped rather than being fatal,
# the next one inherits the previous asset and unpacks the wrong file at the wrong
# offset. An explicit initialiser clears it each time round.
STALE_ASSET = "var asset;"
FRESH_ASSET = "var asset = undefined;"

INSTALLER_SKIP = """// An installer whose paks the manifest carries outright has
// nothing to do: those files came from somebody's own copy of
// the game and are fetched like any other pak. Unpacking the
// installer over them would restore the demo content, because
// the checksums it validates against are the demo's.
var supplied = installer.paks.length > 0;
for (var s = 0; s < installer.paks.length && supplied; s++) {
\tsupplied = false;
\tfor (var t = 0; t < assets.length; t++) {
\t\tif (assets[t].name === installer.paks[s].dest) {
\t\t\tsupplied = true;
\t\t\tbreak;
\t\t}
\t}
}
if (supplied) {
\tcontinue;
}"""

# --- 2. truncation -----------------------------------------------------------
TRUNCATE_MARKER = "truncating to nothing"
TRUNCATE_OLD = """if (attr.size !== undefined) {
            MEMFS.ensureFlexible(node);
            var contents = node.contents;
            if (attr.size < contents.length) contents.length = attr.size;
            else while (attr.size > contents.length) contents.push(0);
          }"""
TRUNCATE_NEW = """if (attr.size !== undefined) {
            // Fast path for truncating to nothing, which is what writeFile does
            // before every write. Going through ensureFlexible would copy the
            // whole existing file into a plain JS array - one element per byte -
            // only to discard it, and for a pak of a few hundred megabytes that
            // array is several gigabytes and throws "Invalid array length".
            if (attr.size === 0) {
              node.contents = [];
              node.contentMode = MEMFS.CONTENT_FLEXIBLE;
            } else {
              MEMFS.ensureFlexible(node);
              var contents = node.contents;
              if (attr.size < contents.length) contents.length = attr.size;
              else while (attr.size > contents.length) contents.push(0);
            }
          }"""


def read(path):
    # open() rather than Path.read_text(): the newline argument only reached the
    # pathlib wrapper in 3.13, and this runs on the container's 3.10.
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as handle:
        return handle.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
        handle.write(text)


def patch_installers(path, text):
    if INSTALLER_MARKER in text:
        print(f"{path}: installers already patched")
        return text, True
    if text.count(INSTALLER_ANCHOR) != 1:
        print(f"{path}: expected one {INSTALLER_ANCHOR!r}, "
              f"found {text.count(INSTALLER_ANCHOR)}", file=sys.stderr)
        return text, False

    # Match the surrounding indentation so the result stays readable.
    line_start = text.rindex("\n", 0, text.index(INSTALLER_ANCHOR)) + 1
    indent = text[line_start:text.index(INSTALLER_ANCHOR)]
    block = "\n".join(indent + line for line in INSTALLER_SKIP.split("\n"))
    text = text.replace(INSTALLER_ANCHOR, INSTALLER_ANCHOR + "\n\n" + block, 1)

    if text.count(BROKEN_ERROR) == 1:
        text = text.replace(BROKEN_ERROR, FIXED_ERROR, 1)
    else:
        print(f"{path}: note - the broken error path was not found, leaving it")

    if text.count(STALE_ASSET) != 1:
        print(f"{path}: expected one {STALE_ASSET!r}, found {text.count(STALE_ASSET)}", file=sys.stderr)
        return text, False
    text = text.replace(STALE_ASSET, FRESH_ASSET, 1)
    print(f"{path}: installers patched")
    return text, True


def patch_truncate(path, text):
    if TRUNCATE_MARKER in text.lower():
        print(f"{path}: truncation already patched")
        return text, True
    # The vendored files have mixed line endings, so try both rather than
    # assuming the block is written the same way in each of them.
    for ending in (chr(10), chr(13) + chr(10)):
        want = TRUNCATE_OLD.replace(chr(10), ending)
        if text.count(want) == 1:
            text = text.replace(want, TRUNCATE_NEW.replace(chr(10), ending), 1)
            break
    else:
        print(f"{path}: could not find exactly one MEMFS setattr resize block",
              file=sys.stderr)
        return text, False
    print(f"{path}: truncation patched")
    return text, True


def main():
    targets = [pathlib.Path(a) for a in sys.argv[1:]]
    if not targets:
        raise SystemExit(__doc__)
    ok = True
    for target in targets:
        if not target.is_file():
            print(f"{target}: not a file", file=sys.stderr)
            ok = False
            continue
        text = read(target)
        text, good = patch_installers(target, text)
        ok = good and ok
        text, good = patch_truncate(target, text)
        ok = good and ok
        write(target, text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
