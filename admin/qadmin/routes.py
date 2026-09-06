"""Every endpoint, one function each.

A route reads what it needs from the request - req.payload for JSON, req.query
for the query string, req.rest for the tail of a prefix path - validates it,
calls into the module that owns the work, and answers with req.json. Whether
a route needs a session is declared where it is registered, so it cannot be
forgotten in a branch; web.dispatch enforces it before any body is read.
"""

import hmac
import json
import pathlib
import secrets
import time
import zipfile

from . import assets
from . import audit
from . import auth
from . import backup
from . import bans
from . import chat
from . import crashes
from . import config
from . import game
from . import settings
from . import stats
from .web import route

# ------------------------------------------------------------- the page
# The page carries its own sign-in form, so it and its assets are public.
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/console.css": ("console.css", "text/css; charset=utf-8"),
    "/console.js": ("console.js", "text/javascript; charset=utf-8"),
}
# What the page may load or do: itself, and nothing else. Its script and
# styles are files beside it - nothing inline, of either - the only images
# are levelshots it serves, and it is framed by the game page (overlay.js),
# which is the same origin. Nothing here is 'unsafe-', and the browser now
# refuses any inline style or script that ever creeps back in.
PAGE_CSP = ("default-src 'self'; style-src 'self'; img-src 'self'; "
            "frame-ancestors 'self'; base-uri 'self'; form-action 'self'; object-src 'none'")

for _path, (_file, _mime) in STATIC.items():
    def _serve(req, _file=_file, _mime=_mime):
        headers = {"Content-Security-Policy": PAGE_CSP} if _mime.startswith("text/html") else None
        req.send_bytes((config.ADMIN_DIR / _file).read_bytes(), _mime, headers=headers)
    route("GET", _path, public=True)(_serve)


# ----------------------------------------------------------- public reads
@route("GET", "/api/ping", public=True)
def api_ping(req):
    req.json({"console": True})


def _public_read(req):
    """(is admin, may proceed) for an anonymous read. An admin's own polling
    is not counted against the address it comes from; a refused read has
    already been answered."""
    admin = req.authed()
    if admin or chat.reads_allowed(req.client_ip()):
        return admin, True
    req.json({"error": "too many requests; slow down"}, 429)
    return admin, False


@route("GET", "/api/messages", public=True)
def api_messages(req):
    # The messenger view reads this. Public chat goes to everyone; private
    # messages only to the browser that sent them, or to an admin.
    admin, proceed = _public_read(req)
    if not proceed:
        return
    try:
        since = int((req.query.get("since") or ["0"])[0])
    except ValueError:
        since = 0
    req.json({"messages": chat.messages_since(since, req.guest_token(), admin)})


@route("GET", "/api/public", public=True)
def api_public(req):
    # The chat view uses this. Names and slots only, never addresses. Cached
    # briefly because each miss writes a status command to the game console.
    if not _public_read(req)[1]:
        return
    req.json(chat.public_state())


@route("GET", "/api/stats", public=True)
def api_stats(req):
    # The leaderboard is for the players, not just the admin.
    if not _public_read(req)[1]:
        return
    req.json(stats.leaderboard())


@route("GET", "/api/session", public=True)
def api_session(req):
    req.json({"authenticated": req.authed(), "user": config.USER})


# ------------------------------------------------------------ public chat
@route("POST", "/api/chat", public=True, body="json")
@route("POST", "/api/pm", public=True, body="json")
def api_chat(req):
    body = req.payload
    token = req.guest_token()
    allowed, why = chat.chat_allowed(token, address=req.client_ip())
    if not allowed:
        return req.json({"error": why}, 429)
    name = chat.clean_chat(body.get("name", ""), chat.NAME_MAX) or "guest"
    message = chat.clean_chat(body.get("message", ""), chat.MESSAGE_MAX)
    if not message:
        return req.json({"error": "message is empty"}, 400)
    if req.path.split("?")[0] == "/api/chat":
        game.send_command('say "%s: %s"' % (name, message))
        chat.add_message("sent", name, None, message)
    else:
        target = body.get("to")
        if not isinstance(target, int) or not 0 <= target <= 63:
            return req.json({"error": "pick someone to send to"}, 400)
        game.send_command('tell %d "[pm] %s: %s"' % (target, name, message))
        # The cached roster is at most a few seconds old and costs nothing;
        # asking the console again per message did.
        roster = chat.public_state().get("players", [])
        to_name = next((p["name"] for p in roster if p["num"] == target), str(target))
        chat.add_message("sent-pm", name, to_name, message, token)
    req.set_guest_cookie(token)
    req.json({"ok": True, "name": name, "message": message})


