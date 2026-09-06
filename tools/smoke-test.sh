#!/usr/bin/env bash
# Start the built image and check the things that have actually broken before:
# the websocket tunnel, the client asset-origin patches, custom maps loading
# through the manifest, and the admin console's auth and map catalog.
#
#     tools/smoke-test.sh [image-tag]
set -euo pipefail

# Git Bash rewrites /container/paths into Windows paths before docker sees them.
# No effect anywhere else, so the same script runs on CI and on a dev machine.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
# That also stops it rewriting /dev/null for native curl, which then tries to
# create a file named dev/null on the current drive, with backslashes -
# succeeding by accident wherever a 'dev' directory happens to exist, and
# failing with curl exit 23 everywhere else. Name the platform's own null
# device instead.
case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) DEVNULL=NUL ;;
	*) DEVNULL=/dev/null ;;
esac

IMAGE="${1:-quakejs:ci}"
NAME="quakejs-smoke-$$"
PASSWORD="smoke-test-password"
HTTP_PORT="${SMOKE_HTTP_PORT:-18080}"
# Generous, because it is a ceiling rather than a wait: a server holding retail
# assets pulls a 457 MB pak0 through its own web server before it will answer.
BOOT_TIMEOUT="${SMOKE_BOOT_TIMEOUT:-600}"

cleanup() { docker rm -f "$NAME" "$NAME-noauth" "$NAME-gen" >/dev/null 2>&1 || true; rm -f ./.smoke-cookies.* ./.smoke-guest*.* ./.smoke-upload.* ./.smoke-basepak.* ./.smoke-bigbody.* ./.smoke-sender.* ./.smoke-other.* ./.smoke-pm.* ./.smoke-unit.* ./.smoke-bundle* ./.smoke-import.* ./.smoke-bots.* ./.smoke-ban.* ./.smoke-backup.* 2>/dev/null || true; }
trap cleanup EXIT
# docker logs into grep -q trips pipefail on a long log: grep quits at the
# first match, docker logs takes SIGPIPE, and the pipeline fails with the line
# right there. Counting reads to the end.
logs_have() { docker logs "$1" 2>&1 | grep -c -- "$2" >/dev/null; }

fail() {
	echo "FAIL: $*" >&2
	echo "--- container logs (tail) ---" >&2
	docker logs "$NAME" 2>&1 | tail -40 >&2 || true
	exit 1
}
ok() { echo "  ok  $*"; }

api() {  # api <method-args...> <path>
	local path="${*: -1}"
	curl -fsS -b "$jar" -u "admin:$PASSWORD" "${@:1:$#-1}" "$BASE$path"
}

jsonfield() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

echo "== starting $IMAGE"
docker run -d --name "$NAME" \
	-e ADMIN_PASSWORD="$PASSWORD" \
	-p "$HTTP_PORT:80" "$IMAGE" >/dev/null

deadline=$((SECONDS + BOOT_TIMEOUT))
until [ "$(docker inspect "$NAME" --format '{{.State.Health.Status}}')" = "healthy" ]; do
	[ $SECONDS -lt $deadline ] || fail "container never became healthy"
	[ "$(docker inspect "$NAME" --format '{{.State.Running}}')" = "true" ] || fail "container exited"
	sleep 3
done
ok "container healthy (game server listening, web server up)"

# --- the page and the single-port websocket tunnel --------------------------
[ "$(curl -fsS -o "$DEVNULL" -w '%{http_code}' "http://localhost:$HTTP_PORT/")" = "200" ] \
	|| fail "client page did not return 200"
ok "client page served"

upgrade=$(curl -s -o "$DEVNULL" -w '%{http_code}' --max-time 10 \
	-H "Connection: Upgrade" -H "Upgrade: websocket" \
	-H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Version: 13" \
	"http://localhost:$HTTP_PORT/" || true)  # curl exits non-zero once the upgraded connection idles out
[ "$upgrade" = "101" ] || fail "websocket upgrade returned $upgrade, expected 101 (game traffic would not reach the server)"
ok "websocket upgrade tunnels to the game server"

# --- client patches ---------------------------------------------------------
# The server advertises its own fs_cdn (localhost:80) to clients, so the client
# must resolve assets against its own origin or every custom map 404s.
docker exec "$NAME" grep -q "window.location.host" /var/www/html/ioquake3.js \
	|| fail "asset-origin patch missing from ioquake3.js"
if docker exec "$NAME" grep -q "_Com_GetCDN());" /var/www/html/ioquake3.js; then
	fail "ioquake3.js still resolves assets via the server-advertised fs_cdn"
fi
docker exec "$NAME" grep -q "wss://" /var/www/html/ioquake3.js \
	|| fail "wss patch missing; the game would break on an HTTPS page"
ok "client patched for own-origin assets and wss"

# --- the console shares the game's port -------------------------------------
[ "$(curl -fsS -o "$DEVNULL" -w '%{http_code}' "http://localhost:$HTTP_PORT/overlay.js")" = "200" ] 	|| fail "overlay.js not served"
grep -q 'overlay.js' <(curl -fsS "http://localhost:$HTTP_PORT/play.html") 	|| fail "overlay script tag not injected into the client page"
ok "overlay script served and injected into the client page"

ping=$(curl -fsS "http://localhost:$HTTP_PORT/admin/api/ping")
echo "$ping" | grep -q '"console": true' || fail "overlay probe failed: $ping"
ok "overlay probe reports a console on the game's own port"

# --- admin console auth -----------------------------------------------------
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' "http://localhost:$HTTP_PORT/admin/api/state")
[ "$code" = "401" ] || fail "admin console answered $code without credentials, expected 401"
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -u "admin:wrong" "http://localhost:$HTTP_PORT/admin/api/state")
[ "$code" = "401" ] || fail "admin console accepted a wrong password ($code)"
# A WWW-Authenticate header is what makes the browser pop its own dialog; the
# console renders its own sign-in form instead, so the header must be absent.
if curl -sI "http://localhost:$HTTP_PORT/admin/api/state" | grep -qi '^www-authenticate'; then
	fail "console sends WWW-Authenticate; the browser would show its own login dialog"
fi
ok "admin console rejects bad credentials without a browser auth dialog"

# --- form login issues a working session ------------------------------------
jar="./.smoke-cookies.$$"
BASE="http://localhost:$HTTP_PORT/admin"
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -c "$jar" -H 'Content-Type: application/json' 	-d "{\"user\":\"admin\",\"password\":\"$PASSWORD\"}" "http://localhost:$HTTP_PORT/admin/api/login")
[ "$code" = "200" ] || fail "form login failed ($code)"
grep -q 'qadmin' "$jar" || fail "login did not set a session cookie"
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -b "$jar" "http://localhost:$HTTP_PORT/admin/api/state")
[ "$code" = "200" ] || fail "session cookie not accepted ($code)"
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -b "$jar" -H 'Content-Type: application/json' 	-d '{}' "http://localhost:$HTTP_PORT/admin/api/logout")
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -b "$jar" "http://localhost:$HTTP_PORT/admin/api/state")
[ "$code" = "401" ] || fail "session still valid after logout ($code)"
ok "form login issues a session cookie and logout revokes it"
# sign back in; the checks below use this session
curl -s -o "$DEVNULL" -c "$jar" -H 'Content-Type: application/json' \n	-d "{\"user\":\"admin\",\"password\":\"$PASSWORD\"}" "$BASE/api/login"

state=$(api /api/state)
maps=$(echo "$state" | jsonfield '["maps"]' )
bots=$(echo "$state" | jsonfield '["bots"]')
ok "admin console reachable with credentials"

