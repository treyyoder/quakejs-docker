"""The settings the console manages, and the saved settings and rotation.

SETTINGS is the one description of what may be set: the console's Match tab
renders it, every value is checked against it before reaching the game
console, and build-config.py imports it to fold the saved values back into
server.cfg at start. Each value is range- or pattern-checked rather than
escaped, because everything here ends up as a server console command.
"""

import json
import re

from . import config
from . import game

GAMETYPES = {0: "Free for all", 1: "Tournament", 2: "Single player",
             3: "Team deathmatch", 4: "Capture the flag"}

SETTINGS = {
    "sv_hostname":     {"kind": "text", "max": 63,  "label": "Server name"},
    "g_motd":          {"kind": "text", "max": 127, "label": "Message of the day"},
    "g_gametype":      {"kind": "int", "min": 0, "max": 4, "reload": True, "label": "Game type"},
    "timelimit":       {"kind": "int", "min": 0, "max": 120, "label": "Time limit (min)"},
    # Snapshot rate. At 20 the server only updates clients every 50ms, which
    # shows up as ~50ms of ping even on a local server.
    "sv_fps":          {"kind": "int", "min": 10, "max": 60, "label": "Tick rate (fps)"},
    "fraglimit":       {"kind": "int", "min": 0, "max": 200, "label": "Frag limit"},
    "capturelimit":    {"kind": "int", "min": 0, "max": 50, "label": "Capture limit"},
    # Bots are what exhaust the game module's G_Alloc pool, not human clients,
    # and this makes the game add them by itself - so it is held to the same
    # ceiling as the Add button (see config.BOT_CEILING for the measurement).
    "bot_minplayers":  {"kind": "int", "min": 0, "max": config.BOT_CEILING, "label": "Min players"},
    "g_gravity":       {"kind": "int", "min": 50, "max": 2000, "label": "Gravity"},
    "g_speed":         {"kind": "int", "min": 50, "max": 1000, "label": "Run speed"},
    "g_quadfactor":    {"kind": "int", "min": 1, "max": 10, "label": "Quad damage factor"},
    "g_weaponrespawn": {"kind": "int", "min": 0, "max": 30, "label": "Weapon respawn (s)"},
    "g_friendlyfire":  {"kind": "int", "min": 0, "max": 1, "label": "Friendly fire"},
    # Lets players run their own map and kick votes from the in-game menu
    # rather than having to ask an admin.
    "g_allowVote":     {"kind": "int", "min": 0, "max": 1, "label": "Allow player votes"},
    "g_inactivity":    {"kind": "int", "min": 0, "max": 36000, "label": "Idle kick (s)"},
    # Reserved slots: without these an admin cannot get into a full server.
    "sv_maxclients":      {"kind": "int", "min": 2, "max": 64, "restart": True,
                           "label": "Max players"},
    "sv_privateClients":  {"kind": "int", "min": 0, "max": 8, "restart": True,
                           "label": "Reserved slots"},
    "sv_privatePassword": {"kind": "text", "max": 63, "secret": True, "restart": True,
                           "label": "Slot password"},
}
# One click's worth of match settings. Each is applied through the same
# route as the form, so every value here is checked by the spec above (a
# unit test insists). Map choice is deliberately left to the Maps tab: its
# rotation presets know which maps carry each game type.
PRESETS = {
    "ffa": {
        "label": "Casual free-for-all",
        "blurb": "Fifteen minutes or thirty frags, four bots to keep it lively, votes allowed.",
        "values": {"g_gametype": 0, "timelimit": 15, "fraglimit": 30, "capturelimit": 8,
                   "bot_minplayers": 4, "g_gravity": 800, "g_speed": 320, "g_quadfactor": 3,
                   "g_weaponrespawn": 5, "g_friendlyfire": 0, "g_allowVote": 1},
    },
    "duel": {
        "label": "Duel",
        "blurb": "Tournament: one on one, ten minutes or fifteen frags, no bots, no votes mid-match.",
        "values": {"g_gametype": 1, "timelimit": 10, "fraglimit": 15, "bot_minplayers": 0,
                   "g_gravity": 800, "g_speed": 320, "g_quadfactor": 3, "g_weaponrespawn": 5,
                   "g_friendlyfire": 0, "g_allowVote": 0},
    },
    "tdm": {
        "label": "Team deathmatch",
        "blurb": "Twenty minutes or fifty frags, six bots to fill the teams, friendly fire off.",
        "values": {"g_gametype": 3, "timelimit": 20, "fraglimit": 50, "bot_minplayers": 6,
                   "g_gravity": 800, "g_speed": 320, "g_quadfactor": 3, "g_weaponrespawn": 5,
                   "g_friendlyfire": 0, "g_allowVote": 1},
    },
    "ctf": {
        "label": "Capture the flag",
        "blurb": "Eight captures or twenty minutes, eight bots, friendly fire off. Fill the rotation with CTF maps.",
        "values": {"g_gametype": 4, "timelimit": 20, "fraglimit": 0, "capturelimit": 8,
                   "bot_minplayers": 8, "g_gravity": 800, "g_speed": 320, "g_quadfactor": 3,
                   "g_weaponrespawn": 5, "g_friendlyfire": 0, "g_allowVote": 1},
    },
    "party": {
        "label": "Party",
        "blurb": "Free-for-all with low gravity, fast running, weapons back in a second and a quad worth five.",
        "values": {"g_gametype": 0, "timelimit": 15, "fraglimit": 50, "bot_minplayers": 4,
                   "g_gravity": 400, "g_speed": 480, "g_quadfactor": 5, "g_weaponrespawn": 1,
                   "g_friendlyfire": 0, "g_allowVote": 1},
    },
}
# Characters that must not reach the server command parser.
UNSAFE_CHARS = frozenset('";$') | {chr(c) for c in range(32)} | {chr(92)}
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
TEAMS = ("red", "blue", "spectator", "free")