# ----------------------------------------------------------------- sign-in
@route("POST", "/api/login", public=True, body="json")
def api_login(req):
    ip = req.client_ip()
    wait = auth.lockout_remaining(ip)
    if wait:
        return req.json({"error": f"too many attempts; try again in {wait}s"}, 429)
    user = str(req.payload.get("user", ""))
    password = str(req.payload.get("password", ""))
    if not (hmac.compare_digest(user, config.USER) and auth.check_password(password)):
        auth.note_failure(ip)
        return req.json({"error": "incorrect username or password"}, 401)
    if auth.needs_rehash():
        # A hash from before the round count went up: the password just
        # proved itself, so store it again the current way. Before the
        # session, since re-storing ends every session.
        auth.set_password(password)
    token = auth.new_session()
    req.set_cookie(f"qadmin={token}; Path={config.MOUNT_PATH}; {req.cookie_flags()}; "
                   f"Max-Age={config.SESSION_HOURS * 3600}")
    req.json({"ok": True, "user": config.USER})


@route("POST", "/api/logout", public=True, body="json")
def api_logout(req):
    token = req.cookie_token()
    if token:
        auth.end_session(token)
    req.set_cookie(f"qadmin=; Path={config.MOUNT_PATH}; {req.cookie_flags()}; Max-Age=0")
    req.json({"ok": True})


@route("POST", "/api/password", body="json")
def api_password(req):
    current = str(req.payload.get("current", ""))
    if not auth.check_password(current):
        auth.note_failure(req.client_ip())
        return req.json({"error": "current password is incorrect"}, 401)
    new = str(req.payload.get("new", ""))
    if new == current:
        return req.json({"error": "new password matches the old one"}, 400)
    auth.set_password(new)  # clears every session, including this one
    req.json({"ok": True, "reauth": True})


# ------------------------------------------------------------ server state
# The console polls this constantly, and computing it is expensive: it asks the
# game server for status over the FIFO, scans every pak to work out which maps
# came from an installed pk3, and totals the whole asset tree. Cached so repeat
# polls cost nothing and the game server is not interrogated several times a
# second.
_state_cache = {"at": 0.0, "data": None}
STATE_TTL = 4.0


def invalidate_state():
    _state_cache["data"] = None


@route("GET", "/api/state")
def api_state(req):
    now = time.time()
    if _state_cache["data"] and _state_cache["at"] > now - STATE_TTL:
        return req.json(_state_cache["data"])
    current, players = game.parse_status(game.query_status())
    stats.annotate(players)
    arenas = assets.map_gametypes()
    maps = assets.installed_maps()
    payload = {
        "map": current,
        "players": players,
        "maps": maps,
        "bots": assets.available_bots(),
        "arenas": {m: arenas.get(m, {"types": [], "longname": None}) for m in maps},
        "removable": assets.removable_maps(maps),
        "rotation": settings.saved_rotation(),
        "gametypes": {str(k): v for k, v in settings.GAMETYPES.items()},
        "usage": assets.cached_usage(),
        "bot_room": game.bot_room(players),
    }
    _state_cache.update(at=now, data=payload)
    req.json(payload)


@route("GET", "/api/log")
def api_log(req):
    req.json({"log": game.log_tail()})


@route("GET", "/api/levelshot/", prefix=True)
def api_levelshot(req):
    name = req.rest
    if name not in assets.installed_maps():
        return req.json({"error": "unknown map"}, 404)
    shot = assets.levelshot(name)
    if not shot:
        return req.json({"error": "no preview available"}, 404)
    body, mime = shot
    req.send_bytes(body, mime, cache="public, max-age=86400")


@route("POST", "/api/restart", body="json")
def api_restart(req):
    req.json({"ok": True, "pids": game.restart_game()})


# ---------------------------------------------------------------- settings
@route("GET", "/api/settings")
def api_settings(req):
    live = settings.read_cvars()
    req.json({"settings": settings.describe(live), "saved": settings.saved_view()})


@route("POST", "/api/settings", body="json")
def api_settings_post(req):
    requested = req.payload.get("settings") or {}
    if not isinstance(requested, dict):
        return req.json({"error": "settings must be an object"}, 400)
    applied, needs_reload, needs_restart = settings.apply(requested)
    if needs_reload and not needs_restart:
        game.send_command("map_restart")   # gametype only takes hold on a reload
    if needs_restart and req.payload.get("restart", True):
        game.restart_game()
    req.json({"ok": True, "applied": applied,
              "reloaded": needs_reload, "restarted": needs_restart})


