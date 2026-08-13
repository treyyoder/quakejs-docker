FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG QUAKEJS_REPO=https://github.com/nerosketch/quakejs.git
ARG QUAKEJS_REF=master
ARG NODE_MAJOR=22

ENV TZ=UTC

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		apache2 \
		ca-certificates \
		curl \
		git \
		gnupg \
		jq \
		wget \
	&& curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
	&& apt-get install -y --no-install-recommends nodejs \
	&& a2enmod -q proxy proxy_wstunnel rewrite \
	&& rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${QUAKEJS_REF}" "${QUAKEJS_REPO}" /quakejs

WORKDIR /quakejs
RUN jq 'del(.dependencies["quakejs-files"]) | del(.devDependencies["quakejs-files"])' package.json > /tmp/package.json \
	&& mv /tmp/package.json package.json \
	&& npm install --legacy-peer-deps

COPY server.cfg /quakejs/base/baseq3/server.cfg
COPY server.cfg /quakejs/base/cpma/server.cfg
COPY include/ioq3ded/ioq3ded.fixed.js /quakejs/build/ioq3ded.js

RUN rm -f /var/www/html/index.html \
	&& cp -r /quakejs/html/. /var/www/html/ \
	# scheme-relative asset URLs + wss when the page is served over HTTPS
	&& sed -i "s|'http://' + root|'//' + root|g" /var/www/html/ioquake3.js \
	&& sed -i "s|'http://' + fs_cdn|'//' + fs_cdn|g" /var/www/html/ioquake3.js \
	&& sed -i "s|'ws://' + addr|((typeof location !== 'undefined' \&\& location.protocol === 'https:') ? 'wss://' : 'ws://') + addr|g" /var/www/html/ioquake3.js \
	&& grep -q "wss://" /var/www/html/ioquake3.js \
	&& ! grep -q "'http://' + fs_cdn" /var/www/html/ioquake3.js \
	# route websocket upgrades on port 80 to the game server so one published port carries everything
	&& sed -i 's#</VirtualHost>#\tRewriteEngine On\n\tRewriteCond %{HTTP:Upgrade} =websocket [NC]\n\tRewriteRule ^/(.*)$ ws://127.0.0.1:27960/$1 [P,L]\n</VirtualHost>#' /etc/apache2/sites-available/000-default.conf \
	&& apache2ctl configtest
COPY include/assets/ /var/www/html/assets

WORKDIR /
COPY --chmod=755 entrypoint.sh /entrypoint.sh

EXPOSE 80
EXPOSE 27960

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
	CMD bash -c "curl --fail --silent --show-error http://localhost/ > /dev/null \
	&& : < /dev/tcp/127.0.0.1/27960" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