def clean_text(value, limit):
    return "".join(c for c in str(value) if c not in UNSAFE_CHARS)[:limit].strip()


def coerce_setting(name, value):
    spec = SETTINGS.get(name)
    if not spec:
        raise ValueError("unknown setting %r" % (name,))
    if spec["kind"] == "text":
        return clean_text(value, spec["max"])
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % name)
    if not spec["min"] <= number <= spec["max"]:
        raise ValueError("%s must be between %s and %s" % (name, spec["min"], spec["max"]))
    return number


def is_secret(name):
    return bool(SETTINGS.get(name, {}).get("secret"))


# --------------------------------------------------------------- live values
def read_cvars():
    """Current values of the settings the console manages."""
    text = game.query_command("cvarlist", timeout=5.0, want="total cvars")
    found = {}
    for name, value in re.findall(r'(\S+)\s+"([^"]*)"', text):
        if name in SETTINGS:
            found[name] = value
    return found


def describe(live):
    """The spec with live values filled in, for the Match tab. A secret is
    reported as present or not, never as its value."""
    spec = {}
    for name, entry in SETTINGS.items():
        if entry.get("secret"):
            spec[name] = dict(entry, value="", present=bool(live.get(name, "")))
        else:
            spec[name] = dict(entry, value=live.get(name, ""))
    return spec


def apply(requested):
    """Validate and set each requested value on the running server.

    Returns (applied, needs_reload, needs_restart). A secret left blank is
    left alone - the form never sees the current value - and an explicit
    null clears it.
    """
    applied, needs_reload, needs_restart = {}, False, False
    for name, raw in requested.items():
        if is_secret(name):
            if raw is None:
                raw = ""
            elif str(raw) == "":
                continue
        value = coerce_setting(name, raw)
        applied[name] = value
        spec = SETTINGS[name]
        needs_reload = needs_reload or spec.get("reload", False)
        needs_restart = needs_restart or spec.get("restart", False)
        game.send_command('set %s "%s"' % (name, value))
    save_settings(applied)
    return applied, needs_reload, needs_restart


# ------------------------------------------------------------ saved state
def saved_settings():
    try:
        return json.loads(config.SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def saved_view():
    """Saved values as the console may see them: secrets blanked."""
    return {k: ("" if is_secret(k) else v) for k, v in saved_settings().items()}


def save_settings(values):
    merged = saved_settings()
    merged.update(values)
    config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.SETTINGS_FILE.write_text(json.dumps(merged, indent=2) + "\n")
    return merged


def saved_rotation():
    try:
        value = json.loads(config.ROTATION_FILE.read_text())
        return [m for m in value if isinstance(m, str)]
    except (OSError, ValueError):
        return []


def save_rotation(maps):
    config.ROTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.ROTATION_FILE.write_text(json.dumps(maps, indent=2) + "\n")
