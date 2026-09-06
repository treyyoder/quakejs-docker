"""The message stream, public chat and its throttle.

The console can only see half of a conversation from the log: player chat is
logged, but say/tell issued from the server console are not echoed. So sent
messages are recorded here as they go out, and received ones are tailed from
the log, giving one ordered stream the messenger view can thread.
"""

import re
import threading
import time

from . import assets
from . import config
from . import game
from . import stats
from .follow import Follower
from .settings import UNSAFE_CHARS

MESSAGE_LIMIT = 500
SAY_RE = re.compile(r"^say:\s+(.+?):\s?(.*)$")
SAYTEAM_RE = re.compile(r"^sayteam:\s+(.+?):\s?(.*)$")
TELL_RE = re.compile(r"^tell:\s+(.+?)\s+to\s+(.+?):\s?(.*)$")
# What an anonymous reader of the stream may see. Chat on a game server is
# public by nature; private messages are not, and names are self-declared, so
# handing everyone the whole stream let anyone read anyone's PMs by asking.
PUBLIC_KINDS = frozenset({"say", "sent"})

_messages = []
_message_seq = [0]
_msg_lock = threading.Lock()


# ------------------------------------------------------------------ stream
def add_message(kind, sender, target, text, token=None):
    with _msg_lock:
        _message_seq[0] += 1
        _messages.append({
            "seq": _message_seq[0],
            "at": time.time(),
            "kind": kind,          # say | team | tell | sent | sent-pm
            "from": game.strip_colors(sender),
            "to": game.strip_colors(target) if target else None,
            "text": game.strip_colors(text),
            "token": token,        # the browser that sent it, for sent-pm only
        })
        if len(_messages) > MESSAGE_LIMIT:
            del _messages[:len(_messages) - MESSAGE_LIMIT]


def messages_since(seq, token=None, admin=False):
    """Messages after seq, less whatever this caller has no business seeing.

    An admin sees everything, as they already can in the raw log. Anyone else
    sees public chat plus the private messages their own browser sent - never
    in-game tells or team chat, which pass between players who did not agree
    to publish them.
    """
    with _msg_lock:
        out = []
        for m in _messages:
            if m["seq"] <= seq:
                continue
            if not admin:
                if m["kind"] == "sent-pm":
                    if not token or m.get("token") != token:
                        continue
                elif m["kind"] not in PUBLIC_KINDS:
                    continue
            out.append({k: v for k, v in m.items() if k != "token"})
        return out


def is_bot_name(name):
    """Whether a speaker is a bot rather than a player.

    The game log records which clients are bots, so prefer that; a name nobody
    has been seen under yet falls back to the roster the paks define. Bot
    banter is relentless and drowns the messenger, so it never reaches it.
    """
    clean = game.strip_colors(name).strip()
    known = stats.bot_status(clean)
    if known is not None:
        return known
    return any(b.lower() == clean.lower() for b in assets.available_bots())


def parse_chat_line(line):
    """A chat event from a server log line, or None."""
    line = line.rstrip("\r\n")
    match = TELL_RE.match(line)
    if match:
        return ("tell", match.group(1), match.group(2), match.group(3))
    match = SAY_RE.match(line)
    if match:
        return ("say", match.group(1), None, match.group(2))
    match = SAYTEAM_RE.match(line)
    if match:
        return ("team", match.group(1), None, match.group(2))
    return None


def tail_chat():
    """Follow the server log: record player chat, and keep what a crash said."""
    from . import crashes

    def on_lines(lines):
        for line in lines:
            event = parse_chat_line(line)
            if event and not is_bot_name(event[1]):
                add_message(*event)
            reason = crashes.is_crash(line)
            if reason:
                # The watchdog restarts the server and the restart empties this
                # log within seconds; the lines are only here now.
                with _chat_lock:
                    cached = _public_cache["data"].get("map")
                slots = stats.slots_snapshot()
                crashes.record(reason, game.log_tail(crashes.TAIL).splitlines(),
                               stats.current_map() or cached,
                               sum(1 for who in slots.values() if who["bot"]))

    Follower(config.LOG).run(on_lines, "chat", interval=0.7)


# ------------------------------------------------------------- public chat
# These endpoints are reachable WITHOUT signing in, so everything they accept is
# sanitised the same way admin input is, and throttled per browser and globally.
# A guest name is self-declared: it is a label, never an identity.
GUEST_COOKIE = "qguest"
GUEST_BURST = 5          # messages per window from one browser
GUEST_WINDOW = 10.0      # seconds
GLOBAL_BURST = 30        # messages per window from everyone combined
NAME_MAX = 31
MESSAGE_MAX = 140
PUBLIC_TTL = 3.0

_chat_lock = threading.Lock()
_public_cache = {"at": 0.0, "data": {}}


class Throttle:
    """A sliding-window burst limit, keyed by whatever the caller chooses:
    a browser's guest token, a client address, or one shared key for
    everyone at once."""

    def __init__(self, burst, window):
        self.burst, self.window = burst, window
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.burst:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 1000:   # forget keys that have gone quiet
                for stale in [k for k, v in self._hits.items() if now - v[-1] > self.window]:
                    del self._hits[stale]
            return True


# Chat: a browser, an address, and everyone. Per address matters even though
# every game client shares one behind the websocket proxy, because console
# requests come through Apache with the real address in X-Forwarded-For.
CHAT_PER_BROWSER = Throttle(GUEST_BURST, GUEST_WINDOW)
CHAT_PER_ADDRESS = Throttle(GUEST_BURST * 3, GUEST_WINDOW)
CHAT_GLOBAL = Throttle(GLOBAL_BURST, GUEST_WINDOW)
# The public reads - roster, messages, leaderboard - are cached and cheap,
# but not free, and nothing else bounded how often one address could ask. A
# chat page polls about seven times in ten seconds; this leaves room for a
# few tabs and refuses a loop.
READS_PER_ADDRESS = Throttle(120, 10.0)
READS_GLOBAL = Throttle(3000, 10.0)


def reads_allowed(address, now=None):
    return READS_GLOBAL.allow("*", now) and READS_PER_ADDRESS.allow(address, now)


def public_state():
    """Map, hostname and roster for anyone. Refreshed at most every few seconds,
    because a refresh writes a status command to the game console - which is
    also why anything else needing the roster reads this rather than asking
    the server again.
    """
    now = time.time()
    with _chat_lock:
        if _public_cache["at"] > now - PUBLIC_TTL:
            return _public_cache["data"]
    current, players = game.parse_status(game.query_status())
    stats.annotate(players)
    payload = {
        "map": current,
        "hostname": game.cached_hostname(),
        "players": [{"num": p["num"], "name": p["name"], "bot": p["bot"]}
                    for p in players],
    }
    with _chat_lock:
        _public_cache.update(at=now, data=payload)
    return payload


def clean_chat(value, limit):
    text = "".join(c for c in str(value) if c not in UNSAFE_CHARS)
    return " ".join(text.split())[:limit].strip()


def chat_allowed(token, now=None, address=None):
    """Whether one more message may go out, and why not if not."""
    if not CHAT_GLOBAL.allow("*", now):
        return False, "the server is busy; try again in a moment"
    if address is not None and not CHAT_PER_ADDRESS.allow(address, now):
        return False, "too many messages from your address; slow down"
    if not CHAT_PER_BROWSER.allow(token, now):
        return False, "you are sending messages too quickly"
    return True, None
