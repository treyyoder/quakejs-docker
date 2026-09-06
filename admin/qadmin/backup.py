"""Export and import of everything in the state volume, from the console.

The bundle is one JSON document: settings, rotation, the leaderboard and the
stored credentials. Importing writes the files the same way the console
would have, so everything is validated on the way in: settings through the
same spec the Match tab uses, map names by pattern, leaderboard rows field by
field, credentials by shape. Settings and rotation take effect on the next
server restart, when build-config.py folds them into server.cfg; the
leaderboard takes effect at once.
"""

import json
import re
import time

from . import auth
from . import config
from . import settings
from . import stats

FORMAT = "quakejs-state/1"
HEX_RE = re.compile(r"^[0-9a-f]+$")
ROW_FIELDS = ("kills", "deaths", "suicides", "matches", "best", "seen")
NAME_MAX = 64
# The same bundle, written into the state volume by itself: once a day, the
# newest seven kept, so a mistake is a download away rather than a memory of
# having meant to press Export.
BACKUP_DIR = config.STATE.parent / "backups"
BACKUP_KEEP = 7
BACKUP_EVERY = 86400.0
BACKUP_CHECK = 3600.0
BACKUP_NAME_RE = re.compile(r"^state-\d{8}-\d{6}\.json$")


def write_backup(now=None):
    now = time.time() if now is None else now
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / time.strftime("state-%Y%m%d-%H%M%S.json", time.gmtime(now))
    path.write_text(json.dumps(export_state(), indent=2) + "\n")
    for stale in sorted(BACKUP_DIR.glob("state-*.json"))[:-BACKUP_KEEP]:
        stale.unlink()
    print(f"[backup] wrote {path.name}", flush=True)
    return path


def list_backups():
    """Newest first: name, size, when."""
    if not BACKUP_DIR.is_dir():
        return []
    out = []
    for path in sorted(BACKUP_DIR.glob("state-*.json"), reverse=True):
        if BACKUP_NAME_RE.match(path.name):
            out.append({"name": path.name, "bytes": path.stat().st_size,
                        "at": int(path.stat().st_mtime)})
    return out


def backup_due(now=None):
    now = time.time() if now is None else now
    newest = list_backups()
    return not newest or now - newest[0]["at"] >= BACKUP_EVERY


def backup_thread():
    """A backup a day, checked hourly, the first one at start."""
    while True:
        try:
            if backup_due():
                write_backup()
        except Exception as exc:
            print(f"[backup] {type(exc).__name__}: {exc}", flush=True)
        time.sleep(BACKUP_CHECK)


def export_state():
    return {
        "format": FORMAT,
        "exported": int(time.time()),
        "settings": settings.saved_settings(),
        "rotation": settings.saved_rotation(),
        "stats": stats.snapshot(),
        "credentials": auth.stored_credentials(),
    }


def clean_rows(players):
    """Leaderboard rows as the stats module would have written them."""
    out = {}
    for name, row in players.items():
        if not (isinstance(name, str) and name.strip() and len(name) <= NAME_MAX
                and isinstance(row, dict)):
            continue
        clean = {}
        for field in ROW_FIELDS:
            try:
                clean[field] = max(0, int(row.get(field, 0)))
            except (TypeError, ValueError):
                clean[field] = 0
        clean["bot"] = bool(row.get("bot"))
        out[name] = clean
    return out


def valid_credentials(record):
    return (isinstance(record, dict)
            and isinstance(record.get("salt"), str) and HEX_RE.match(record["salt"])
            and isinstance(record.get("hash"), str) and HEX_RE.match(record["hash"])
            and isinstance(record.get("iterations"), int) and record["iterations"] > 0)


def import_state(bundle):
    """Apply a bundle. Returns (what was applied, whether sessions were ended)."""
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
        raise ValueError(f"not a {FORMAT} bundle")
    applied = {}

    if isinstance(bundle.get("settings"), dict):
        clean = {}
        for name, value in bundle["settings"].items():
            try:
                clean[name] = settings.coerce_setting(name, value)
            except ValueError:
                continue    # not a console setting, or out of range
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SETTINGS_FILE.write_text(json.dumps(clean, indent=2) + "\n")
        applied["settings"] = len(clean)

    if isinstance(bundle.get("rotation"), list):
        maps = [m for m in bundle["rotation"] if isinstance(m, str) and config.MAP_RE.match(m)]
        settings.save_rotation(maps)
        applied["rotation"] = len(maps)

    if isinstance(bundle.get("stats"), dict) and isinstance(bundle["stats"].get("players"), dict):
        rows = clean_rows(bundle["stats"]["players"])
        stats.replace_players(rows)
        applied["players"] = len(rows)

    reauth = False
    if bundle.get("credentials") is not None:
        record = bundle["credentials"]
        if not valid_credentials(record):
            raise ValueError("credentials in the bundle are not a salt, hash and iteration count")
        reauth = auth.store_credentials(
            {"salt": record["salt"], "hash": record["hash"], "iterations": record["iterations"]})
        applied["credentials"] = True

    return applied, reauth
