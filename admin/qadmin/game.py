"""The game server itself: its console FIFO, its log, and what `status` says.

Everything written to the FIFO is executed as a server console command, so
the callers here validate every value against a whitelist; this module only
refuses what could never be one command.
"""

import os
import pathlib
import re
import signal
import threading
import time

from . import config

_write_lock = threading.Lock()
# Console reads are a send followed by a read of whatever the log grew by, so
# two of them at once each see the other's output: a status poll overlapping a
# manual refresh would list every player twice. One at a time.
_console_lock = threading.Lock()

COLOR_RE = re.compile(r"\^.")
# One row of `status`: num score ping name lastmsg address qport rate. The
# name is printed raw and padded, so it may hold any run of spaces; anchoring
# on the four fixed fields after it is what keeps "Sarge  Jr" or a long-idle
# lastmsg from breaking the row. A connecting client shows CNCT for its ping
# and has no name yet; those rows are skipped.
STATUS_ROW_RE = re.compile(
    r"^(\d+)\s+(-?\d+)\s+(\d+)\s+(.*?)\s+(\d+)\s+(\S+)\s+(\d+)\s+(\d+)$")
MAP_LINE_RE = re.compile(r"^map:\s*(\S+)")
BAN_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def strip_colors(text):
    return COLOR_RE.sub("", str(text)).strip()


# ----------------------------------------------------------------- commands
def send_command(command):
    """Write one console command to the game server. Caller must validate it."""
    if "\n" in command or "\r" in command:
        raise ValueError("command must be a single line")
    with _write_lock:
        with open(config.FIFO, "w") as fifo:
            fifo.write(command + "\n")
            fifo.flush()


def query_command(command, timeout=2.5, want=None):
    """Run a console command and return the output it appends to the log.

    With `want`, keep reading until that text shows up or time runs out; the
    longer outputs (cvarlist) arrive in pieces.
    """
    log = config.LOG
    with _console_lock:
        start = log.stat().st_size if log.exists() else 0
        send_command(command)
        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            time.sleep(0.2)
            if not log.exists():
                continue
            size = log.stat().st_size
            if size < start:      # truncated by a server restart
                start = 0
            if size > start:
                time.sleep(0.3)
                with log.open("rb") as handle:
                    handle.seek(start)
                    text = handle.read().decode("latin-1")
                if want is None or want in text:
                    break
        return text


def query_status(timeout=2.0):
    """Run `status` and return the fresh output it appends to the log."""
    return query_command("status", timeout=timeout)


def restart_game(timeout=10.0):
    """Signal the game server so the entrypoint's supervisor starts a fresh one.

    The console "quit" command is not usable here: it shuts the game down but
    leaves the emscripten runtime alive, so the process never exits and the
    supervisor never restarts it, leaving a container that looks healthy with no
    server running.
    """
    pids = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("latin-1")
        except OSError:
            continue
        if "ioq3ded.js" in cmdline:
            pids.append(int(entry.name))
    if not pids:
        raise RuntimeError("no running game server found")

    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    for pid in pids:
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            os.kill(pid, signal.SIGKILL)
    return pids


def log_tail(limit=200):
    if not config.LOG.exists():
        return ""
    data = config.LOG.read_bytes()[-200_000:]
    return "\n".join(data.decode("latin-1").splitlines()[-limit:])


# ------------------------------------------------------------------ parsing
def parse_status(text):
    """(current map, players) from `status` output.

    Names here are what the engine printed, colour codes stripped; the game
    log knows them exactly, and stats.annotate overlays that where it can.
    """
    current, players = None, []
    for line in text.splitlines():
        line = line.strip()
        match = MAP_LINE_RE.match(line)
        if match:
            current = match.group(1)
            continue
        row = STATUS_ROW_RE.match(line)
        if not row:
            continue
        peer = row.group(6)
        is_bot = peer.startswith("bot")
        # The address column carries a port; bans match on the address alone,
        # and rsplit keeps this right if the engine ever prints a bracketed
        # IPv6 literal here.
        address = None if is_bot else peer.rsplit(":", 1)[0].strip("[]")
        players.append({
            "num": int(row.group(1)),
            "score": int(row.group(2)),
            "ping": int(row.group(3)),
            "name": strip_colors(row.group(4)),
            "bot": is_bot,
            "address": address,
        })
    # One status output per client. Two of them can land in the window this
    # reads - a poll and a manual refresh overlapping - and every player would
    # then appear twice, flickering in and out as the reads interleave.
    latest = {}
    for player in players:
        latest[player["num"]] = player
    return current, sorted(latest.values(), key=lambda p: p["num"])


def parse_banlist(text):
    return sorted(set(BAN_RE.findall(text)))


# --------------------------------------------------------------- serverinfo
SERVERINFO_TTL = 60.0
SERVERINFO_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s+(.*)$")
_serverinfo_cache = {"at": 0.0, "value": {}}
_serverinfo_lock = threading.Lock()


def parse_serverinfo(text):
    """key -> value from `serverinfo` output: one setting per line, the key
    padded to a column and the value everything after. Anything else that
    landed in the same stretch of log - chat, say - has no such key."""
    found = {}
    for line in text.splitlines():
        if line.startswith("Server info"):
            continue
        match = SERVERINFO_KEY_RE.match(line.rstrip())
        if match:
            found[match.group(1)] = match.group(2).strip()
    return found


def serverinfo():
    """The server's serverinfo settings, refreshed at most once a minute: each
    refresh is a console round trip, and nothing read from it changes without
    a restart. A read that came back empty is not cached, so it is retried."""
    now = time.time()
    with _serverinfo_lock:
        if _serverinfo_cache["at"] > now - SERVERINFO_TTL and _serverinfo_cache["value"]:
            return _serverinfo_cache["value"]
    found = parse_serverinfo(query_command("serverinfo", timeout=2.0, want="sv_maxclients"))
    with _serverinfo_lock:
        if found:
            _serverinfo_cache.update(at=now, value=found)
    return found


def cached_hostname():
    """sv_hostname without dumping the whole cvar list into the log each poll."""
    return strip_colors(serverinfo().get("sv_hostname", ""))


def bot_room(players, info=None):
    """How many bots may be added right now, and what bounds it.

    Two limits: free client slots - sv_maxclients less the reserved ones and
    everyone connected - and the game module's memory, config.BOT_CEILING
    bots in total whatever the slots say. With the server not answering
    (maxclients unknown), the slots are not held against the caller.
    """
    info = serverinfo() if info is None else info

    def number(key):
        try:
            return int(info.get(key, 0))
        except (TypeError, ValueError):
            return 0

    maxclients = number("sv_maxclients")
    bots = sum(1 for p in players if p.get("bot"))
    pool = max(0, config.BOT_CEILING - bots)
    slots = max(0, maxclients - number("sv_privateClients") - len(players)) if maxclients else pool
    return {"room": min(slots, pool), "slots": slots, "pool": pool, "bots": bots,
            "maxclients": maxclients, "ceiling": config.BOT_CEILING}
