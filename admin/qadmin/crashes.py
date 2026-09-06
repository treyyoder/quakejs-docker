"""What the game server said before it died, kept for the next look.

The engine prints "Server crashed: <why>" on its way down, the watchdog
restarts it, and the console log is emptied on that restart - so the lines
that explain a crash are gone within seconds unless somebody keeps them. The
chat tailer sees every line of that log; this keeps the last two hundred of
them with the map and the bot count at the time, twenty crashes deep, in the
state volume beside everything else.
"""

import json
import re
import time

from . import config

CRASH_DIR = config.STATE.parent / "crashes"
KEEP = 20
TAIL = 200
CRASH_RE = re.compile(r"Server crashed: (.*)")


def is_crash(line):
    """The reason, when a console-log line reports a crash; otherwise None."""
    match = CRASH_RE.search(line)
    return match.group(1).strip() if match else None


def record(reason, tail, map_name=None, bots=None, now=None):
    now = time.time() if now is None else now
    CRASH_DIR.mkdir(parents=True, exist_ok=True)
    path = CRASH_DIR / time.strftime("crash-%Y%m%d-%H%M%S.json", time.gmtime(now))
    entry = {"at": int(now), "reason": reason, "map": map_name, "bots": bots,
             "tail": list(tail)[-TAIL:]}
    path.write_text(json.dumps(entry, indent=2) + "\n")
    for stale in sorted(CRASH_DIR.glob("crash-*.json"))[:-KEEP]:
        stale.unlink()
    print(f"[crash] {reason} on {map_name or '?'} with {bots if bots is not None else '?'} bots;"
          f" {len(entry['tail'])} lines kept", flush=True)
    return path


def recent(limit=KEEP):
    """Newest first."""
    out = []
    if not CRASH_DIR.is_dir():
        return out
    for path in sorted(CRASH_DIR.glob("crash-*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return out
