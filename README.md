<div align="center">

![logo](https://github.com/treyyoder/quakejs-docker/blob/master/quakejs-docker.png?raw=true)
# quakejs-docker

![Docker Image CI](https://github.com/treyyoder/quakejs-docker/actions/workflows/dockerimage.yml/badge.svg)
</div>

Fully local and Dockerized QuakeJS server. This project bundles assets and server binaries so gameplay does not depend on content.quakejs.com.

## Quick Start (Docker Compose)

From the repository root:

```bash
docker compose up --build -d
```

Then open:

```text
http://localhost:8080
```

To stop:

```bash
docker compose down
```

## Quick Start (Docker Run)

Build locally:

```bash
docker build -t treyyoder/quakejs:latest .
```

Run:

```bash
docker run -d --name quakejs -p 8080:80 treyyoder/quakejs:latest
```

## Configuration

- A single published port carries everything: the page, the game assets, and the game itself. The client adapts to whatever origin the browser used (any host, any port, HTTP or HTTPS), and Apache inside the container proxies websocket upgrades to the internal game server on 27960 — so no second port mapping is needed and the game works behind a TLS-terminating reverse proxy over `wss://`.
- The `HTTP_PORT` environment variable from older versions is gone; no configuration is needed for the page to find its server.
- Main game settings live in [server.cfg](server.cfg).
- Remote administration is disabled by default. Set a strong `rconpassword` in [server.cfg](server.cfg) before enabling it.
- See Quake 3 server variable reference at https://www.quake3world.com/q3guide/servers.html.

## Non-Docker Testing

You can run this project without Docker by reproducing the container steps:

1. Clone https://github.com/nerosketch/quakejs and run npm install.
2. Copy [server.cfg](server.cfg) into both quakejs/base/baseq3/server.cfg and quakejs/base/cpma/server.cfg.
3. Copy [include/ioq3ded/ioq3ded.fixed.js](include/ioq3ded/ioq3ded.fixed.js) to quakejs/build/ioq3ded.js.
4. Copy [include/assets](include/assets) into quakejs/html/assets.
5. Update quakejs/html/index.html host/port in the same way [entrypoint.sh](entrypoint.sh) does.
6. Serve quakejs/html on your desired HTTP port and run:

```bash
node build/ioq3ded.js +set fs_cdn localhost:8080 +set fs_game baseq3 +set dedicated 1 +exec server.cfg
```

## Notes

- This repo targets current Docker Compose syntax (no compose file version key).
- The container installs Node 22 by default (configurable via Docker build argument NODE_MAJOR).
- The game server listens on TCP 27960 inside the container (QuakeJS clients connect over WebSockets), but browser traffic reaches it through the published HTTP port via Apache's websocket proxy. Publish `-p 27960:27960` additionally only if something needs to hit the game socket directly.

## Credits

Thanks to begleysm and their QuakeJS fork/documentation:

- https://github.com/begleysm/quakejs
- https://steamforge.net/wiki/index.php/How_to_setup_a_local_QuakeJS_server_under_Debian_9_or_Debian_10