# ----------------------------------------------------------------- players
@route("POST", "/api/say", body="json")
def api_say(req):
    message = settings.clean_text(req.payload.get("message", ""), 180)
    if not message:
        return req.json({"error": "message is empty"}, 400)
    game.send_command('say "%s"' % message)
    chat.add_message("sent", "console", None, message)
    req.json({"ok": True, "message": message})


def _slot(body):
    num = body.get("num")
    if not isinstance(num, int) or not 0 <= num <= 63:
        raise ValueError("num must be a client slot 0-63")
    return num


@route("POST", "/api/team", body="json")
def api_team(req):
    num = _slot(req.payload)
    team = str(req.payload.get("team", "")).lower()
    if team not in settings.TEAMS:
        return req.json({"error": "team must be one of %s" % ", ".join(settings.TEAMS)}, 400)
    game.send_command("forceteam %d %s" % (num, team))
    req.json({"ok": True})


@route("POST", "/api/kick", body="json")
def api_kick(req):
    num = _slot(req.payload)
    game.send_command(f"clientkick {num}")
    invalidate_state()
    req.json({"ok": True})


@route("POST", "/api/bot", body="json")
def api_bot(req):
    body = req.payload
    name = str(body.get("name", ""))
    bots = assets.available_bots()
    match = next((b for b in bots if b.lower() == name.lower()), None)
    if not match:
        return req.json({"error": f"{name!r} is not defined in this bundle; "
                                  f"available: {', '.join(bots)}"}, 400)
    try:
        skill = int(body.get("skill", 3))
    except (TypeError, ValueError):
        return req.json({"error": "skill must be a number"}, 400)
    if not 1 <= skill <= 5:
        return req.json({"error": "skill must be 1-5"}, 400)
    try:
        count = int(body.get("count", 1))
    except (TypeError, ValueError):
        return req.json({"error": "count must be a number"}, 400)
    if count < 1:
        return req.json({"error": "count must be at least 1"}, 400)
    # As many as fit, and not one more: free client slots, and the game
    # module's memory - past its ceiling it dies with "G_Alloc: failed" and
    # takes everyone with it. Measured, not guessed; see config.BOT_CEILING.
    _, players = game.parse_status(game.query_status())
    stats.annotate(players)
    room = game.bot_room(players)
    if count > room["room"]:
        return req.json({"error": _no_room(room)}, 400)
    for _ in range(count):
        game.send_command(f'addbot "{match}" {skill}')
    invalidate_state()
    req.json({"ok": True, "bot": match, "skill": skill, "count": count,
              "room": room["room"] - count})


def _no_room(room):
    """Why more bots will not fit, in terms an admin can act on."""
    n = room["room"]
    lead = "no more bots fit" if n <= 0 else f"only {n} more bot{'s' if n != 1 else ''} fit"
    if room["slots"] <= room["pool"]:
        why = (f"{room['slots']} of {room['maxclients']} client slots are free - "
               "raise Max players on the Match tab for more")
    else:
        why = (f"the game module runs out of memory past {room['ceiling']} bots, "
               f"and {room['bots']} are already in")
    return f"{lead}: {why}"


# -------------------------------------------------------------------- bans
@route("GET", "/api/bans")
def api_bans(req):
    # Reading the list is also when it is put right: expired bans lifted,
    # bans the game forgot over a restart put back.
    req.json({"bans": bans.sync()})


@route("POST", "/api/ban", body="json")
@route("POST", "/api/unban", body="json")
def api_ban(req):
    ip = str(req.payload.get("ip", "")).strip()
    if not settings.IP_RE.match(ip) or any(int(o) > 255 for o in ip.split(".")):
        return req.json({"error": "expected an IPv4 address"}, 400)
    banning = req.path.split("?")[0] == "/api/ban"
    reason = settings.clean_text(req.payload.get("reason", ""), bans.REASON_MAX)
    hours = req.payload.get("hours")
    if hours not in (None, "", 0, "0"):
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            return req.json({"error": "hours must be a number, or empty for good"}, 400)
        if not 0 < hours <= bans.HOURS_MAX:
            return req.json({"error": f"hours must be between 0 and {bans.HOURS_MAX}"}, 400)
    else:
        hours = None
    game.send_command(("addip " if banning else "removeip ") + ip)
    # addip lives in the game module, not the engine, and the demo one predates
    # it - it accepts the command and does nothing. Check the list rather than
    # report a ban that never happened.
    listed = ip in game.parse_banlist(game.query_command("listip", want="IP"))
    if banning and not listed:
        return req.json(
            {"error": "this game module cannot ban addresses. Upload pak1.pk3 - pak8.pk3 from the Quake 3 1.32 point release under Maps, Upload, and the feature appears."}, 501)
    if banning:
        note = bans.record(ip, reason, hours, by=req.client_ip())
        return req.json({"ok": True, "ip": ip, "reason": note["reason"], "expires": note["expires"]})
    bans.forget(ip)
    req.json({"ok": True, "ip": ip})


