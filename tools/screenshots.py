#!/usr/bin/env python3
"""Screenshots of the front page and every console tab, for the README.

Run in Microsoft's Playwright image against a running server - a local
container will do - and the pictures land in docs/screenshots. The image
carries the browsers but not the Python package, so that is installed on
the way in, pinned to the browsers' own version:

    MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/w" -w /w \\
      -e BASE=http://host.docker.internal:18080 -e ADMIN_PASSWORD=... \\
      mcr.microsoft.com/playwright/python:v1.47.0-jammy \\
      sh -c 'pip install -q playwright==1.47.0 && python3 tools/screenshots.py docs/screenshots'

It signs in, puts a little life on the server - three bots, a few lines of
chat, a timed ban - takes the pictures, and takes the life away again.
Rerun it after any change to the console so the guide matches.
"""

import base64
import json
import os
import pathlib
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://localhost:18080").rstrip("/")
PASSWORD = os.environ["ADMIN_PASSWORD"]
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots")
VIEWPORT = {"width": 1280, "height": 860}
TABS = ("chat", "stats", "server", "match", "players", "maps", "log")
# SHOTS="front-page console-maps" takes only those; empty takes them all. The
# chat lines are only posted when a chat picture is wanted - they show up in
# game, and a front-page retake should not talk to whoever is playing.
ONLY = set(os.environ.get("SHOTS", "").split())


def wanted(name):
    return not ONLY or name in ONLY


def api(path, body=None, auth=True):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
    request = urllib.request.Request(BASE + "/admin" + path, data=data,
                                     method="POST" if data is not None else "GET", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


# A leaderboard worth looking at, for the Stats picture. It goes in through
# the console's own import and the real one goes back the same way after.
MOCK_PLAYERS = {
    "Trey":        {"kills": 412, "deaths": 233, "suicides": 9,  "matches": 38, "best": 31},
    "rocketjump":  {"kills": 377, "deaths": 251, "suicides": 14, "matches": 35, "best": 28},
    "Nyx":         {"kills": 298, "deaths": 190, "suicides": 3,  "matches": 27, "best": 25},
    "railgod":     {"kills": 256, "deaths": 204, "suicides": 2,  "matches": 24, "best": 22},
    "quad_damage": {"kills": 201, "deaths": 240, "suicides": 21, "matches": 30, "best": 19},
    "Mabel":       {"kills": 164, "deaths": 121, "suicides": 4,  "matches": 15, "best": 24},
    "fragbait":    {"kills": 97,  "deaths": 288, "suicides": 17, "matches": 29, "best": 12},
    "spawnfrag":   {"kills": 88,  "deaths": 130, "suicides": 6,  "matches": 12, "best": 15},
    "lagg":        {"kills": 41,  "deaths": 96,  "suicides": 11, "matches": 9,  "best": 9},
}
_real_stats = {}


def seed():
    """A server worth photographing."""
    bots = api("/api/state")["bots"]
    for name in bots[:3]:
        api("/api/bot", {"name": name, "skill": 3, "count": 1})
    if wanted("console-chat"):
        api("/api/say", {"message": "Welcome - the console can talk to the server too."})
        api("/api/chat", {"name": "Trey", "message": "Anyone up for CTF later?"}, auth=False)
        api("/api/chat", {"name": "Alex", "message": "Give me ten minutes"}, auth=False)
    if wanted("console-players"):
        api("/api/ban", {"ip": "203.0.113.9", "reason": "spawn camping", "hours": 24})
    if wanted("console-stats"):
        _real_stats.update(api("/api/export")["stats"])
        now = int(time.time())
        rows = {name: dict(row, bot=False, seen=now - 3600 * i)
                for i, (name, row) in enumerate(MOCK_PLAYERS.items())}
        api("/api/import", {"format": "quakejs-state/1", "stats": {"players": rows}})
    time.sleep(4)   # bots connect, chat lands in the log and is picked up


def unseed():
    if _real_stats:
        api("/api/import", {"format": "quakejs-state/1", "stats": _real_stats})
    try:
        api("/api/unban", {"ip": "203.0.113.9"})
    except Exception:
        pass
    for player in api("/api/state")["players"]:
        if player["bot"]:
            api("/api/kick", {"num": player["num"]})


def shoot(page, name):
    if not wanted(name):
        return
    page.screenshot(path=str(OUT / f"{name}.png"))
    print(f"  {name}.png")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    seed()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport=VIEWPORT, device_scale_factor=1).new_page()

            page.goto(BASE + "/")
            page.wait_for_function("document.getElementById('map').textContent.trim() !== '…'")
            # The server name arrives with the console's first serverinfo read,
            # which can lag the roster by a poll right after a restart.
            try:
                page.wait_for_selector("#hostname:not([hidden])", timeout=30000)
            except Exception:
                pass
            page.fill("#name", "Trey")    # the Play-as field, in use
            page.wait_for_timeout(1500)
            shoot(page, "front-page")

            if ONLY and not any(name.startswith("console-") for name in ONLY):
                browser.close()
                return

            page.goto(BASE + "/admin/")
            page.wait_for_timeout(2500)
            page.fill("#myname", "Trey")
            shoot(page, "console-chat-public")

            page.click("#adminlogin")
            page.wait_for_timeout(300)
            shoot(page, "console-sign-in")
            page.fill("#pass", PASSWORD)
            page.click("#signinbtn")
            page.wait_for_selector("#tab-server:not([hidden])")
            page.wait_for_timeout(2500)

            for tab in TABS:
                page.click(f'.tabs button[data-tab="{tab}"]')
                page.wait_for_timeout(3000 if tab in ("chat", "players", "maps") else 2000)
                shoot(page, f"console-{tab}")
            browser.close()
    finally:
        unseed()


if __name__ == "__main__":
    main()