# --- map catalog is limited to what the server can load ---------------------
# Maps outside the active fs_game crash the server when loaded.
fs_game=$(docker exec "$NAME" sh -c 'echo "$FS_GAME"' | tr -d '\r')
for bad in cpm1a cpm22 cpm3a; do
	if echo "$maps" | grep -qw "$bad"; then
		fail "catalog offers '$bad' from outside fs_game=$fs_game; loading it crashes the server"
	fi
done
ok "catalog excludes maps outside fs_game=$fs_game"

echo "$bots" | grep -q "Sarge" || fail "bot list is empty or missing stock bots"
ok "bot list read from the bundle: $(echo "$bots" | tr -d "[]'" )"

# --- a bundled custom map loads through the manifest ------------------------
target=$(echo "$state" | python3 -c "
import json,sys
maps = json.load(sys.stdin)['maps']
custom = [m for m in maps if not m.startswith(('q3dm','q3tourney','pro-'))]
print(custom[0] if custom else '')")
if [ -n "$target" ]; then
	api -H 'Content-Type: application/json' -d "{\"map\":\"$target\"}" /api/map >/dev/null
	deadline=$((SECONDS + 90))
	until [ "$(api /api/state | jsonfield '["map"] or ""')" = "$target" ]; do
		[ $SECONDS -lt $deadline ] || fail "custom map '$target' never loaded (manifest or asset path broken)"
		if logs_have "$NAME" "Couldn't load maps/$target"; then
			fail "server crashed loading '$target'"
		fi
		sleep 4
	done
	ok "custom map '$target' loaded through the bundled manifest"
else
	ok "no bundled custom maps to load (the image ships only base content)"
fi

# --- bundled maps render with the textures this bundle actually has ---------
# Screened against the base paks only: the server unpacks map pk3s into the same
# directory at runtime, and those would otherwise mask each other's gaps.
#
# The bundled maps screen at 2 missing shaders or fewer against the paks the
# image ships. This catches a map added that leans on textures nobody here has,
# which would render with placeholder surfaces. Read the count as a smell rather
# than a verdict: Quake 3's own maps reference up to five shaders that ship in no
# pak at all and render fine.
docker exec "$NAME" sh -c '
	set -e
	mkdir -p /tmp/basepaks && cp /quakejs/base/baseq3/pak*.pk3 /tmp/basepaks/ 2>/dev/null || true
	ls /tmp/basepaks/*.pk3 >/dev/null 2>&1 || { echo "  -- base paks not unpacked yet; skipping"; exit 0; }
	python3 /tools/check-map.py --base /tmp/basepaks --max-missing 5 \
		/var/www/html/assets/'"$fs_game"'/*.pk3' \
	|| fail "a bundled map needs textures this bundle does not ship"
ok "bundled maps screened for missing textures"

# --- injection through the console command channel --------------------------
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -u "admin:$PASSWORD" \
	-H 'Content-Type: application/json' -d '{"map":"q3dm1; quit"}' \
	"http://localhost:$HTTP_PORT/admin/api/map")
[ "$code" = "400" ] || fail "console accepted an unvalidated map name ($code)"
ok "console rejects unvalidated console input"

# --- console stays off when the password is explicitly empty -----------------
docker run -d --name "$NAME-noauth" -e ADMIN_PASSWORD= -p "$((HTTP_PORT + 1)):80" "$IMAGE" >/dev/null
sleep 20
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' --max-time 10 "http://localhost:$((HTTP_PORT + 1))/admin/api/ping" || true)
case "$code" in
	200) fail "console answered 200 with ADMIN_PASSWORD empty; it must not run" ;;
	50*|000) ok "console not running when ADMIN_PASSWORD is empty (proxy returned $code)" ;;
	*) fail "unexpected status $code for /admin/api/ping with an empty password" ;;
esac

# --- admin features ---------------------------------------------------------
state=$(api /api/state)
echo "$state" | grep -q '"arenas"' || fail "state is missing arena metadata"
echo "$state" | grep -q '"usage"' || fail "state is missing disk usage"
ok "state exposes arena metadata, rotation and disk usage"

# Maps outside the active gametype must be identifiable, and CTF maps are the
# proof that arena parsing works at all.
echo "$state" | python3 -c "
import json,sys
d=json.load(sys.stdin)
# Gametypes come from .arena files. The image ships none of its own, so this
# only sees what the bundled map paks declare - and no CTF map among them does,
# now that the pak carrying that metadata is gone with the rest of the content
# we cannot redistribute. Assert parsing works, not that any given type exists.
typed=[m for m,a in d['arenas'].items() if a['types']]
named=[m for m,a in d['arenas'].items() if a['longname']]
assert typed, 'no gametypes parsed from any arena file'
assert named, 'no long names parsed from any arena file'
print('  ok  arena parsing: %d maps, %d with gametypes, %d with long names'
      % (len(d['arenas']), len(typed), len(named)))" || fail "arena parsing failed"

shot=$(curl -s -o "$DEVNULL" -w '%{http_code} %{content_type}' -b "$jar" "$BASE/api/levelshot/q3dm1")
case "$shot" in
	200\ image/*) ok "levelshots served from pk3s ($shot)" ;;
	404*) ok "no levelshot for q3dm1 (map ships a .tga)" ;;
	*) fail "levelshot endpoint returned $shot" ;;
esac

api -H 'Content-Type: application/json' -d '{"settings":{"timelimit":17,"g_quadfactor":2},"restart":false}' /api/settings >/dev/null \
	|| fail "settings could not be applied"
echo "$(api /api/settings)" | grep -q '"17"' || fail "applied setting did not stick"
ok "match settings apply to the running server"

code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -b "$jar" -H 'Content-Type: application/json' \
	-d '{"settings":{"timelimit":9999}}' "$BASE/api/settings")
[ "$code" = "400" ] || fail "out-of-range setting accepted ($code)"
hostname=$(api -H 'Content-Type: application/json' \
	-d '{"settings":{"sv_hostname":"a\"; quit; set b \""},"restart":false}' /api/settings)
case "$hostname" in
	*quit*\;*) fail "console text reached the command parser unsanitised" ;;
esac
ok "settings reject out-of-range values and strip console metacharacters"

api -H 'Content-Type: application/json' -d '{"message":"smoke test"}' /api/say >/dev/null \
	|| fail "broadcast failed"
ok "broadcast reaches the server"

# addip is a game module command, and the demo module that ships with the image
# predates it: it accepts the command and silently does nothing. The console has
# to say so rather than report a ban that never happened. Upload the 1.32 point
# release paks and the feature appears.
banned=$(curl -sS -b "$jar" -u "admin:$PASSWORD" -H 'Content-Type: application/json' \
	-d '{"ip":"10.9.9.9"}' "$BASE/api/ban")
case "$banned" in
	*'"ok": true'*)
		sleep 1
		api /api/bans | grep -q '10.9.9.9' || fail "a ban reported as applied was not listed"
		api -H 'Content-Type: application/json' -d '{"ip":"10.9.9.9"}' /api/unban >/dev/null \
			|| fail "unban failed" ;;
	*'cannot ban addresses'*)
		: ;;   # demo game module: reported honestly, which is the point
	*) fail "ban neither applied nor explained: $banned" ;;
esac
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -b "$jar" -H 'Content-Type: application/json' \
	-d '{"ip":"not-an-ip"}' "$BASE/api/ban")
[ "$code" = "400" ] || fail "malformed address accepted for ban ($code)"
ok "bans add, list, remove, and reject malformed addresses"

first_map=$(echo "$state" | python3 -c "import json,sys; print(json.load(sys.stdin)['maps'][0])")
api -H 'Content-Type: application/json' -d "{\"maps\":[\"$first_map\"]}" /api/rotation >/dev/null \
	|| fail "rotation could not be saved"
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -b "$jar" -H 'Content-Type: application/json' \
	-d '{"maps":["definitely-not-installed"]}' "$BASE/api/rotation")
[ "$code" = "400" ] || fail "rotation accepted a map that is not installed ($code)"
ok "rotation saves and rejects unknown maps"

# Bundled maps must never be removable, or a redeploy would silently restore them.
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -b "$jar" -H 'Content-Type: application/json' \
	-d "{\"map\":\"$first_map\"}" "$BASE/api/uninstall")
[ "$code" = "400" ] || fail "a bundled map was accepted for uninstall ($code)"
ok "bundled maps are protected from uninstall"

# --- public (signed out) surface --------------------------------------------
pub=$(curl -fsS "$BASE/api/public") || fail "public state endpoint not reachable signed out"
echo "$pub" | grep -q '"players"' || fail "public state has no player list"
# Player addresses must never appear on the unauthenticated endpoint.
if echo "$pub" | grep -qE '"[0-9]{1,3}(\.[0-9]{1,3}){3}"'; then
	fail "public state leaks player addresses"
fi
ok "public state served signed out, without addresses"

guest="./.smoke-guest.$$"
guest2="./.smoke-guest2.$$"
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -c "$guest" -H 'Content-Type: application/json' \
	-d '{"name":"smoke","message":"hello"}' "$BASE/api/chat")
[ "$code" = "200" ] || fail "public chat rejected signed out ($code)"
ok "public chat works signed out"

sanitised=$(curl -s -b "$guest" -c "$guest" -H 'Content-Type: application/json' \
	-d '{"name":"a\"; quit; say \"","message":"b\"; map q3dm17; say \""}' "$BASE/api/chat")
case "$sanitised" in
	*'quit;'*|*'map q3dm17;'*) fail "public chat passed console metacharacters through" ;;
esac
ok "public chat strips console metacharacters"

# Admin surfaces must stay closed to anonymous callers.
for guarded in state settings bans log; do
	code=$(curl -s -o "$DEVNULL" -w '%{http_code}' "$BASE/api/$guarded")
	[ "$code" = "401" ] || fail "/api/$guarded answered $code signed out, expected 401"
done
for guarded in map bot restart uninstall rotation; do
	code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -H 'Content-Type: application/json' \
		-d '{}' "$BASE/api/$guarded")
	[ "$code" = "401" ] || fail "/api/$guarded accepted an anonymous POST ($code)"
done
ok "admin endpoints stay closed to anonymous callers"

burst=""
for _ in 1 2 3 4 5 6 7 8; do
	burst="$burst $(curl -s -o "$DEVNULL" -w '%{http_code}' -b "$guest" -c "$guest" \
		-H 'Content-Type: application/json' -d '{"name":"s","message":"x"}' "$BASE/api/chat")"
done
case "$burst" in
	*429*) ok "public chat is rate limited ($burst)" ;;
	*) fail "public chat never throttled:$burst" ;;
esac
rm -f "$guest"

# --- messenger stream -------------------------------------------------------
before=$(curl -fsS "$BASE/api/messages?since=0" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['messages']))")
curl -s -o "$DEVNULL" -b "$guest2" -c "$guest2" -H 'Content-Type: application/json' \
	-d '{"name":"smoketester","message":"threaded hello"}' "$BASE/api/chat"
sleep 2
stream=$(curl -fsS "$BASE/api/messages?since=0")
echo "$stream" | grep -q 'threaded hello' \
	|| fail "a message sent from the console never appeared in the stream"
echo "$stream" | grep -q '"kind": "sent"' || fail "sent messages are not tagged for threading"
ok "messenger records sent messages (was $before)"

# The stream must be incremental or the client would re-render everything.
tail_only=$(curl -fsS "$BASE/api/messages?since=999999" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['messages']))")
[ "$tail_only" = "0" ] || fail "since= filter returned $tail_only messages for a future sequence"
ok "message stream is incremental"
rm -f "$guest2"

# --- real client addresses --------------------------------------------------
# Behind the bundled Apache proxy every player used to connect from loopback, so
# the engine saw one address for the whole server and a single ban removed
# everybody. These check the patched vendored server still reads the forwarded
# address, and still refuses to believe one the client wrote itself.
docker exec "$NAME" grep -q "realClientAddress" /quakejs/build/ioq3ded.js \
	|| fail "the client-address patch is missing from the vendored server"
ok "vendored server carries the client-address patch"

address_probe() {  # address_probe <x-forwarded-for>
	docker exec -e SMOKE_XFF="$1" "$NAME" node -e '
	var src = require("fs").readFileSync("/quakejs/build/ioq3ded.js", "utf8");
	var start = src.indexOf("realClientAddress:function");
	var end = src.indexOf("},createPeer:function", start);
	var SOCKFS = {};
	eval("SOCKFS.websocket_sock_ops = {trustedProxies:null," + src.slice(start, end) + "}};");
	var ws = { upgradeReq: { headers: { "x-forwarded-for": process.env.SMOKE_XFF } } };
	console.log(SOCKFS.websocket_sock_ops.realClientAddress(ws, "127.0.0.1"));'
}

[ "$(address_probe "203.0.113.9")" = "203.0.113.9" ] \
	|| fail "the forwarded client address was not recovered"
[ "$(address_probe "9.9.9.9, 203.0.113.9")" = "203.0.113.9" ] \
	|| fail "a client-supplied X-Forwarded-For entry was trusted"
ok "client addresses come from the proxy, not from the client"

if command -v node >/dev/null 2>&1 && [ -f tools/test-play-launch.js ]; then
	node tools/test-play-launch.js >/dev/null \
		|| fail "the game-launch unit tests failed"
	ok "game-launch unit tests pass (saved name applied, sanitised, connect last)"
fi

if command -v node >/dev/null 2>&1 && [ -f tools/test-client-address.js ]; then
	node tools/test-client-address.js >/dev/null \
		|| fail "the client-address unit tests failed"
	ok "client-address unit tests pass"
fi

# The Players tab bans by address, so the address has to reach it.
api /api/state | python3 -c "
import json,sys
state = json.load(sys.stdin)
if 'players' not in state: raise SystemExit('no players key')
for player in state['players']:
    if 'address' not in player: raise SystemExit('player rows carry no address')
" || fail "player rows do not expose an address to ban"
ok "player rows expose an address"

# --- uploading a map --------------------------------------------------------
# The console posts raw bytes, so this route sidesteps the JSON body parsing and
# has to answer Expect: 100-continue itself; without that a command line upload
# hangs forever waiting on an interim response that never comes.
# Skip the numbered base paks: they carry no maps, so they are not installable.
# install_blob needs a pk3 that actually contains a map, and the image may ship
# none of its own - so pick the smallest pak here that has one, wherever it came
# from. Uploaded under a map name it takes the map path, screening and all.
pk3=$(docker exec "$NAME" python3 -c "
import glob, os, zipfile
best = None
for path in glob.glob('/quakejs/base/baseq3/*.pk3') + glob.glob('/var/www/html/assets/baseq3/*.pk3'):
    try:
        names = zipfile.ZipFile(path).namelist()
    except Exception:
        continue
    if not any(n.lower().startswith('maps/') and n.lower().endswith('.bsp') for n in names):
        continue
    size = os.path.getsize(path)
    if best is None or size < best[0]:
        best = (size, path)
print(best[1] if best else '')")
[ -n "$pk3" ] || fail "no pak with a map in it to test uploading"
docker cp "$NAME:$pk3" ./.smoke-upload.pk3 >/dev/null
# force=1: this checks the upload route installs, not that the file screens
# clean - the pak picked above may well reference textures this bundle lacks,
# and rejection has its own check below.
uploaded=$(curl -sS -b "$jar" -u "admin:$PASSWORD" -X POST \
	--data-binary "@./.smoke-upload.pk3" -H 'Content-Type: application/octet-stream' \
	--max-time 180 "$BASE/api/upload?name=smoketest.pk3&restart=0&force=1")
case "$uploaded" in
	*'"ok": true'*) ok "a pk3 uploaded from the console installs" ;;
	*) fail "uploading a pk3 failed: $uploaded" ;;
esac

printf 'definitely not a zip' > ./.smoke-upload.bad
rejected=$(curl -sS -b "$jar" -u "admin:$PASSWORD" -X POST \
	--data-binary "@./.smoke-upload.bad" -H 'Content-Type: application/octet-stream' \
	"$BASE/api/upload?name=bad.pk3&restart=0")
case "$rejected" in
	*'not a zip archive'*) ok "uploads that are not archives are rejected" ;;
	*) fail "a non-archive upload was not rejected: $rejected" ;;
esac

code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -X POST --data-binary '@./.smoke-upload.bad' \
	-H 'Content-Type: application/octet-stream' "$BASE/api/upload?name=bad.pk3")
[ "$code" = "401" ] || fail "/api/upload accepted an anonymous POST ($code)"
ok "uploads stay closed to anonymous callers"

# --- leaderboard ------------------------------------------------------------
# Read from the engine's own match log, so it must be public and well formed
# even before anybody has played a round.
stats=$(curl -fsS "$BASE/api/stats")
echo "$stats" | python3 -c "
import json,sys
data = json.load(sys.stdin)
# Bots are tracked but never shown: nobody is competing with them.
if not isinstance(data.get('players'), list): raise SystemExit('players is not a list')
if 'bots' in data: raise SystemExit('the leaderboard still exposes bots')
for row in data['players']:
    for field in ('name', 'kills', 'deaths', 'ratio', 'best', 'matches'):
        if field not in row: raise SystemExit('a row is missing ' + field)
" || fail "the leaderboard is malformed: $stats"
ok "leaderboard is served publicly and well formed"

# Votes are a match rule players run themselves, so the console must offer it.
api /api/settings | grep -q 'g_allowVote' || fail "the vote setting is not offered"
ok "player votes are settable from the console"

# --- supplied base paks survive the bootstrapper -----------------------------
# Both the server and the client unpack hardcoded installers to produce their
# base paks, and the demo installer's expected checksum is the demo pak0's. Left
# unpatched it overwrites an owner's retail assets on every single start.
for target in /quakejs/build/ioq3ded.js /var/www/html/ioquake3.js; do
	docker exec "$NAME" grep -qF "installer.paks[s].dest" "$target" \
		|| fail "$target is missing the installer patch; supplied paks would be overwritten"
done
ok "server and client skip installers whose paks the manifest supplies"

# Overwriting a large pak used to convert it into a plain JS array first, one
# element per byte, which threw RangeError once a pak reached a few hundred MB.
# The first write of a pak worked and every later one failed, so a server broke
# for returning players the moment its assets changed.
for target in /quakejs/build/ioq3ded.js /var/www/html/ioquake3.js; do
	docker exec "$NAME" grep -qF "Fast path for truncating to nothing" "$target" \
		|| fail "$target is missing the truncation patch; rewriting a large pak would throw"
done
ok "a large pak can be overwritten without being copied into a JS array"

# When the image really does carry supplied base paks, prove they survived the
# round trip. Without them there is nothing to check, which is the CI case.
supplied=$(docker exec "$NAME" python3 -c "
import json
manifest = json.load(open('/var/www/html/assets/manifest.json'))
print(next((e['name'] for e in manifest if e['name'].endswith('/pak0.pk3')), ''))")
if [ -n "$supplied" ]; then
	docker exec "$NAME" python3 -c "
import json, pathlib, sys, zlib
manifest = json.load(open('/var/www/html/assets/manifest.json'))
entry = next(e for e in manifest if e['name'] == '$supplied')
served = pathlib.Path('/quakejs/base/baseq3/pak0.pk3')
if not served.exists():
    sys.exit('the server never fetched pak0.pk3')
actual = zlib.crc32(served.read_bytes()) & 0xFFFFFFFF
if actual != entry['checksum']:
    sys.exit('pak0.pk3 is not the supplied file: crc %d, manifest says %d '
             '(the demo installer overwrote it)' % (actual, entry['checksum']))
print('  ok  supplied %s survived startup (crc %d, %d bytes)'
      % ('$supplied', actual, served.stat().st_size))" || fail "supplied pak0 was replaced"
else
	ok "no supplied base paks in this image (demo content, nothing to check)"
fi

# --- supplying base paks ----------------------------------------------------
# Retail Quake 3 content cannot be redistributed, so the image never carries it
# and an operator supplies their own copy - by URL, or by uploading it. Both
# routes land the file in the served tree for the manifest to pick up.
docker exec "$NAME" test ! -e /opt/assets/baseq3/pak0.pk3 \
	|| fail "the image ships a pak0.pk3; licensed content must not be baked in"
bundled_pak0=$(docker exec "$NAME" sh -c 'ls /opt/assets/baseq3/ | grep -c -- "-pak0.pk3" || true')
[ "$bundled_pak0" = "0" ] || fail "the image ships $bundled_pak0 pak0 files; it must ship none"
ok "the image carries no licensed base pak"

# EXTRA_PAKS parsing: a name override, a checksum, and a bare url.
docker exec "$NAME" python3 -c "
import sys
sys.path.insert(0, '/admin')
import importlib.util
spec = importlib.util.spec_from_file_location('fp', '/admin/fetch-paks.py')
fp = importlib.util.module_from_spec(spec); spec.loader.exec_module(fp)
sha = 'a' * 64
got = fp.parse('https://h/pak0.pk3  pak1.pk3=https://h/d?id=7  https://h/x.pk3#sha256=' + sha)
want = [('pak0.pk3', 'https://h/pak0.pk3', None),
        ('pak1.pk3', 'https://h/d?id=7', None),
        ('x.pk3', 'https://h/x.pk3', sha)]
assert got == want, 'parsed %r, wanted %r' % (got, want)
" || fail "EXTRA_PAKS is not parsed as documented"
ok "EXTRA_PAKS parses names, urls and checksums"

# Fetch one for real over HTTP, using a pak this container is already serving.
# Any bundled pak will do; take the smallest so the round trip stays quick.
seed=$(docker exec "$NAME" sh -c 'ls -S /var/www/html/assets/baseq3/*.pk3 | tail -1')
seed_name=$(basename "$seed")
seed_sha=$(docker exec "$NAME" python3 -c "
import hashlib; print(hashlib.sha256(open('$seed','rb').read()).hexdigest())")

fetched=$(docker exec -e EXTRA_PAKS="pak9.pk3=http://127.0.0.1/assets/baseq3/$seed_name#sha256=$seed_sha" \
	"$NAME" python3 /admin/fetch-paks.py /var/www/html/assets)
echo "$fetched" | grep -q "installed" || fail "EXTRA_PAKS did not fetch the pak: $fetched"
docker exec "$NAME" sh -c 'ls /var/www/html/assets/baseq3/*-pak9.pk3 >/dev/null 2>&1' \
	|| fail "the fetched pak is not in the served tree"
ok "a pak named in EXTRA_PAKS is fetched over HTTP"

# Fetching again must be a no-op, or every restart re-downloads hundreds of MB.
again=$(docker exec -e EXTRA_PAKS="pak9.pk3=http://127.0.0.1/assets/baseq3/$seed_name#sha256=$seed_sha" \
	"$NAME" python3 /admin/fetch-paks.py /var/www/html/assets)
echo "$again" | grep -q "already present" || fail "a present pak was fetched again: $again"
ok "an already-fetched pak is not downloaded twice"

# A wrong checksum must be refused rather than installed.
docker exec "$NAME" sh -c 'rm -f /var/www/html/assets/baseq3/*-pak9.pk3'
refused=$(docker exec -e EXTRA_PAKS="pak9.pk3=http://127.0.0.1/assets/baseq3/$seed_name#sha256=$(printf 'b%.0s' $(seq 64))" \
	"$NAME" python3 /admin/fetch-paks.py /var/www/html/assets)
echo "$refused" | grep -q "checksum mismatch" || fail "a mismatched checksum was accepted: $refused"
docker exec "$NAME" sh -c '! ls /var/www/html/assets/baseq3/*-pak9.pk3 >/dev/null 2>&1' \
	|| fail "a pak that failed its checksum was installed anyway"
ok "a pak that fails its checksum is refused"

# An unreachable url is logged and skipped, never fatal: the server has to come
# up on the demo content rather than refuse to start.
missing=$(docker exec -e EXTRA_PAKS="http://127.0.0.1:9/nope.pk3" \
	"$NAME" python3 /admin/fetch-paks.py /var/www/html/assets 2>&1) \
	|| fail "an unreachable EXTRA_PAKS url made the fetcher exit non-zero"
echo "$missing" | grep -q "could not install" || fail "an unreachable url was not reported: $missing"
ok "an unreachable pak url is skipped, not fatal"

# The upload route takes a base pak too, and must not screen it like a map:
# there is nothing to screen the game's own content against.
docker cp "$NAME:$seed" ./.smoke-basepak.pk3 >/dev/null
uploaded=$(curl -sS -b "$jar" -u "admin:$PASSWORD" -X POST \
	--data-binary "@./.smoke-basepak.pk3" -H 'Content-Type: application/octet-stream' \
	--max-time 180 "$BASE/api/upload?name=pak9.pk3&restart=0")
case "$uploaded" in
	*'"ok": true'*) ok "a base pak uploads through the console" ;;
	*) fail "uploading a base pak failed: $uploaded" ;;
esac
docker exec "$NAME" sh -c 'ls /var/www/html/assets/baseq3/*-pak9.pk3 >/dev/null 2>&1' \
	|| fail "the uploaded base pak is not in the served tree"
docker exec "$NAME" sh -c 'rm -f /var/www/html/assets/baseq3/*-pak9.pk3'
rm -f ./.smoke-basepak.pk3
ok "an uploaded base pak lands in the served tree"

# ...and is listed in the manifest, which is the part that actually matters.
# The manifest is what the game server reads at start and what browser clients
# download from; sync-assets.py only rebuilds it when the container starts, so
# an upload that skips this looks installed and does nothing until a restart.
curl -fsS "http://localhost:$HTTP_PORT/assets/manifest.json" | python3 -c "
import json, sys
names = [e['name'] for e in json.load(sys.stdin)]
if 'baseq3/pak9.pk3' not in names:
    raise SystemExit('an uploaded pak is missing from the manifest: %r' % (names,))
" || fail "an uploaded base pak never reached the served manifest"
ok "an uploaded base pak is indexed in the manifest"
# The synthetic pak was deleted from disk above; drop its manifest entry too,
# or every later restart of the game server dies fetching a pak that is gone.
docker exec "$NAME" python3 -c "
import json, pathlib
p = pathlib.Path('/var/www/html/assets/manifest.json')
p.write_text(json.dumps([e for e in json.loads(p.read_text()) if e['name'] != 'baseq3/pak9.pk3'], indent=2) + chr(10))
"

# --- request-layer hardening ---------------------------------------------
# The console validated its inputs but not its resources: bodies were read
# before authentication and without a cap, Basic auth skipped the lockout, the
# lockout keyed on the proxy's address, and an idle connection held a thread
# forever. Each of these pins one of those fixes.

# S1: an anonymous POST is refused before its body is read, and a signed-in one
# over the cap is refused without being read either. The first is checked
# against the console directly: Apache relays the whole announced body before
# it will pass an early response back, so through the proxy the client just
# waits - Apache's own request timeout bounds that side.
code=$(docker exec "$NAME" curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
	-H 'Content-Type: application/json' -H 'Content-Length: 500000000' -d '{}' \
	http://127.0.0.1:8092/api/kick || true)
[ "$code" = "401" ] || fail "an anonymous POST with a huge body was not refused up front ($code)"
python3 -c "print('{\"pad\": \"' + 'x' * 100000 + '\"}')" > ./.smoke-bigbody.json
code=$(curl -s -o "$DEVNULL" -w '%{http_code}' -b "$jar" -u "admin:$PASSWORD" \
	-H 'Content-Type: application/json' --data-binary @./.smoke-bigbody.json "$BASE/api/kick")
[ "$code" = "413" ] || fail "a 100 KB JSON body was accepted ($code); the cap is not enforced"
rm -f ./.smoke-bigbody.json
ok "request bodies are refused before being read unless small and authorised"

# --- unit tests, run inside the image ----------------------------------------
# The pure parts are pinned by tools/test-admin.py: the status, chat and game
# log parsers, the settings spec build-config.py folds into server.cfg, the
# file follower both tailers share, client attribution through the proxy,
# leaderboard pruning, base-pak recognition. Run here against the image's own
# Python and files rather than the checkout's.
docker exec "$NAME" python3 /tools/test-admin.py >/dev/null 2>./.smoke-unit.log \
	|| { cat ./.smoke-unit.log; fail "the unit tests fail inside the image"; }
rm -f ./.smoke-unit.log
ok "unit tests pass inside the image (parsers, settings spec, follower, client address)"

# --- S5: the network-facing processes are not root ---------------------------
docker exec "$NAME" sh -c '
	bad=0
	for p in /proc/[0-9]*; do
		# Not this shell: its own command line carries the patterns below, and
		# docker exec runs it as root, which read as the server being root.
		[ "${p#/proc/}" = "$$" ] && continue
		# xargs -0 joins the NUL-separated arguments without an escape sequence
		# that a shell layer could mangle.
		c=$(xargs -0 echo < "$p/cmdline" 2>/dev/null) || continue
		case "$c" in
			*build/ioq3ded.js*|*python3\ /admin/server.py*)
				u=$(stat -c %U "$p")
				echo "  $u  ${c%% +set*}"
				[ "$u" = "quake" ] || bad=1 ;;
		esac
	done
	exit $bad' || fail "the game server or the console is running as root"
ok "game server and console run as an unprivileged user"

# The volumes must have been handed over, or uploads and settings would fail
# for that user - and an image upgrade over an older root-owned volume is the
# case that bites.
docker exec "$NAME" sh -c 'test "$(stat -c %U /var/www/html/assets)" = quake && test "$(stat -c %U /var/lib/quakejs)" = quake' \
	|| fail "the volumes are not owned by the service user"
ok "volumes are owned by the service user"

# --- S6: cookies are Secure when the proxy reports TLS, never otherwise ------
plain=$(curl -sS -D - -o "$DEVNULL" -H 'Content-Type: application/json' \
	-d "{\"user\":\"admin\",\"password\":\"$PASSWORD\"}" "$BASE/api/login" | grep -i '^set-cookie')
case "$plain" in
	*[Ss]ecure*) fail "the session cookie was marked Secure on a plain-HTTP request; browsers would drop it: $plain" ;;
esac
tls=$(curl -sS -D - -o "$DEVNULL" -H 'X-Forwarded-Proto: https' -H 'Content-Type: application/json' \
	-d "{\"user\":\"admin\",\"password\":\"$PASSWORD\"}" "$BASE/api/login" | grep -i '^set-cookie')
case "$tls" in
	*[Ss]ecure*) : ;;
	*) fail "the session cookie lacks Secure when the proxy reports TLS: $tls" ;;
esac
case "$tls" in
	*HttpOnly*SameSite=Strict*|*SameSite=Strict*HttpOnly*) : ;;
	*) fail "the session cookie lost HttpOnly or SameSite: $tls" ;;
esac
ok "session cookie is Secure behind TLS and HttpOnly + SameSite always"

# --- S7: the build is pinned ---------------------------------------------------
grep -qE '^ARG QUAKEJS_REF=[0-9a-f]{40}$' Dockerfile || fail "QUAKEJS_REF is not pinned to a commit"
grep -qE '^ARG NODE_SHA256_AMD64=[0-9a-f]{64}$' Dockerfile && grep -qE '^ARG NODE_SHA256_ARM64=[0-9a-f]{64}$' Dockerfile || fail "node is not pinned to a checksum per architecture"
! grep -q 'nodesource' Dockerfile || fail "the build still pipes a setup script to a shell"
ok "build inputs are pinned to a commit and a checksum"

# --- S8: private messages reach only the browser that sent them --------------
# Needs somebody to send to; a bot will do, the message goes into the game log
# either way.
api -H 'Content-Type: application/json' -d '{"name":"Sarge","skill":1,"count":1}' /api/bot >/dev/null
sleep 3
# Two browsers, each with its own guest cookie from a public post first; the
# public text must differ from the private one or the stranger legitimately
# sees it in the open stream and the check reads as a leak.
sender=./.smoke-sender.$$; other=./.smoke-other.$$
curl -sS -o "$DEVNULL" -b "$sender" -c "$sender" -H 'Content-Type: application/json' \
	-d '{"name":"alice","message":"hello from alice"}' "$BASE/api/chat"
curl -sS -o "$DEVNULL" -b "$other" -c "$other" -H 'Content-Type: application/json' \
	-d '{"name":"bob","message":"hello from bob"}' "$BASE/api/chat"
curl -sS -o "$DEVNULL" -b "$sender" -c "$sender" -H 'Content-Type: application/json' \
	-d '{"name":"alice","to":0,"message":"secret handshake"}' "$BASE/api/pm"
sleep 1
curl -fsS -b "$sender" "$BASE/api/messages?since=0" | grep -q 'secret handshake' \
	|| fail "a sender cannot see the private message they just sent"
curl -fsS -b "$other" -c "$other" "$BASE/api/messages?since=0" | grep -q 'secret handshake' \
	&& fail "a stranger can read somebody else's private message"
curl -fsS "$BASE/api/messages?since=0" | grep -q 'secret handshake' \
	&& fail "an anonymous caller with no cookie can read a private message"
api /api/messages?since=0 | grep -q 'secret handshake' \
	|| fail "the admin cannot see private messages"
anon_kinds=$(curl -fsS "$BASE/api/messages?since=0" | python3 -c "
import json, sys
print(' '.join(sorted({m['kind'] for m in json.load(sys.stdin)['messages']})))")
case "$anon_kinds" in
	*tell*|*team*|*sent-pm*) fail "the anonymous stream carries private kinds: $anon_kinds" ;;
esac
rm -f "$sender" "$other"
ok "private messages are visible only to their sender and to admins"

# --- S13: nothing is baked in; a bare container mints its own password -------
# The image carries no ADMIN_PASSWORD. Started without one, the console
# generates a password, stores it hashed in the state volume, prints it once,
# and keeps it across a restart. Given one, it generates nothing.
docker image inspect "$IMAGE" --format '{{.Config.Env}}' | grep -q 'ADMIN_PASSWORD' \
	&& fail "the image bakes in an admin password"
logs_have "$NAME" 'generated one for user' \
	&& fail "a password was generated although ADMIN_PASSWORD was given"
docker run -d --name "$NAME-gen" -e Q3_GAME_LOG_MAX=100 "$IMAGE" >/dev/null
deadline=$((SECONDS + 120))
until logs_have "$NAME-gen" "\[admin\] listening"; do
	[ $SECONDS -lt $deadline ] || fail "a container started without ADMIN_PASSWORD never started its console"
	sleep 2
done
gen=$(docker logs "$NAME-gen" 2>&1 | sed -n "s/.*generated one for user 'admin': //p" | head -1)
[ -n "$gen" ] || fail "no generated password in the log of a container started without one"
docker exec "$NAME-gen" curl -s -u "admin:$gen" http://127.0.0.1:8092/api/session \
	| grep -q '"authenticated": true' || fail "the generated password does not sign in"
docker exec "$NAME-gen" curl -s -u admin:changeme-local http://127.0.0.1:8092/api/session \
	| grep -q '"authenticated": true' && fail "the old repository default still signs in"
docker exec "$NAME-gen" sh -c 'test "$(stat -c %U:%a /var/lib/quakejs/admin.json)" = quake:600' \
	|| fail "the generated password's hash is not a quake-owned 0600 file"
docker restart "$NAME-gen" >/dev/null
deadline=$((SECONDS + 120))
until [ "$(docker logs "$NAME-gen" 2>&1 | grep -c "\[admin\] listening")" -ge 2 ]; do
	[ $SECONDS -lt $deadline ] || fail "the console did not come back after a restart"
	sleep 2
done
[ "$(docker logs "$NAME-gen" 2>&1 | grep -c 'generated one for user')" -eq 1 ] \
	|| fail "a restart generated a second password instead of keeping the stored one"
docker exec "$NAME-gen" curl -s -u "admin:$gen" http://127.0.0.1:8092/api/session \
	| grep -q '"authenticated": true' || fail "the generated password did not survive a restart"
# E3: the game log was past its (tiny) cap, so the restart rotated it. The
# engine writes the new one a few seconds into the map, hence the wait.
deadline=$((SECONDS + 60))
until docker exec "$NAME-gen" sh -c 'test -f /quakejs/base/baseq3/games.log.1 && test -s /quakejs/base/baseq3/games.log'; do
	[ $SECONDS -lt $deadline ] || fail "games.log was not rotated at the restart although it was past its cap"
	sleep 2
done
docker rm -f "$NAME-gen" >/dev/null 2>&1
ok "no password in the image; a bare container generates, stores and keeps its own; games.log rotates past its cap"

# --- B3: a private message no longer asks the game console for the roster ---
# The roster comes from the same cache the public view uses; status output in
# the game log must not grow with each message.
api /api/public >/dev/null
before=$(docker exec "$NAME" sh -c 'grep -c "^map: " /tmp/q3.log')
pmjar=./.smoke-pm.$$
for i in 1 2 3; do
	curl -sS -o "$DEVNULL" -b "$pmjar" -c "$pmjar" -H 'Content-Type: application/json' \
		-d '{"name":"alice","to":0,"message":"ping"}' "$BASE/api/pm"
done
after=$(docker exec "$NAME" sh -c 'grep -c "^map: " /tmp/q3.log')
rm -f "$pmjar"
[ "$after" -le $((before + 1)) ] || fail "private messages still query the console: status ran $((after - before)) times for 3 messages"
ok "private messages use the cached roster instead of querying the console"

# --- B5: a secret setting is never returned, blank keeps it, null clears it --
api -H 'Content-Type: application/json' -d '{"settings":{"sv_privatePassword":"hunter2"},"restart":false}' /api/settings >/dev/null \
	|| fail "could not set a secret setting"
secret=$(api /api/settings)
echo "$secret" | grep -q 'hunter2' && fail "a secret setting came back in the clear"
echo "$secret" | python3 -c "
import json, sys
s = json.load(sys.stdin)['settings']['sv_privatePassword']
assert s['value'] == '' and s['present'] is True, s
" || fail "a set secret is not reported as present-but-masked"
api -H 'Content-Type: application/json' -d '{"settings":{"sv_privatePassword":""},"restart":false}' /api/settings >/dev/null
api /api/settings | grep -q '"present": true' || fail "saving a blank secret cleared it; blank must mean keep"
api -H 'Content-Type: application/json' -d '{"settings":{"sv_privatePassword":null},"restart":false}' /api/settings >/dev/null
api /api/settings | python3 -c "
import json, sys
assert json.load(sys.stdin)['settings']['sv_privatePassword']['present'] is False, 'null did not clear the secret'
" || fail "an explicit null did not clear the secret"
ok "secrets are masked on read; blank keeps them and null clears them"

# --- S11: a map upload is held to the map limit before it is read ------------
# Only a base pak streams; a map is read whole once on disk, so a 200 MB one
# is refused on its announced size. Straight at the backend: Apache relays a
# body before it passes an early answer back.
code=$(docker exec "$NAME" sh -c "curl -s -o /dev/null -w '%{http_code}' --max-time 10 -u admin:$PASSWORD -H 'Content-Length: 200000000' -d x 'http://127.0.0.1:8092/api/upload?name=map.pk3'")
[ "$code" = "413" ] || fail "a 200 MB map upload was not refused up front ($code)"
ok "map uploads are refused past the map limit before being read"

# --- S12: no version banners; content is what it says; no framing elsewhere ---
head=$(curl -sI "http://localhost:$HTTP_PORT/")
echo "$head" | grep -qi '^server: apache[[:space:]]*$' || fail "Apache still announces its version: $(echo "$head" | grep -i '^server:')"
echo "$head" | grep -qi '^x-content-type-options: nosniff' || fail "no nosniff on the game page"
echo "$head" | grep -qi "^content-security-policy: .*frame-ancestors 'self'" || fail "no frame-ancestors on the game page"
page=$(curl -sI "$BASE/")
echo "$page" | grep -qi "^content-security-policy: default-src 'self'" || fail "the console page carries no policy of its own"
echo "$page" | grep -qi 'unsafe-' && fail "the console policy allows something inline: $(echo "$page" | grep -i '^content-security-policy')"
echo "$page" | grep -qi '^server: .*python' && fail "the console announces its Python version"
docker exec "$NAME" curl -sI http://127.0.0.1:8092/api/ping | grep -qi '^server: quakejs-admin[[:space:]]*$' || fail "the console's own Server header still carries a version"
ok "no version banners; nosniff and frame-ancestors on every page, a strict policy on the console"

# --- bots: as many as fit, never one more ---------------------------------------
# The room is the free client slots or the game module's memory ceiling,
# whichever is lower; the state reports it and the server enforces it.
room=$(api /api/state | python3 -c "import json,sys; r=json.load(sys.stdin)['bot_room']; print(r['room'], r['maxclients'], r['ceiling'])")
set -- $room
[ "$2" -gt 0 ] && [ "$1" -le "$2" ] && [ "$1" -le "$3" ] || fail "bot room is not bounded by slots and the module ceiling: $room"
code=$(curl -s -o ./.smoke-bots.$$ -w '%{http_code}' -u "admin:$PASSWORD" -H 'Content-Type: application/json' \
	-d "{\"name\":\"Sarge\",\"skill\":1,\"count\":$(( $1 + 1 ))}" "$BASE/api/bot")
[ "$code" = "400" ] && grep -q 'fit' ./.smoke-bots.$$ || fail "one bot more than fits was not refused ($code): $(cat ./.smoke-bots.$$)"
rm -f ./.smoke-bots.$$
ok "bots may be added up to what fits ($1 right now of $2 slots, ceiling $3); one more is refused"

# --- front page ------------------------------------------------------------------
page=$(curl -fsS "http://localhost:$HTTP_PORT/")
echo "$page" | grep -q 'id="join"' && echo "$page" | grep -q 'href="/play.html"' || fail "the front page is not served at /"
[ "$(curl -fsS -o "$DEVNULL" -w '%{http_code}' "http://localhost:$HTTP_PORT/landing.js")" = "200" ] || fail "landing.js is not served"
grep -q 'TOTAL_MEMORY' <(curl -fsS "http://localhost:$HTTP_PORT/play.html") || fail "the game page is not at /play.html"
grep -q 'window.quakejsLaunch(args, extra)' <(curl -fsS "http://localhost:$HTTP_PORT/play.html") || fail "the game page does not use the launch hook"
[ "$(curl -fsS -o "$DEVNULL" -w '%{http_code}' "http://localhost:$HTTP_PORT/play-launch.js")" = "200" ] || fail "play-launch.js is not served"
echo "$page" | grep -q 'id="name"' || fail "the front page has no Play-as field"
echo "$page" | grep -q 'href="https://github.com/treyyoder/quakejs-docker"' || fail "the front page heading does not link to GitHub"
shot=$(curl -s -o "$DEVNULL" -w '%{http_code} %{content_type}' "$BASE/api/public/levelshot")
case "$shot" in 200\ image/*|404*) : ;; *) fail "the public levelshot answers $shot" ;; esac
ok "front page at /, game at /play.html, the current map's picture is public ($shot)"

# --- temporary bans with a reason ------------------------------------------------
code=$(curl -s -o ./.smoke-ban.$$ -w '%{http_code}' -u "admin:$PASSWORD" -H 'Content-Type: application/json' \
	-d '{"ip":"203.0.113.77","reason":"smoke test","hours":0.0006}' "$BASE/api/ban")
if [ "$code" = "501" ]; then
	ok "temporary bans: skipped, this game module cannot ban"
else
	[ "$code" = "200" ] || fail "a timed ban was refused ($code): $(cat ./.smoke-ban.$$)"
	api /api/bans | grep -q '"reason": "smoke test"' || fail "the ban's reason is not listed"
	sleep 4
	api /api/bans | grep -q '203.0.113.77' && fail "a two-second ban is still listed after four"
	ok "a timed ban carries its reason and lifts itself"
fi
rm -f ./.smoke-ban.$$

# --- scheduled backups ----------------------------------------------------------
api /api/backups | grep -q '"name": "state-' || fail "no automatic backup was written at start"
first=$(api /api/backups | python3 -c "import json,sys; print(json.load(sys.stdin)['backups'][0]['name'])")
curl -fsS -o ./.smoke-backup.$$ -u "admin:$PASSWORD" "$BASE/api/backups/$first" || fail "a backup cannot be downloaded"
grep -q '"format": "quakejs-state/1"' ./.smoke-backup.$$ || fail "the backup is not a state bundle"
api -H 'Content-Type: application/json' -d '{}' /api/backups | grep -q '"ok": true' || fail "back up now failed"
rm -f ./.smoke-backup.$$
ok "a backup is written at start, listed, downloadable, and on demand"

# --- match presets --------------------------------------------------------------
# Through the settings route, like the form. A game-type change makes the engine
# respawn the server, which in this synthetic setup stalls on a pak it cannot
# fetch, so the preset exercised here keeps the game type and moves the rest.
api /api/presets | grep -q '"key": "duel"' || fail "presets are not offered"
values=$(api /api/presets | python3 -c "import json,sys; print(json.dumps(next(p for p in json.load(sys.stdin)['presets'] if p['key']=='party')['values']))")
api -H 'Content-Type: application/json' -d "{\"settings\":$values,\"restart\":false}" /api/settings | grep -q '"ok": true' || fail "applying a preset failed"
api /api/settings | python3 -c "import json,sys; s=json.load(sys.stdin)['settings']; assert (s['g_gravity']['value'], s['g_quadfactor']['value'], s['g_speed']['value']) == ('400', '5', '480'), (s['g_gravity']['value'], s['g_quadfactor']['value'], s['g_speed']['value'])" || fail "the preset's values did not land"
values=$(api /api/presets | python3 -c "import json,sys; print(json.dumps(next(p for p in json.load(sys.stdin)['presets'] if p['key']=='ffa')['values']))")
api -H 'Content-Type: application/json' -d "{\"settings\":$values,\"restart\":false}" /api/settings >/dev/null
ok "match presets apply through the settings route"

# --- E2: nothing in the container runs as root -------------------------------
# The entrypoint hands the volumes over and drops privileges by exec'ing
# itself; from then on Apache, the sync, the game server, the console and the
# supervisor are all quake. docker top looks from outside, so the shell this
# suite execs is not in the picture.
rootprocs=$(docker top "$NAME" -eo pid,uid,comm | awk 'NR > 1 && $2 == 0 { print $3 }')
[ -z "$rootprocs" ] || fail "processes still running as root: $rootprocs"
ok "no process in the container runs as root"

# --- E3: Apache logs do not grow inside the container -------------------------
logs_have "$NAME" '"GET /admin/api/[a-z]* HTTP/1.1" 200' \
	|| fail "Apache's access log is not in the container output"
docker exec "$NAME" sh -c 'test ! -s /var/log/apache2/access.log && test ! -s /var/log/apache2/error.log' \
	|| fail "Apache still writes log files inside the container"
ok "Apache logs go to the container output, not to files"

# --- E5: admin actions are on the record --------------------------------------
api -H 'Content-Type: application/json' -d '{"num":0}' /api/kick >/dev/null
audit=$(api "/api/audit?limit=50")
echo "$audit" | grep -q '"action": "/api/kick"' || fail "a kick left no audit entry"
echo "$audit" | grep -q '"action": "/api/login"' || fail "sign-ins are not audited"
# The B5 checks above set the slot password to hunter2: the key may appear,
# the value never, and sign-ins carry nothing at all.
echo "$audit" | grep -q 'hunter2' && fail "a secret setting's value reached the audit trail"
echo "$audit" | grep -q '"sv_privatePassword": "(secret)"' || fail "a secret setting is not shown redacted in the audit trail"
echo "$audit" | grep -q '"action": "/api/login", "detail": {}' || fail "a sign-in entry carries detail"
logs_have "$NAME" '^\[audit\] .* /api/kick' || fail "audit entries are not in the container log"
ok "admin actions and sign-ins are audited, without secrets"

# --- E6: the state exports and imports ----------------------------------------
bundle=./.smoke-bundle.$$
curl -fsS -D ./.smoke-bundle-headers.$$ -o "$bundle" -u "admin:$PASSWORD" "$BASE/api/export" || fail "export failed"
grep -qi '^content-disposition: attachment' ./.smoke-bundle-headers.$$ || fail "export is not offered as a download"
grep -q '"format": "quakejs-state/1"' "$bundle" || fail "export is not a state bundle"
code=$(curl -s -o ./.smoke-import.$$ -w '%{http_code}' -u "admin:$PASSWORD" -H 'Content-Type: application/json' --data-binary "@$bundle" "$BASE/api/import")
[ "$code" = "200" ] || fail "import of an export was refused ($code): $(cat ./.smoke-import.$$)"
grep -q '"ok": true' ./.smoke-import.$$ || fail "import did not succeed"
rm -f "$bundle" ./.smoke-bundle-headers.$$ ./.smoke-import.$$
ok "state exports as a download and imports back"

# --- E8: the healthcheck covers the console, which is supervised ---------------
docker inspect "$NAME" --format '{{json .Config.Healthcheck.Test}}' | grep -q 'healthcheck.sh' \
	|| fail "the image healthcheck is not the script"
docker exec "$NAME" sh -c 'exec /tools/healthcheck.sh' || fail "the healthcheck fails on a healthy container"
docker exec "$NAME" sh -c 'kill $(pidof python3)'
sleep 1
docker exec "$NAME" sh -c 'exec /tools/healthcheck.sh' 2>/dev/null && fail "the healthcheck passed with the console dead"
deadline=$((SECONDS + 30))
until docker exec "$NAME" sh -c 'exec /tools/healthcheck.sh' 2>/dev/null; do
	[ $SECONDS -lt $deadline ] || fail "the console did not come back after being killed"
	sleep 2
done
ok "a dead console fails the healthcheck and is restarted by the entrypoint"

# --- E9: the build is pinned, per platform, and signed --------------------------
grep -qE '^FROM ubuntu:22.04@sha256:[0-9a-f]{64}$' Dockerfile || fail "the base image is not pinned by digest"
grep -q 'NODE_SHA256_ARM64=' Dockerfile && grep -q 'TARGETARCH' Dockerfile || fail "node is not pinned per architecture"
grep -q 'linux/arm64' .github/workflows/dockerimage.yml || fail "CI does not build arm64"
grep -q 'cosign sign' .github/workflows/dockerimage.yml || fail "CI does not sign the image"
ok "base image pinned by digest, node pinned per architecture, CI builds both and signs"

# --- crash notes ---------------------------------------------------------------
# "Server crashed:" in the console log makes the tailer keep the log's tail as a
# crash note. The write goes in as quake, which owns the log: root in some
# daemons (GitHub Actions) runs without CAP_DAC_OVERRIDE and cannot write a file
# it does not own. The same line trips the watchdog into a restart that empties
# the log, so re-inject each poll until the tailer has recorded the note.
deadline=$((SECONDS + 60))
until api /api/crashes | grep -q '"reason": "smoke test crash"'; do
	[ $SECONDS -lt $deadline ] || fail "the crash was not recorded"
	docker exec -u quake "$NAME" sh -c 'echo "----- Server Shutdown (Server crashed: smoke test crash" >> /tmp/q3.log' 2>/dev/null || true
	sleep 2
done
api /api/crashes | python3 -c "import json,sys; c=json.load(sys.stdin)['crashes'][0]; assert c['tail'] and any('smoke test crash' in l for l in c['tail']), c" || fail "the crash note did not capture the log tail"
deadline=$((SECONDS + 150))
until [ -n "$(api /api/state | python3 -c "import json,sys; print(json.load(sys.stdin).get('map') or '')" 2>/dev/null)" ] && ! docker exec "$NAME" sh -c 'grep -q "Server crashed" /tmp/q3.log' 2>/dev/null; do
	[ $SECONDS -lt $deadline ] || fail "the game server did not come back after the crash"
	sleep 3
done
ok "a crash is recorded with the log tail and the map; the server came back"

# --- E7: anonymous reads are throttled per address ------------------------------
# Last, because it spends this address's allowance for the next ten seconds.
codes=$(curl -s -o "$DEVNULL" -w '%{http_code}\n' "$BASE/api/public?n=[1-200]")
served=$(echo "$codes" | grep -c '^200$' || true)
throttled=$(echo "$codes" | grep -c '^429$' || true)
[ "$throttled" -gt 0 ] || fail "200 anonymous reads from one address were all served"
[ "$served" -ge 100 ] || fail "too few reads served before throttling ($served)"
ok "anonymous reads are throttled per address ($served served, $throttled refused)"

echo "PASS: all smoke checks succeeded"
