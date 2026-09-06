#!/usr/bin/env python3
"""Build the effective server.cfg from the image default plus console state.

The image ships the base config. Anything changed from the admin console is
stored as JSON in the state directory and folded in here at startup, so settings
and the map rotation survive a restart without the console having to rewrite a
config file it might corrupt.

    python3 build-config.py <base.cfg> <state-dir> <out.cfg>

What may be folded in, and how each value is checked, comes from the console's
own settings spec - the same one it validates against before writing to the
game console - so the two cannot drift. They did once: g_allowVote applied
live and then silently reverted on every restart, because a hand-kept copy of
the list here had never heard of it.
"""

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from qadmin.config import MAP_RE            # noqa: E402
from qadmin.settings import coerce_setting  # noqa: E402


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def render(text, settings, rotation):
    """The config text with the console's settings and rotation folded in."""
    lines = []
    for key, value in sorted(settings.items()):
        try:
            clean = coerce_setting(key, value)
        except ValueError:
            continue    # not a console setting, or out of its range: never written blindly
        lines.append('seta %s "%s"' % (key, clean))

    # The base config ends by starting the first map. Everything the console sets
    # has to land before that, or latched cvars such as sv_maxclients only take
    # effect one map later, so the starter is stripped and re-added at the end.
    text = re.sub(r"^\s*vstr d1\s*$", "", text, flags=re.M)

    clean = [m for m in rotation if MAP_RE.fullmatch(str(m))]
    if clean:
        text = re.sub(r"^\s*set d\d+ .*$", "", text, flags=re.M)  # replace shipped chain
        for index, name in enumerate(clean, start=1):
            nxt = 1 if index == len(clean) else index + 1
            lines.append('set d%d "map %s ; set nextmap vstr d%d"' % (index, name, nxt))

    body = text.rstrip("\n")
    if lines:
        body += "\n\n// --- set from the admin console ---\n" + "\n".join(lines)
    body += "\nvstr d1\n"
    return body, len(clean)


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    base, state, out = (pathlib.Path(a) for a in sys.argv[1:4])
    settings = load(state / "settings.json") or {}
    rotation = load(state / "rotation.json") or []
    body, rotated = render(base.read_text(), settings, rotation)
    out.write_text(body)
    print("[config] %d settings, %d rotation entries" % (len(settings), rotated), flush=True)


if __name__ == "__main__":
    main()
