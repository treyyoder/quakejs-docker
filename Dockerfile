# Pinned by the digest of the manifest list, which covers every architecture
# in it, so the same tag cannot quietly become a different image. Bump it
# deliberately: docker buildx imagetools inspect ubuntu:22.04
FROM ubuntu:22.04@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc

ARG DEBIAN_FRONTEND=noninteractive
ARG QUAKEJS_REPO=https://github.com/nerosketch/quakejs.git
# Pinned to a commit rather than a branch: a change upstream must not change
# this image silently on the next build. Move it deliberately.
ARG QUAKEJS_REF=00230a844d64ac6158af8f129411ddd4e621e3a2
# Node comes from the official tarball for the architecture being built,
# checked against the sha256 nodejs.org publishes for it, rather than a setup
# script piped to a root shell. Bump all three together; the sums are in
# https://nodejs.org/dist/v<version>/SHASUMS256.txt.
ARG NODE_VERSION=22.23.2
ARG NODE_SHA256_AMD64=d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307
ARG NODE_SHA256_ARM64=fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8
# Set by buildx to the platform being built (amd64, arm64); plain docker
# build leaves it empty, and that means amd64 below.
ARG TARGETARCH

ENV TZ=UTC
# The mod the server runs. The admin console scopes its map catalog to this,
# because loading a map from another game directory crashes the server.
ENV FS_GAME=baseq3
# Console user. No password is baked in: set ADMIN_PASSWORD at run time, or
# leave it unset and the console generates one on first start and prints it
# to the log. An empty ADMIN_PASSWORD disables the console.
ENV ADMIN_USER=admin

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		apache2 \
		ca-certificates \
		curl \
		git \
		jq \
		libcap2-bin \
		python3 \
		xz-utils \
	&& a2enmod -q proxy proxy_http proxy_wstunnel rewrite \
	&& rm -rf /var/lib/apt/lists/* \
	&& case "${TARGETARCH:-amd64}" in \
		amd64) NODE_ARCH=x64; NODE_SHA256="${NODE_SHA256_AMD64}" ;; \
		arm64) NODE_ARCH=arm64; NODE_SHA256="${NODE_SHA256_ARM64}" ;; \
		*) echo "no node build pinned for ${TARGETARCH}"; exit 1 ;; \
	esac \
	&& curl -fsSLo /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" \
	&& echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
	&& tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
	&& rm /tmp/node.tar.xz \
	&& node --version && npm --version

# Runtime configuration. Kept below the package install on purpose: an ENV
# above it invalidates that layer, so editing a default here would otherwise
# mean reinstalling every package.
# Reverse proxies that sit between the browser and this container, as IPs or
# CIDRs. Players arrive through the bundled Apache, so the game server takes
# their real address from X-Forwarded-For; addresses listed here are skipped
# while walking that header, because a hop one of our own proxies appended is
# never the client. Leave empty when this container is reached directly.
ENV TRUSTED_PROXIES=""
# Optional Discord or Slack incoming webhook. When a real player joins a
# server that had nobody on it, the console posts there once, and then not
# again for NOTIFY_COOLDOWN seconds, so a quiet server cannot spam a channel.
ENV NOTIFY_WEBHOOK=""
# Paks to pull at start rather than ship. Quake 3's retail pak0.pk3 is licensed
# content that must not be baked into a published image, so operators point this
# at their own copy instead. Whitespace-separated, each entry optionally
# 'name.pk3=' prefixed and '#sha256=...' suffixed. See admin/fetch-paks.py.
ENV EXTRA_PAKS=""

# git clone --branch takes a branch or tag, not a commit; fetching the commit
# by hash is what actually pins it.
RUN git init -q /quakejs \
	&& git -C /quakejs fetch -q --depth 1 "${QUAKEJS_REPO}" "${QUAKEJS_REF}" \
	&& git -C /quakejs checkout -q FETCH_HEAD \
	&& rm -rf /quakejs/.git

WORKDIR /quakejs
RUN jq 'del(.dependencies["quakejs-files"]) | del(.devDependencies["quakejs-files"])' package.json > /tmp/package.json \
	&& mv /tmp/package.json package.json \
	&& npm install --legacy-peer-deps

# The game server takes packets from players and the console takes HTTP from
# anyone, so neither runs as root. Only what they write is theirs: the game
# directory the engine unpacks paks into, and the two volumes. Apache keeps
# its own model - a root master that binds 80 and drops workers to www-data.
RUN useradd --system --uid 1000 --user-group --create-home --home-dir /home/quake --shell /usr/sbin/nologin quake \
	&& mkdir -p /var/www/html/assets /var/lib/quakejs \
	&& chown -R quake:quake /quakejs/base /var/www/html/assets /var/lib/quakejs

COPY server.cfg /quakejs/base/baseq3/server.cfg.base
COPY server.cfg /quakejs/base/cpma/server.cfg
COPY include/ioq3ded/ioq3ded.fixed.js /quakejs/build/ioq3ded.js
# The dedicated server has the same fixed 224MB heap as the client, and its
# 128MB hunk lives inside it. Large maps do not fit: rustgrad (65MB unpacked)
# kills the server with "Hunk_Alloc failed", which closes the game port and
# leaves every client reconnect refused. Raise the heap so the hunk can grow.
RUN sed -i "s/|| 234881024/|| 536870912/" /quakejs/build/ioq3ded.js \
	&& grep -q "|| 536870912" /quakejs/build/ioq3ded.js \
	# the vendored server carries our patch for real client addresses; without
	# it every player looks like the proxy and one ban would remove them all
	&& grep -q "realClientAddress" /quakejs/build/ioq3ded.js \
	&& node --check /quakejs/build/ioq3ded.js

RUN rm -f /var/www/html/index.html \
	&& cp -r /quakejs/html/. /var/www/html/ \
	# The game page becomes /play.html, in the same directory so its relative
	# references still resolve; the front page (admin/landing) takes / below.
	&& mv /var/www/html/index.html /var/www/html/play.html \
	# scheme-relative asset URLs + wss when the page is served over HTTPS
	&& sed -i "s|'http://' + root|'//' + root|g" /var/www/html/ioquake3.js \
	&& sed -i "s|'http://' + fs_cdn|'//' + fs_cdn|g" /var/www/html/ioquake3.js \
	&& sed -i "s|'ws://' + addr|((typeof location !== 'undefined' \&\& location.protocol === 'https:') ? 'wss://' : 'ws://') + addr|g" /var/www/html/ioquake3.js \
	&& grep -q "wss://" /var/www/html/ioquake3.js \
	&& ! grep -q "'http://' + fs_cdn" /var/www/html/ioquake3.js \
	# always fetch assets from the page's own origin: the server replicates its own
	# fs_cdn (localhost:80, correct only inside the container) through systeminfo,
	# which otherwise breaks any pk3 downloaded after connecting - i.e. every custom map
	&& sed -i "s|return Pointer_stringify(_Com_GetCDN());|return window.location.host;|" /var/www/html/ioquake3.js \
	&& sed -i "s|var url = '//' + fs_cdn + '/assets/manifest.json';|var url = '//' + window.location.host + '/assets/manifest.json';|" /var/www/html/ioquake3.js \
	&& ! grep -q "_Com_GetCDN());" /var/www/html/ioquake3.js \
	&& ! grep -q "'//' + fs_cdn" /var/www/html/ioquake3.js \
	# one published port carries the page, the assets, the game, and the console
	&& sed -i 's#</VirtualHost>#\tRewriteEngine On\n\tRewriteCond %{HTTP:Upgrade} =websocket [NC]\n\tRewriteCond %{REQUEST_URI} !^/admin\n\tRewriteRule ^/(.*)$ ws://127.0.0.1:27960/$1 [P,L]\n\tRedirectMatch ^/admin$ /admin/\n\tProxyPass /admin/ http://127.0.0.1:8092/\n\tProxyPassReverse /admin/ http://127.0.0.1:8092/\n</VirtualHost>#' /etc/apache2/sites-available/000-default.conf \
	&& sed -i 's#<title>QuakeJS Local</title>#<title>QuakeJS-Docker</title>#' /var/www/html/play.html \
	&& grep -q '<title>QuakeJS-Docker</title>' /var/www/html/play.html \
	# The client runs in a fixed heap that emscripten compiled in years ago at
	# 224MB, which is not enough for large maps: rustgrad unpacks to 65MB and
	# trespass to 45MB on top of the base assets and every decompressed
	# texture, and running out aborts the load with no network error. The
	# module honours a Module object defined before it loads.
	&& sed -i 's#<script type="text/javascript" src="ioquake3.js">#<script>var Module={TOTAL_MEMORY:536870912};</script><script type="text/javascript" src="ioquake3.js">#' /var/www/html/play.html \
	&& grep -q "TOTAL_MEMORY:536870912" /var/www/html/play.html \
	# The launch arguments - saved name, query commands, connect last - are
	# assembled by admin/landing/play-launch.js; the page hands it what it built.
	&& sed -i 's#<script>var Module={TOTAL_MEMORY#<script src="/play-launch.js"></script><script>var Module={TOTAL_MEMORY#' /var/www/html/play.html \
	&& sed -i 's#args.push.apply(args, getQueryCommands());#var extra = getQueryCommands(); args = window.quakejsLaunch ? window.quakejsLaunch(args, extra) : args.concat(extra);#' /var/www/html/play.html \
	&& grep -q 'play-launch.js' /var/www/html/play.html \
	&& grep -q 'window.quakejsLaunch(args, extra)' /var/www/html/play.html \
	# the overlay claims the backquote key in the game page and opens the console
	&& sed -i 's#</body>#<script src="/overlay.js" defer></script></body>#' /var/www/html/play.html \
	&& grep -q 'overlay.js' /var/www/html/play.html \
	# No version banners, no MIME sniffing, no framing from another site. The
	# console's own page carries a fuller policy: setifempty leaves it alone, and
	# only without 'always', which would consult a table a proxied header is
	# never in and stack a second policy on top. nosniff and the frame option
	# are 'always' so Apache's own error pages carry them too.
	&& sed -i 's/^ServerTokens .*/ServerTokens Prod/; s/^ServerSignature .*/ServerSignature Off/' /etc/apache2/conf-available/security.conf \
	&& grep -q '^ServerTokens Prod' /etc/apache2/conf-available/security.conf \
	&& printf '%s\n' 'ServerName localhost' 'Header always set X-Content-Type-Options "nosniff"' 'Header always set X-Frame-Options "SAMEORIGIN"' "Header setifempty Content-Security-Policy \"frame-ancestors 'self'\"" > /etc/apache2/conf-available/quakejs-headers.conf \
	&& a2enmod -q headers && a2enconf -q quakejs-headers \
	# Apache runs unprivileged: the binary carries the one capability port 80
	# needs, its user is quake, and the directories it writes are quake's. Its
	# logs go to the container's own output instead of files that grow forever -
	# through a piped logger, because an unprivileged Apache cannot reopen the
	# container's own stderr by path, but a child of it inherits it.
	&& setcap 'cap_net_bind_service=+ep' /usr/sbin/apache2 \
	&& sed -i 's/^export APACHE_RUN_USER=.*/export APACHE_RUN_USER=quake/; s/^export APACHE_RUN_GROUP=.*/export APACHE_RUN_GROUP=quake/' /etc/apache2/envvars \
	&& grep -q '^export APACHE_RUN_USER=quake' /etc/apache2/envvars \
	&& sed -i 's#ErrorLog ${APACHE_LOG_DIR}/error.log#ErrorLog "||/bin/cat"#' /etc/apache2/apache2.conf /etc/apache2/sites-available/000-default.conf \
	&& sed -i 's#CustomLog ${APACHE_LOG_DIR}/access.log combined#CustomLog "||/bin/cat" combined#' /etc/apache2/sites-available/000-default.conf \
	&& grep -q 'ErrorLog "||/bin/cat"' /etc/apache2/apache2.conf \
	&& grep -q 'CustomLog "||/bin/cat" combined' /etc/apache2/sites-available/000-default.conf \
	&& a2disconf -q other-vhosts-access-log \
	&& mkdir -p /var/run/apache2 /var/lock/apache2 /var/log/apache2 \
	&& chown -R quake:quake /var/run/apache2 /var/lock/apache2 /var/log/apache2 \
	&& apache2ctl configtest

