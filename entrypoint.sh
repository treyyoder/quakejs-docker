#!/bin/sh
set -eu

Q3_FIFO="${Q3_FIFO:-/tmp/q3.fifo}"
Q3_LOG="${Q3_LOG:-/tmp/q3.log}"
Q3_STATE=/tmp/q3.state
# One source of truth: the console only offers maps from this game directory,
# because loading a map the server cannot see crashes it.
FS_GAME="${FS_GAME:-baseq3}"
STATE_DIR="$(dirname "${ADMIN_STATE:-/var/lib/quakejs/admin.json}")"
GAME_LOG="/quakejs/base/$FS_GAME/games.log"
# Past these, the game log is rotated at the next server start (one previous
# copy kept) and the console log is emptied in place; the console reads both
# and copes with either. Apache logs go to the container's own output, where
# Docker's log rotation applies.
Q3_GAME_LOG_MAX="${Q3_GAME_LOG_MAX:-52428800}"
Q3_LOG_MAX="${Q3_LOG_MAX:-20971520}"
export Q3_FIFO Q3_LOG FS_GAME

# Root does one thing here: hands the volumes to quake - they may have been
# created root-owned by an earlier version of this image - and then becomes
# quake for good by exec'ing this same script. Nothing that runs after this
# block, Apache included, has any privilege. Started with --user already,
# there is nothing to hand over and this is skipped.
if [ "$(id -u)" = 0 ]; then
	mkdir -p "$STATE_DIR" /var/www/html/assets
	chown -R quake:quake /var/www/html/assets "$STATE_DIR" /quakejs/base
	exec setpriv --reuid=quake --regid=quake --init-groups env HOME=/home/quake "$0" "$@"
fi

for dir in /var/www/html/assets "$STATE_DIR" /quakejs/base /var/www/html; do
	if ! [ -w "$dir" ]; then
		echo "[entrypoint] $dir is not writable by uid $(id -u), and this container runs unprivileged."
		echo "[entrypoint] Drop --user so the entrypoint can hand the volume over at start, or once:"
		echo "[entrypoint]   docker run --rm -v <volume>:/v ubuntu:22.04 chown -R 1000:1000 /v"
		exit 1
	fi
done
mkdir -p "$STATE_DIR"

# Rewrite the client args so they adapt to whatever origin the browser used:
# assets come from the page's own host:port, and the game connects over the same
# port (Apache proxies websocket upgrades to the internal game server on 27960).
cd /var/www/html
# Stamp the overlay URL with a hash of its contents. Apache serves it without a
# Cache-Control header, so browsers may reuse a cached copy without revalidating,
# and a stale overlay keeps the previous key bindings.
OVERLAY_V="$(python3 -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('/var/www/html/overlay.js').read_bytes()).hexdigest()[:12])")"
sed -i "s#src=\"/overlay.js[^\"]*\"#src=\"/overlay.js?v=$OVERLAY_V\"#" play.html


sed -i "s/'quakejs:80'/window.location.host/g" play.html
sed -i "s/'quakejs:27960'/window.location.hostname + ':' + (window.location.port || (window.location.protocol === 'https:' ? '443' : '80'))/g" play.html

# The client has the same latched 128MB hunk as the server, inside the same
# heap, so large maps exhaust it there too. Runs after the rewrites above so
# the args line is in its final form.
sed -i "s/\['+set', 'fs_cdn', window.location.host/['+set', 'com_hunkMegs', '${Q3_HUNK_MEGS:-256}', '+set', 'fs_cdn', window.location.host/" play.html

# Pull any pak named in EXTRA_PAKS before the manifest is rebuilt, so whatever
# arrives gets indexed with everything else. Licensed content lives here rather
# than in the image, which is why this runs at start instead of at build.
python3 /admin/fetch-paks.py /var/www/html/assets

python3 /admin/sync-assets.py /opt/assets /var/www/html/assets

# Apache in the foreground under this shell, so it ends with the container and
# its logs are the container's. It binds port 80 unprivileged through a file
# capability set at build.
apache2ctl -DFOREGROUND &

# The admin console drives the game server by writing to this FIFO, which is the
# server's console stdin. Hold the write end open ourselves so the server never
# sees EOF between commands.
rm -f "$Q3_FIFO"
mkfifo "$Q3_FIFO"
chmod 600 "$Q3_FIFO"
sleep 2147483647 > "$Q3_FIFO" &

