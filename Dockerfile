FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG QUAKEJS_REPO=https://github.com/nerosketch/quakejs.git
ARG QUAKEJS_REF=master
ARG NODE_MAJOR=22

ENV TZ=UTC
ENV HTTP_PORT=8080

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
	&& cp -r /quakejs/html/. /var/www/html/
COPY include/assets/ /var/www/html/assets

WORKDIR /
COPY --chmod=755 entrypoint.sh /entrypoint.sh

EXPOSE 80
EXPOSE 27960

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
	CMD bash -c "curl --fail --silent --show-error http://localhost/ > /dev/null \
	&& : < /dev/tcp/127.0.0.1/27960" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
