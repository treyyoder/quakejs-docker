"""Environment, paths and limits shared by more than one module.

Configuration (environment):
    ADMIN_PASSWORD  optional. Unset, a random password is generated on first
                    start, stored hashed at ADMIN_STATE and printed once to the
                    log. Empty, the console does not run at all.
    ADMIN_USER      default "admin"
    ADMIN_PORT      default 8092 (Apache fronts it; not published)
    ADMIN_BIND      default 127.0.0.1
    Q3_FIFO         default /tmp/q3.fifo
    Q3_LOG          default /tmp/q3.log
    ASSETS_DIR      default /var/www/html/assets
    BASE_PAKS       default /quakejs/base/baseq3
    FS_GAME         default baseq3; must match the server's own fs_game
"""

import os
import pathlib
import re

# The console's own directory: the page and its assets live beside the package.
ADMIN_DIR = pathlib.Path(__file__).resolve().parent.parent

# None when unset and "" when explicitly empty; the two mean different things.
PASSWORD = os.environ.get("ADMIN_PASSWORD")
USER = os.environ.get("ADMIN_USER", "admin")
PORT = int(os.environ.get("ADMIN_PORT", "8092"))
BIND = os.environ.get("ADMIN_BIND", "127.0.0.1")
FIFO = pathlib.Path(os.environ.get("Q3_FIFO", "/tmp/q3.fifo"))
LOG = pathlib.Path(os.environ.get("Q3_LOG", "/tmp/q3.log"))
ASSETS = pathlib.Path(os.environ.get("ASSETS_DIR", "/var/www/html/assets"))
BASE_PAKS = pathlib.Path(os.environ.get("BASE_PAKS", "/quakejs/base/baseq3"))
CHECK_MAP = pathlib.Path("/tools/check-map.py")
# The mod the server was started with. Paks outside this game directory are not
# loaded, and asking for a map from one crashes the server outright, so the
# catalog is scoped to it rather than to everything present on disk.
FS_GAME = os.environ.get("FS_GAME", "baseq3")
# Where a password set from the console is stored. Deliberately outside the web
# root so it is never served. Mount a volume here to keep changes across
# redeploys; without one, the password reverts to ADMIN_PASSWORD - or to a
# freshly generated one - whenever the container is recreated.
STATE = pathlib.Path(os.environ.get("ADMIN_STATE", "/var/lib/quakejs/admin.json"))
MOUNT_PATH = os.environ.get("ADMIN_MOUNT_PATH", "/admin")
BUNDLED_ASSETS = pathlib.Path(os.environ.get("BUNDLED_ASSETS", "/opt/assets"))
SETTINGS_FILE = STATE.parent / "settings.json"
ROTATION_FILE = STATE.parent / "rotation.json"
STATS_FILE = STATE.parent / "stats.json"
# The engine keeps its own game log, far better structured than the console
# output: every kill is named by client slot, and a bot gives itself away with
# a skill field in its userinfo.
GAME_LOG = pathlib.Path(os.environ.get("GAME_LOG", f"/quakejs/base/{FS_GAME}/games.log"))
NOTIFY_WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "")
# Long enough that a quiet server is not announced over and over.
NOTIFY_COOLDOWN = float(os.environ.get("NOTIFY_COOLDOWN", "900"))

SESSION_HOURS = 12
# OWASP's current figure for PBKDF2-HMAC-SHA256. A hash stored with fewer
# rounds still verifies and is re-hashed on the next successful sign-in.
PBKDF2_ROUNDS = 600_000
LOCKOUT_AFTER = 5
LOCKOUT_SECONDS = 60
# The console sits behind Apache on loopback, so every request arrives from
# 127.0.0.1 and anything keyed on the peer address - the sign-in lockout above
# all - would collapse to one bucket shared by the whole internet. Apache
# appends the real peer to X-Forwarded-For; auth.client_ip reads it the same
# way the game server does, so a ban and a lockout agree about who somebody is.
MAX_JSON_BYTES = 64 * 1024     # no console request carries more than a few KB
REQUEST_TIMEOUT = 30           # seconds a client may go quiet mid-request

LVL_BASE = "https://lvlworld.com"
UA = "quakejs-docker admin console"
MAX_ZIP_BYTES = 120 * 1024 * 1024
# Uploads come from a signed-in admin rather than a map site, and a base pak is
# far larger than any map: retail pak0.pk3 alone is 457 MB.
MAX_UPLOAD_BYTES = 600 * 1024 * 1024
# The most bots the game module can hold at once. Its G_Alloc pool is 256 KB,
# compiled into qagame.qvm, and each bot takes about 9 KB of it on top of what
# the map's entities took at load. Measured on this build, adding one at a
# time: the allocator fails on bot #27 on pro-q3dm13 and on bot #28 on q3dm1
# and q3dm17, so 26-27 stand. Three below the worst case; a mod with a bigger
# pool may raise it. Client slots are the other limit, and usually the lower.
BOT_CEILING = int(os.environ.get("BOT_CEILING", "24"))
MISSING_SHADER_LIMIT = 5

MAP_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# The game's own paks, as opposed to a map. These are supplied by whoever runs
# the server, because Quake 3's retail content cannot be redistributed.
BASE_PAK_RE = re.compile(r"^pak\d+\.pk3$", re.IGNORECASE)
BOT_NAME_RE = re.compile(r'^\s*name\s+"?([A-Za-z0-9_ ]+?)"?\s*$', re.MULTILINE)
