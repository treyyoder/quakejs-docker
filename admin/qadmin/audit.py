"""Who did what, when: an append-only record of every admin action.

There is one admin account, so "who" is the address a request came from -
the real one, read through the proxy the same way the lockout keys on it -
plus the action and what it was applied to. Passwords never appear; a secret
setting appears by name only. The file lives with the other state, capped by
keeping the newer half whenever it grows past a megabyte, and every entry is
also printed to the container log so it is in two places.
"""

import json
import threading
import time

from . import config
from . import settings

AUDIT_FILE = config.STATE.parent / "audit.jsonl"
AUDIT_MAX_BYTES = 1024 * 1024
# Body fields that are credentials and are never recorded, whatever the route.
REDACT = frozenset({"password", "current", "new"})
# What each route's record carries. Anything not named here is dropped, so a
# new field is opt-in rather than logged by accident.
FIELDS = {
    "/api/settings": ("settings",),
    "/api/say": ("message",),
    "/api/team": ("num", "team"),
    "/api/kick": ("num",),
    "/api/ban": ("ip", "reason", "hours"),
    "/api/unban": ("ip",),
    "/api/bot": ("name", "skill", "count"),
    "/api/map": ("map",),
    "/api/rotation": ("maps",),
    "/api/uninstall": ("map",),
    "/api/install": ("ref", "force"),
    "/api/lookup": ("ref",),
    "/api/import": ("restart",),
}
_lock = threading.Lock()


def detail(route, payload=None, query=None):
    """The part of a request worth keeping, with secrets left out."""
    payload = payload if isinstance(payload, dict) else {}
    kept = {}
    for name in FIELDS.get(route, ()):
        if name not in payload or name in REDACT:
            continue
        value = payload[name]
        if name == "settings" and isinstance(value, dict):
            value = {k: ("(secret)" if settings.is_secret(k) else v) for k, v in value.items()}
        kept[name] = value
    if route == "/api/upload" and query:
        kept["name"] = (query.get("name") or [""])[0]
    return kept


def record(actor, action, what=None):
    entry = {"at": int(time.time()), "actor": actor, "action": action, "detail": what or {}}
    line = json.dumps(entry, separators=(",", ":"))
    print(f"[audit] {actor} {action} {json.dumps(entry['detail'], separators=(',', ':'))}",
          flush=True)
    with _lock:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        try:
            if AUDIT_FILE.stat().st_size > AUDIT_MAX_BYTES:
                _trim()
        except OSError:
            pass
    return entry


def _trim():
    """Keep the newer half. Caller holds the lock."""
    lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    keep = lines[len(lines) // 2:]
    scratch = AUDIT_FILE.with_suffix(".tmp")
    scratch.write_text("\n".join(keep) + "\n", encoding="utf-8")
    scratch.replace(AUDIT_FILE)


def recent(limit=200):
    """The newest entries, newest last."""
    with _lock:
        try:
            lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue    # a line cut short by a crash; the rest still read
    return out