# Two fixes the vendored server and client both need before a server can use
# base paks its owner supplied instead of the bundled demo content: skipping an
# installer that would overwrite them, and truncating a large file without
# copying it into a per-byte JS array first. See tools/patch-quakejs.py.
COPY tools/patch-quakejs.py /tools/patch-quakejs.py
RUN python3 /tools/patch-quakejs.py \
	/quakejs/build/ioq3ded.js /var/www/html/ioquake3.js \
	&& grep -qF "installer.paks[s].dest" /quakejs/build/ioq3ded.js \
	&& grep -qF "installer.paks[s].dest" /var/www/html/ioquake3.js \
	&& grep -qF "Fast path for truncating to nothing" /quakejs/build/ioq3ded.js \
	&& grep -qF "Fast path for truncating to nothing" /var/www/html/ioquake3.js \
	# the error path referenced a callback that was never in scope
	&& ! grep -qF "return callback(new Error('Failed to find" /quakejs/build/ioq3ded.js \
	&& node --check /quakejs/build/ioq3ded.js
COPY admin/overlay.js /var/www/html/overlay.js
# The front page: server name, map, who is on, the leaderboard, a Join button.
COPY admin/landing/ /var/www/html/
# Bundled assets stay in the image; the entrypoint materialises the served tree
# from here so a volume can hold console-installed maps without freezing these.
COPY include/assets/ /opt/assets/

WORKDIR /
COPY admin/ /admin/
COPY tools/ /tools/
# The console's pure parts - parsers, the settings spec, the log follower - are
# unit-tested against this image's own Python, so a regression cannot build.
# The modules are compiled here as root, because the user that runs them
# cannot write bytecode beside them; and the page is handed to that user,
# which rewrites it for its origin at every start.
RUN python3 /tools/test-admin.py \
	&& python3 -m compileall -q /admin /tools \
	&& chmod 755 /tools/healthcheck.sh \
	&& test -f /var/www/html/play.html && grep -q 'id="join"' /var/www/html/index.html \
	&& chown -R quake:quake /var/www/html
COPY --chmod=755 entrypoint.sh /entrypoint.sh

EXPOSE 80
EXPOSE 27960

# Page, game port, no crash marker, and the console unless it is disabled;
# see the script. A dead console used to read as healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
	CMD ["/tools/healthcheck.sh"]

ENTRYPOINT ["/entrypoint.sh"]