# The game module can die while the emscripten runtime keeps running: overloading
# it with bots raises "G_Alloc: failed", which shuts the game down without node
# ever exiting, so the supervisor below never notices and the container sits
# there answering "Server is not running". Watch the game port instead and end
# the process when it stops listening, which lets the supervisor restart it.
game_pid() {
	for entry in /proc/[0-9]*; do
		if tr '\0' ' ' < "$entry/cmdline" 2>/dev/null | grep -q 'build/ioq3ded.js'; then
			basename "$entry"
			return 0
		fi
	done
	return 1
}

size_of() {
	stat -c %s "$1" 2>/dev/null || echo 0
}

watchdog() {
	while [ "$(cat "$Q3_STATE" 2>/dev/null || echo stop)" = "run" ]; do
		sleep 10
		# The console log grows with every status poll. Past the cap it is
		# emptied in place: it is written through tee -a, so writing carries on
		# from the start rather than leaving a hole, and its readers notice the
		# shrink and start over.
		if [ "$(size_of "$Q3_LOG")" -gt "$Q3_LOG_MAX" ]; then
			: > "$Q3_LOG"
			echo "[watchdog] console log passed $Q3_LOG_MAX bytes; emptied"
		fi
		pid="$(game_pid || true)"
		[ -n "$pid" ] || continue
		# A port check is not enough: when the game module dies the emscripten
		# runtime keeps running and keeps the socket bound, so the port stays
		# open and the container looks healthy while serving nothing. Look for
		# the marker the engine prints instead. The log is truncated on every
		# supervisor start, so any occurrence belongs to the running instance.
		if grep -q "Server crashed:" "$Q3_LOG" 2>/dev/null; then
			echo "[watchdog] game server crashed; restarting it"
			kill "$pid" 2>/dev/null || true
			sleep 10
		fi
	done
}

echo run > "$Q3_STATE"
watchdog &

# The console is supervised like the game server: if it ever dies, the
# healthcheck sees it (it asks the console too), and this brings it back.
console() {
	while [ "$(cat "$Q3_STATE" 2>/dev/null || echo stop)" = "run" ]; do
		python3 /admin/server.py || true
		[ "$(cat "$Q3_STATE" 2>/dev/null || echo stop)" = "run" ] || break
		echo "[entrypoint] admin console stopped; restarting"
		sleep 2
	done
}

# Unset and empty differ: unset, the console generates a password on first
# start and prints it (see server.py); empty means no console at all.
if [ -n "${ADMIN_PASSWORD+set}" ] && [ -z "$ADMIN_PASSWORD" ]; then
	echo "[entrypoint] ADMIN_PASSWORD is empty - admin console disabled"
else
	console &
fi

running=1
trap 'running=0; echo stop > "$Q3_STATE"; kill 0 2>/dev/null || true; exit 0' TERM INT

# Supervise the game server: installing a map means restarting it, and the
# watchdog ends a wedged one so it comes back here.
cd /quakejs
while [ "$running" -eq 1 ]; do
	# Fold console-set settings and rotation into the config the server reads.
	python3 /admin/build-config.py \
		"/quakejs/base/$FS_GAME/server.cfg.base" \
		"$STATE_DIR" \
		"/quakejs/base/$FS_GAME/server.cfg"
	# The engine has its game log closed between runs, so this is when it can
	# be rotated. One previous copy is kept.
	if [ "$(size_of "$GAME_LOG")" -gt "$Q3_GAME_LOG_MAX" ]; then
		mv -f "$GAME_LOG" "$GAME_LOG.1"
		echo "[entrypoint] games.log passed $Q3_GAME_LOG_MAX bytes; rotated"
	fi
	: > "$Q3_LOG"
	# com_hunkMegs is latched: the engine allocates the hunk before it execs any
	# config, so setting it in server.cfg is ignored and large maps still die with
	# "Hunk_Alloc failed". It has to be given on the command line.
	node build/ioq3ded.js +set com_hunkMegs "${Q3_HUNK_MEGS:-256}" \
		+set fs_cdn localhost:80 +set fs_game "$FS_GAME" \
		+set dedicated 1 +exec server.cfg < "$Q3_FIFO" 2>&1 | tee -a "$Q3_LOG" || true
	[ "$running" -eq 1 ] || break
	echo "[entrypoint] game server stopped; restarting"
	sleep 2
done