# -------------------------------------------------------------------- maps
@route("POST", "/api/map", body="json")
def api_map(req):
    name = str(req.payload.get("map", ""))
    if name not in assets.installed_maps():
        return req.json({"error": f"unknown map {name!r}"}, 400)
    game.send_command(f"map {name}")
    invalidate_state()
    req.json({"ok": True, "map": name})


@route("POST", "/api/rotation", body="json")
def api_rotation(req):
    maps = req.payload.get("maps")
    if not isinstance(maps, list) or not all(isinstance(m, str) for m in maps):
        return req.json({"error": "maps must be a list of map names"}, 400)
    known = set(assets.installed_maps())
    unknown = [m for m in maps if m not in known]
    if unknown:
        return req.json({"error": "not installed: %s" % ", ".join(unknown)}, 400)
    settings.save_rotation(maps)
    if req.payload.get("restart", False):
        game.restart_game()
    req.json({"ok": True, "rotation": maps, "note": "takes effect on the next server restart"})


@route("POST", "/api/uninstall", body="json")
def api_uninstall(req):
    name = str(req.payload.get("map", ""))
    current, _ = game.parse_status(game.query_status())
    if name == current:
        return req.json({"error": "that map is running; switch away first"}, 400)
    result = assets.uninstall_map(name)
    settings.save_rotation([m for m in settings.saved_rotation() if m != name])
    if req.payload.get("restart", True):
        result["restarted"] = bool(game.restart_game())
    req.json({"ok": True, **result})


@route("POST", "/api/lookup", body="json")
def api_lookup(req):
    req.json(assets.lvl_lookup(req.payload.get("ref", "")))


@route("POST", "/api/install", body="json")
def api_install(req):
    result = assets.install_from_lvl(req.payload.get("ref", ""), bool(req.payload.get("force")))
    if req.payload.get("restart", True):
        result["restarted"] = bool(game.restart_game())  # re-reads the manifest
    req.json({"ok": True, **result})


@route("POST", "/api/upload", body="raw")
def api_upload(req):
    """Install a pk3 (or a zip of them) posted as raw bytes.

    The payload is binary, so the filename and options ride in the query string
    and nothing has to parse a multipart body. The body is streamed straight to
    disk, because a base pak is hundreds of megabytes and buffering one here
    would cost that twice over.
    """
    # BaseHTTPRequestHandler only answers Expect: 100-continue when it is
    # speaking HTTP/1.1, and this server speaks 1.0, so Apache would sit
    # waiting for an interim response that never arrives and the upload
    # would hang. Browsers never send the header; command line clients do.
    if req.headers.get("Expect", "").lower() == "100-continue":
        req.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        req.wfile.flush()
    filename = pathlib.PurePath((req.query.get("name") or [""])[0]).name
    force = (req.query.get("force") or [""])[0] == "1"
    restart = (req.query.get("restart") or ["1"])[0] != "0"
    try:
        length = int(req.headers.get("Content-Length") or 0)
    except ValueError:
        return req.json({"error": "bad Content-Length"}, 400)
    if length <= 0:
        return req.json({"error": "empty upload"}, 400)
    # Only a base pak may be hundreds of megabytes, and only a base pak is
    # streamed; a map is read whole once it is on disk, so it is held to the
    # map limit - refused here, before a byte of it is accepted.
    base = bool(config.BASE_PAK_RE.match(filename))
    limit = config.MAX_UPLOAD_BYTES if base else config.MAX_ZIP_BYTES
    if length > limit:
        return req.json(
            {"error": f"upload exceeds the {limit // 1048576} MiB limit for "
                      f"{'a base pak' if base else 'a map'}"}, 413)

    # Staged inside the served tree so the finished file is a rename rather
    # than a copy across devices - /tmp and the asset volume are not the same
    # filesystem. Not named .pk3, or a restart mid-upload would leave a
    # half-written file for the manifest to index, and unique per request so
    # two uploads cannot land on each other.
    staging = config.ASSETS / config.FS_GAME / f".incoming-{secrets.token_hex(4)}.part"
    try:
        staging.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        with staging.open("wb") as handle:
            while received < length:
                chunk = req.rfile.read(min(1 << 20, length - received))
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
        if received != length:
            return req.json({"error": "upload truncated"}, 400)

        if base:
            result = assets.install_base_pak(staging, filename.lower())
        else:
            result = assets.install_blob(staging.read_bytes(), filename, force)
    except ValueError as exc:
        return req.json({"error": str(exc)}, 400)
    except (OSError, zipfile.BadZipFile) as exc:
        return req.json({"error": f"{type(exc).__name__}: {exc}"}, 500)
    finally:
        # install_base_pak renames the staged file into place; anything else
        # leaves it behind.
        try:
            staging.unlink()
        except OSError:
            pass
    # The game server reads the manifest once at start, so new content is
    # invisible to it until it comes back.
    if restart:
        result["restarted"] = bool(game.restart_game())
    req.json({"ok": True, **result})


