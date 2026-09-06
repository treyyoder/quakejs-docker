"""Bans with a reason and, when wanted, an end.

The game keeps its own list (addip, listip) in memory and forgets it when the
server restarts. This keeps the record - address, reason, when, until - in the
state volume, and a sweep keeps the two in step: a ban whose time is up is
lifted, and one the game has forgotten is put back. The address is the whole
identity, as it is for the game; names are self-declared.
"""

import json
import threading
import time

from . import config
from . import game

BANS_FILE = config.STATE.parent / "bans.json"
REASON_MAX = 120
HOURS_MAX = 24 * 365
SWEEP_EVERY = 60.0
_lock = threading.Lock()


def load():
    try:
        data = json.loads(BANS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(bans):
    BANS_FILE.parent.mkdir(parents=True, exist_ok=True)
    scratch = BANS_FILE.with_suffix(".tmp")
    scratch.write_text(json.dumps(bans, indent=2) + "\n")
    scratch.replace(BANS_FILE)


def record(ip, reason="", hours=None, by=None, now=None):
    """Note a ban the game has just been told about. hours None or 0 is for good."""
    now = time.time() if now is None else now
    note = {"reason": reason[:REASON_MAX], "at": int(now), "by": by,
            "expires": int(now + hours * 3600) if hours else None}
    with _lock:
        bans = load()
        bans[ip] = note
        save(bans)
    return note


def forget(ip):
    with _lock:
        bans = load()
        if bans.pop(ip, None) is not None:
            save(bans)


def listing(listed, bans, now=None):
    """Every address banned by either account, with what is known about it."""
    now = time.time() if now is None else now
    out = []
    for ip in sorted(set(listed) | set(bans), key=lambda a: [int(o) for o in a.split(".")]):
        note = bans.get(ip, {})
        expires = note.get("expires")
        out.append({
            "ip": ip,
            "reason": note.get("reason", ""),
            "at": note.get("at"),
            "expires": expires,
            "remaining": max(0, int(expires - now)) if expires else None,
            "enforced": ip in listed,      # the game module has it; the demo one cannot
        })
    return out


def sync(listed=None, now=None, send=None):
    """Lift what has expired, restore what the game forgot; return the listing.

    `listed` is what the game reports (read from it when not given), `send`
    is how a command reaches it; both are parameters so this can be tested
    without a server.
    """
    now = time.time() if now is None else now
    send = game.send_command if send is None else send
    if listed is None:
        listed = game.parse_banlist(game.query_command("listip", want="IP"))
    listed = set(listed)
    with _lock:
        bans = load()
        changed = False
        for ip, note in list(bans.items()):
            expires = note.get("expires")
            if expires and expires <= now:
                if ip in listed:
                    send("removeip " + ip)
                    listed.discard(ip)
                del bans[ip]
                changed = True
                print(f"[bans] {ip} lifted; its {note.get('reason') or 'ban'} ran out", flush=True)
            elif ip not in listed:
                # The game server restarted and started with an empty list.
                send("addip " + ip)
                listed.add(ip)
        if changed:
            save(bans)
        return listing(listed, bans, now)


def sweep():
    """Keep the game's list and the record in step, forever."""
    while True:
        try:
            sync()
        except Exception as exc:
            print(f"[bans] {type(exc).__name__}: {exc}", flush=True)
        time.sleep(SWEEP_EVERY)
