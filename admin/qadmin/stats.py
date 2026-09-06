"""The engine's own game log: the leaderboard, arrivals, and the live roster.

Far better structured than the console output: every kill is named by client
slot, every client's userinfo arrives verbatim, and a bot gives itself away
with a skill field in it. Following it yields a leaderboard that survives
restarts, shows when a real player arrives on an empty server, and knows
every slot's exact name - which is what `status` cannot be trusted for.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

from . import config
from . import game
from .follow import Follower

# A map change disconnects and reconnects everybody, so a name returning within
# this window is the same session rather than somebody arriving.
REJOIN_GRACE = 90.0
WORLD_SLOT = 1022
BACKSLASH = chr(92)
# Names nobody has played under in this long are dropped, and the table is
# capped by most recently seen, so a name-cycling visitor cannot grow the file
# without limit. Bots are seen on every map change and survive both.
STATS_MAX_AGE = 90 * 86400
STATS_MAX_PLAYERS = 500
SAVE_EVERY = 10.0

# InitGame: \sv_hostname\quakejs\mapname\q3dm7\... - the engine's own record of
# which map is up, so a crash note can name it without asking anyone.
MAPNAME_RE = re.compile(r"InitGame:.*?" + BACKSLASH * 2 + "mapname" + BACKSLASH * 2
                        + "([^" + BACKSLASH * 2 + "]+)")
USERINFO_RE = re.compile(r"ClientUserinfoChanged:\s+(\d+)\s+(.*)")
KILL_RE = re.compile(r"Kill:\s+(\d+)\s+(\d+)\s+(\d+):")
BEGIN_RE = re.compile(r"ClientBegin:\s+(\d+)")
DISCONNECT_RE = re.compile(r"ClientDisconnect:\s+(\d+)")
SCORE_RE = re.compile(r"score:\s+(-?\d+)\s+ping:\s+(-?\d+)\s+client:\s+(\d+)")

_lock = threading.Lock()
_stats = {"players": {}, "offset": 0, "fingerprint": ""}
_slots = {}      # client slot -> {"name", "bot"}
_present = {}    # human name -> when they joined
_recent = {}     # human name -> when they dropped, for the rejoin grace
_last_notify = [0.0]
_current = {"map": None}   # from the last InitGame line


# ------------------------------------------------------------ persistence
def load_stats():
    try:
        data = json.loads(config.STATS_FILE.read_text())
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and isinstance(data.get("players"), dict):
        _stats["players"] = data["players"]
        _stats["offset"] = int(data.get("offset") or 0)
        _stats["fingerprint"] = str(data.get("fingerprint") or "")


def prune_stats():
    now = time.time()
    players = _stats["players"]
    for name in [n for n, r in players.items() if now - r.get("seen", 0) > STATS_MAX_AGE]:
        del players[name]
    excess = len(players) - STATS_MAX_PLAYERS
    if excess > 0:
        oldest = sorted(players.items(), key=lambda kv: kv[1].get("seen", 0))[:excess]
        for name, _ in oldest:
            del players[name]


def save_stats():
    prune_stats()
    config.STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    scratch = config.STATS_FILE.with_suffix(".tmp")
    scratch.write_text(json.dumps(_stats, indent=2) + "\n")
    scratch.replace(config.STATS_FILE)   # never leave a half-written file behind


def record(name, **deltas):
    if name not in _stats["players"]:
        # A row born of a kill line rather than a userinfo line - after the
        # leaderboard was replaced by an import, say, with bots already in -
        # must still know what the slots know, or a bot lands on the board
        # until its next map change.
        known_bot = any(who["name"] == name and who["bot"] for who in _slots.values())
        _stats["players"][name] = {"kills": 0, "deaths": 0, "suicides": 0, "matches": 0,
                                   "best": 0, "bot": known_bot, "seen": int(time.time())}
    row = _stats["players"][name]
    for key, value in deltas.items():
        if key == "best":
            row["best"] = max(row.get("best", 0), value)
        elif key in ("bot", "seen"):
            row[key] = value
        else:
            row[key] = row.get(key, 0) + value


# ---------------------------------------------------------------- arrivals
def webhook_payload(url, text):
    """The body a webhook wants. Discord and Slack disagree on the field name,
    so pick by host - and neither may turn a player's name into a mention:
    "@everyone" is a legal Quake name, and a webhook posts as the server.
    """
    if "slack.com" in url:
        # Slack mentions are markup, <!everyone> and the like; escaping the
        # three characters it reserves leaves them as text.
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return {"text": safe}
    return {"content": text, "allowed_mentions": {"parse": []}}


def post_webhook(text):
    """Announce something out of band. Best effort; never raises at the caller."""
    payload = webhook_payload(config.NOTIFY_WEBHOOK, text)
    request = urllib.request.Request(
        config.NOTIFY_WEBHOOK, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": config.UA})
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[notify] {type(exc).__name__}: {exc}", flush=True)


def announce_arrival(name):
    """Post when a real player turns up on a server that had nobody on it."""
    now = time.time()
    for gone, when in list(_recent.items()):
        if now - when > REJOIN_GRACE:
            _recent.pop(gone, None)
    if name in _present:
        return
    was_empty = not _present
    _present[name] = now
    if _recent.pop(name, None) is not None:
        return       # the same session returning after a map change
    if not (was_empty and config.NOTIFY_WEBHOOK):
        return
    if now - _last_notify[0] < config.NOTIFY_COOLDOWN:
        return
    _last_notify[0] = now
    # Resolving the hostname talks to the game server, so keep it off this
    # thread: the tailer must not stall behind a console round trip.
    threading.Thread(target=lambda: post_webhook(
        f"{name} is playing on {game.cached_hostname() or 'the QuakeJS server'}."),
        daemon=True).start()


# ------------------------------------------------------------------ parsing
def handle_game_line(line, live=True):
    """Apply one game log line. Caller holds the lock."""
    started = MAPNAME_RE.search(line)
    if started:
        _current["map"] = started.group(1)
        return

    info = USERINFO_RE.search(line)
    if info:
        fields = info.group(2).split(BACKSLASH)
        pairs = dict(zip(fields[0::2], fields[1::2]))
        name = game.strip_colors(pairs.get("n", "")).strip()
        if not name:
            return
        # Only bots carry a skill in their userinfo, which is the one dependable
        # way to tell them apart from a player who took a bot name.
        is_bot = "skill" in pairs
        _slots[int(info.group(1))] = {"name": name, "bot": is_bot}
        record(name, bot=is_bot, seen=int(time.time()))
        return

    begin = BEGIN_RE.search(line)
    if begin:
        who = _slots.get(int(begin.group(1)))
        if live and who and not who["bot"]:
            announce_arrival(who["name"])
        return

    gone = DISCONNECT_RE.search(line)
    if gone:
        who = _slots.pop(int(gone.group(1)), None)
        if who and not who["bot"]:
            _present.pop(who["name"], None)
            _recent[who["name"]] = time.time()
        return

    kill = KILL_RE.search(line)
    if kill:
        killer, victim = int(kill.group(1)), int(kill.group(2))
        victim_name = (_slots.get(victim) or {}).get("name")
        killer_name = (_slots.get(killer) or {}).get("name")
        if victim_name:
            record(victim_name, deaths=1)
            if killer == victim:
                record(victim_name, suicides=1)
        if killer not in (victim, WORLD_SLOT) and killer_name:
            record(killer_name, kills=1)
        return

    score = SCORE_RE.search(line)
    if score:
        who = _slots.get(int(score.group(3)))
        if who:
            record(who["name"], matches=1, best=int(score.group(1)))
        return

    if "ShutdownGame:" in line:
        # Everyone reconnects on a map change, so remember who was already here
        # or the entire server looks like it has just turned up.
        now = time.time()
        for name in _present:
            _recent[name] = now
        _present.clear()
        _slots.clear()


def tail_game_log():
    """Follow the game log the engine writes, for the leaderboard and arrivals."""
    with _lock:
        load_stats()
        follower = Follower(config.GAME_LOG, _stats["offset"], _stats["fingerprint"])
        # A first read of a log we have never seen is history, not news.
        state = {"live": bool(_stats["fingerprint"]), "dirty": False, "saved_at": 0.0}

    def on_lines(lines):
        with _lock:
            for line in lines:
                handle_game_line(line, state["live"])
            state["dirty"] = True

    def on_tick():
        state["live"] = True
        now = time.time()
        if state["dirty"] and now - state["saved_at"] > SAVE_EVERY:
            with _lock:
                _stats["offset"], _stats["fingerprint"] = follower.offset, follower.fingerprint
                save_stats()
            state["dirty"], state["saved_at"] = False, now

    follower.run(on_lines, "stats", interval=1.0, on_tick=on_tick)


# ------------------------------------------------------------------ readers
def leaderboard(limit=25):
    """Ranked players. Bots are tracked but never shown - nobody competes with them."""
    with _lock:
        rows = [dict(row, name=name) for name, row in _stats["players"].items()]
    for row in rows:
        deaths = row.get("deaths", 0)
        kills = row.get("kills", 0)
        row["ratio"] = round(kills / deaths, 2) if deaths else float(kills)
    rows.sort(key=lambda r: (r.get("kills", 0), r.get("best", 0)), reverse=True)
    return {"players": [r for r in rows if not r.get("bot")][:limit]}


def snapshot():
    """The leaderboard as stored, for a backup."""
    with _lock:
        return {"players": json.loads(json.dumps(_stats["players"]))}


def replace_players(players):
    """The leaderboard from a backup, in place of the current one. The log
    offset stays: the rows came from another log, this one goes on."""
    with _lock:
        _stats["players"] = dict(players)
        save_stats()


def slots_snapshot():
    """slot -> {"name", "bot"} as the game log last had it."""
    with _lock:
        return {num: dict(who) for num, who in _slots.items()}


def current_map():
    """The map the last InitGame line named, or None before the first."""
    with _lock:
        return _current["map"]


def bot_status(name):
    """True or False for a name the game log has seen, None for one it has not."""
    with _lock:
        known = _stats["players"].get(name)
    return None if known is None else bool(known.get("bot"))


def annotate(players):
    """Overlay what the game log knows onto a `status` roster, slot by slot.

    `status` pads and truncates its name column, and reading it back is a
    regex over free text; the log carries each client's userinfo verbatim and
    marks the bots. So where the log knows a slot, its name and bot flag win.
    It does not know addresses, ping or score, which is why `status` is still
    asked at all. Between a map change and the reconnects that follow it the
    log knows nothing, and the status names stand.
    """
    with _lock:
        slots = {num: dict(who) for num, who in _slots.items()}
    for player in players:
        known = slots.get(player["num"])
        if known and known["name"]:
            player["name"] = known["name"]
            player["bot"] = known["bot"] or player["bot"]
    return players