# ------------------------------------------------------------ the record
@route("GET", "/api/audit")
def api_audit(req):
    try:
        limit = max(1, min(1000, int((req.query.get("limit") or ["200"])[0])))
    except ValueError:
        limit = 200
    req.json({"entries": audit.recent(limit)})


@route("GET", "/api/crashes")
def api_crashes(req):
    req.json({"crashes": crashes.recent()})


# ----------------------------------------------------------- presets
@route("GET", "/api/presets")
def api_presets(req):
    # Applied by the console through /api/settings, so the same validation,
    # audit and restart handling as the form.
    req.json({"presets": [dict(entry, key=key) for key, entry in settings.PRESETS.items()]})


# ---------------------------------------------------------- the front page
@route("GET", "/api/public/levelshot", public=True)
def api_public_levelshot(req):
    """The current map's preview, for the front page. The map's name is
    already public; the picture is game content anyone joining downloads."""
    if not _public_read(req)[1]:
        return
    current = chat.public_state().get("map")
    shot = assets.levelshot(current) if current else None
    if not shot:
        return req.json({"error": "no preview available"}, 404)
    body, mime = shot
    req.send_bytes(body, mime, cache="public, max-age=60")


# ---------------------------------------------------------- scheduled backups
@route("GET", "/api/backups")
def api_backups(req):
    req.json({"backups": backup.list_backups(), "keep": backup.BACKUP_KEEP,
              "every_hours": int(backup.BACKUP_EVERY // 3600)})


@route("POST", "/api/backups", body="json")
def api_backup_now(req):
    path = backup.write_backup()
    req.json({"ok": True, "name": path.name, "backups": backup.list_backups()})


@route("GET", "/api/backups/", prefix=True)
def api_backup_file(req):
    name = req.rest
    if not backup.BACKUP_NAME_RE.match(name) or not (backup.BACKUP_DIR / name).is_file():
        return req.json({"error": "no such backup"}, 404)
    req.send_bytes((backup.BACKUP_DIR / name).read_bytes(), "application/json", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ------------------------------------------------------------- backups
IMPORT_MAX_BYTES = 8 * 1024 * 1024   # a leaderboard of 500 rows is well under 100 KB


@route("GET", "/api/export")
def api_export(req):
    body = json.dumps(backup.export_state(), indent=2).encode()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    req.send_bytes(body, "application/json", headers={
        "Content-Disposition": f'attachment; filename="quakejs-state-{stamp}.json"',
        "Cache-Control": "no-store"})


@route("POST", "/api/import", body="raw")
def api_import(req):
    """Restore a bundle from /api/export. Raw rather than JSON-bodied because
    a leaderboard can be larger than the JSON cap; read and capped here."""
    try:
        length = int(req.headers.get("Content-Length") or 0)
    except ValueError:
        return req.json({"error": "bad Content-Length"}, 400)
    if length <= 0:
        return req.json({"error": "empty import"}, 400)
    if length > IMPORT_MAX_BYTES:
        return req.json({"error": f"import exceeds {IMPORT_MAX_BYTES // 1048576} MiB"}, 413)
    try:
        bundle = json.loads(req.rfile.read(length))
    except ValueError:
        return req.json({"error": "not valid JSON"}, 400)
    applied, reauth = backup.import_state(bundle)
    restart = (req.query.get("restart") or ["0"])[0] == "1"
    if restart:
        applied["restarted"] = bool(game.restart_game())
    # Settings and the rotation live in server.cfg once the server restarts.
    req.json({"ok": True, "applied": applied, "reauth": reauth,
              "note": "settings and rotation take effect on the next server restart"})
