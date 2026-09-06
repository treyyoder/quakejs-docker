"""Paks, maps, bots, and installs - from lvlworld or from an upload.

The catalog is scoped to the game directory the server runs (config.FS_GAME):
paks outside it are not loaded, and asking for a map from one crashes the
server outright. Every scan of the paks is cached against their names and
mtimes, because the console polls constantly and a pak is a zip to open.
"""

import hashlib
import http.cookiejar
import io
import json
import pathlib
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib

from . import config
from . import settings

_map_cache = {"stamp": None, "maps": []}
_bot_cache = {"stamp": None, "bots": []}
_arena_cache = {"stamp": None, "arenas": {}}
_usage_cache = {"stamp": None, "data": {"bytes": 0, "paks": 0}}
_removable_cache = {"stamp": None, "data": []}
_manifest_lock = threading.Lock()
# The most any one text or image entry read out of a pak may unpack to. A pak
# is a zip, and a zip may declare a member that inflates to gigabytes; nothing
# the console reads out of one - a bot list, an arena file, a levelshot - is
# anywhere near this.
MEMBER_LIMIT = 8 * 1024 * 1024


def _paks():
    """Paks the running server can actually load: the active game dir only."""
    return (sorted((config.ASSETS / config.FS_GAME).glob("*.pk3"))
            + sorted(config.BASE_PAKS.glob("*.pk3")))


def _stamp(paks):
    return tuple((p.name, p.stat().st_mtime_ns) for p in paks)


def forget():
    """Drop every cache; the paks on disk have changed."""
    for cache in (_map_cache, _bot_cache, _arena_cache, _usage_cache, _removable_cache):
        cache["stamp"] = None


def _read_member(archive, name, limit=MEMBER_LIMIT):
    """One entry out of a pak, refused before a byte is inflated if it would
    unpack past the limit. The size in the central directory bounds what
    zipfile will hand back, so checking it is checking the read."""
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise ValueError(f"{name} would unpack to {info.file_size >> 20} MiB, "
                         f"over the {limit >> 20} MiB limit")
    return archive.read(name)


# ------------------------------------------------------------------ catalog
def available_bots():
    """Bots this bundle actually defines.

    The demo assets ship far fewer than retail Quake 3 (six, not thirty-odd), so
    this is read from scripts/bots.txt rather than assumed; addbot rejects
    anything undefined with "Bot '<name>' not defined".
    """
    paks = _paks()
    stamp = _stamp(paks)
    if _bot_cache["stamp"] == stamp:
        return _bot_cache["bots"]
    names = []
    for pak in paks:
        try:
            with zipfile.ZipFile(pak) as archive:
                for entry in archive.namelist():
                    if entry.lower().endswith("scripts/bots.txt"):
                        text = _read_member(archive, entry).decode("latin-1")
                        names += config.BOT_NAME_RE.findall(text)
        except (zipfile.BadZipFile, OSError, KeyError, ValueError):
            continue
    unique = sorted({n.strip() for n in names if n.strip()}, key=str.lower)
    _bot_cache.update(stamp=stamp, bots=unique)
    return unique


def installed_maps():
    """Every map name playable from the bundled assets and base paks."""
    paks = _paks()
    stamp = _stamp(paks)
    if _map_cache["stamp"] == stamp:
        return _map_cache["maps"]
    names = set()
    for pak in paks:
        try:
            with zipfile.ZipFile(pak) as archive:
                for entry in archive.namelist():
                    low = entry.lower()
                    if low.startswith("maps/") and low.endswith(".bsp"):
                        names.add(pathlib.PurePath(low).stem)
        except (zipfile.BadZipFile, OSError):
            continue
    _map_cache.update(stamp=stamp, maps=sorted(names))
    return _map_cache["maps"]


def map_gametypes():
    """map name -> gametypes its arena file declares, so the UI can filter."""
    paks = _paks()
    stamp = _stamp(paks)
    if _arena_cache["stamp"] == stamp:
        return _arena_cache["arenas"]
    arenas = {}
    for pak in paks:
        try:
            archive = zipfile.ZipFile(pak)
        except (zipfile.BadZipFile, OSError):
            continue
        for entry in archive.namelist():
            low = entry.lower()
            if not (low.endswith(".arena") or low.endswith("arenas.txt")):
                continue
            try:
                text = _read_member(archive, entry).decode("latin-1")
            except (OSError, zipfile.BadZipFile, KeyError, ValueError):
                continue
            for block in re.findall(r"\{([^}]*)\}", text):
                name = re.search(r'^\s*map\s+"?([^"\s]+)"?', block, re.M)
                if not name:
                    continue
                kinds = re.search(r'^\s*type\s+"?([^"\n]*)"?', block, re.M)
                types = sorted(set(re.findall(r"[a-z]+", kinds.group(1).lower()))) if kinds else []
                longname = re.search(r'^\s*longname\s+"([^"]*)"', block, re.M)
                arenas[name.group(1).lower()] = {
                    "types": types,
                    "longname": longname.group(1) if longname else None,
                }
    _arena_cache.update(stamp=stamp, arenas=arenas)
    return arenas


def levelshot(name):
    """(bytes, content-type) for a map preview image, or None."""
    for pak in _paks():
        try:
            archive = zipfile.ZipFile(pak)
        except (zipfile.BadZipFile, OSError):
            continue
        for entry in archive.namelist():
            low = entry.lower()
            if not low.startswith("levelshots/"):
                continue
            if pathlib.PurePath(low).stem != name.lower():
                continue
            try:
                if low.endswith((".jpg", ".jpeg")):
                    return _read_member(archive, entry), "image/jpeg"
                if low.endswith(".png"):
                    return _read_member(archive, entry), "image/png"
            except ValueError:
                continue   # an absurd levelshot is no levelshot
    return None  # some maps ship .tga levelshots, which browsers cannot display


def is_base_pak(filename):
    """Whether a pak is the game's own content rather than an installed map.

    These are supplied by whoever runs the server - uploaded, or fetched through
    EXTRA_PAKS - so they do not live in the image and the bundled check below
    does not cover them. Without this, uninstalling any one stock map would
    delete the whole of pak0.pk3 and take every map, bot and texture with it.
    """
    stem = filename.split("-", 1)[-1] if "-" in filename else filename
    return bool(config.BASE_PAK_RE.match(stem))


def bundled_map_files():
    """pk3 filenames shipped in the image; these may not be uninstalled."""
    source = config.BUNDLED_ASSETS / config.FS_GAME
    return {p.name for p in source.glob("*.pk3")} if source.is_dir() else set()


def pk3_for_map(name):
    """The installed pk3 providing a map, or None."""
    for pak in sorted((config.ASSETS / config.FS_GAME).glob("*.pk3")):
        try:
            with zipfile.ZipFile(pak) as archive:
                for entry in archive.namelist():
                    low = entry.lower()
                    if (low.startswith("maps/") and low.endswith(".bsp")
                            and pathlib.PurePath(low).stem == name.lower()):
                        return pak
        except (zipfile.BadZipFile, OSError):
            continue
    return None


def removable_maps(maps):
    paks = sorted((config.ASSETS / config.FS_GAME).glob("*.pk3"))
    stamp = _stamp(paks)
    if _removable_cache["stamp"] == stamp:
        return _removable_cache["data"]
    bundled = bundled_map_files()
    owner = {}
    for pak in paks:
        try:
            with zipfile.ZipFile(pak) as archive:
                for entry in archive.namelist():
                    low = entry.lower()
                    if low.startswith("maps/") and low.endswith(".bsp"):
                        owner.setdefault(pathlib.PurePath(low).stem, pak.name)
        except (zipfile.BadZipFile, OSError):
            continue
    data = sorted(m for m in maps if owner.get(m) and owner[m] not in bundled
                  and not is_base_pak(owner[m]))
    _removable_cache.update(stamp=stamp, data=data)
    return data


def assets_usage():
    total = sum(p.stat().st_size for p in config.ASSETS.rglob("*") if p.is_file())
    return {"bytes": total, "paks": len(list((config.ASSETS / config.FS_GAME).glob("*.pk3")))}


def cached_usage():
    paks = sorted((config.ASSETS / config.FS_GAME).glob("*.pk3"))
    stamp = _stamp(paks)
    if _usage_cache["stamp"] == stamp:
        return _usage_cache["data"]
    data = assets_usage()
    _usage_cache.update(stamp=stamp, data=data)
    return data


# ----------------------------------------------------------------- lvlworld
# The download page offers the file from more than one host: lvlworld's own
# and the FSS mirror (and an FTP one, MHG, that browsers dropped and so does
# this). Its links run location="/dl/"+s+"/<token path>" with s the link's
# data-dl, and the token is minted for the visit that loaded the page, so the
# file must be fetched in the same session. lvlworld's own host has taken to
# refusing downloads with a 403 that blames "a direct link" - browsers
# included, coming from the page itself - while the mirror serves the very
# file the page's sha256 vouches for.
MIRRORS_SKIPPED = frozenset({"MHG"})
MIRROR_RETRY = 600.0     # a mirror that refused is tried last for this long
_mirror_down = {}        # mirror key -> when it last refused


def _fetch(url, referer=None, limit=None, jar=None, verify=True):
    """One GET. With a cookie jar the request belongs to a session; with
    verify off the transport's certificate is not checked - only ever for a
    mirror download the page has published a sha256 for, see _mirror_blob."""
    request = urllib.request.Request(url, headers={"User-Agent": config.UA})
    if referer:
        request.add_header("Referer", referer)
    handlers = []
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    if not verify:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    with urllib.request.build_opener(*handlers).open(request, timeout=120) as response:
        return response.read(limit) if limit else response.read()


def parse_lvl_page(page, map_id):
    """What a map's download page says: file name and size, the sha256 it
    publishes, the per-visit token path, and the mirrors on offer."""
    flat = re.sub(r"<[^>]*>", "|", page)

    def field(label):
        found = re.search(label + r":\|*([^|]+)", flat)
        return found.group(1).strip() if found else None

    token = re.search(r'/dl/"\+s\+"/([0-9a-f/]+)', page)
    sha = re.search(r"\b[0-9a-f]{64}\b", page)
    filename = field("Filename")
    mirrors = [m for m in dict.fromkeys(re.findall(r'data-dl="([^"]+)"', page))
               if m not in MIRRORS_SKIPPED]
    if not (token and filename and mirrors):
        raise ValueError(f"lvlworld has no downloadable file for id {map_id}")
    path = token.group(1)
    return {
        "id": map_id,
        "filename": filename,
        "filesize": field("Filesize"),
        "sha256": sha.group(0) if sha else None,
        "mirrors": mirrors,
        "urls": {m: f"{config.LVL_BASE}/dl/{m}/{path}" for m in mirrors},
        "url": f"{config.LVL_BASE}/dl/{mirrors[0]}/{path}",
        "referer": f"{config.LVL_BASE}/download/id:{map_id}",
        "page": f"{config.LVL_BASE}/review/id:{map_id}",
    }


def _lvl_session(ref):
    """(metadata, cookie jar) for a map. The page is read into the jar, and
    the download goes out with it: the token on the page belongs to that
    visit."""
    match = re.search(r"(\d{1,6})", str(ref))
    if not match:
        raise ValueError("expected an lvlworld map id or URL")
    map_id = match.group(1)
    jar = http.cookiejar.CookieJar()
    page = _fetch(f"{config.LVL_BASE}/download/id:{map_id}", jar=jar)
    return parse_lvl_page(page.decode("utf-8", "replace"), map_id), jar


def lvl_lookup(ref):
    """Resolve an lvlworld id/URL to download metadata."""
    return _lvl_session(ref)[0]


class MirrorRefused(Exception):
    """One mirror did not hand over the file; the message says why."""


def _mirror_blob(meta, key, jar):
    """The bytes one mirror serves, or MirrorRefused.

    The FSS mirror serves its certificate without the intermediate that links
    it to a trusted root, so the chain cannot be built here - a browser goes
    and fetches the missing link, Python does not. When the page published a
    sha256 for the file, the download is retried without checking the
    transport: the hash came over a verified connection to lvlworld and is
    checked before the file is installed, so a substituted file is refused
    either way, and the hash is the stronger guarantee of the two. Without a
    published hash there is no such retry.
    """
    url, referer, limit = meta["urls"][key], meta["referer"], config.MAX_ZIP_BYTES + 1
    try:
        return _fetch(url, referer=referer, limit=limit, jar=jar)
    except urllib.error.HTTPError as exc:
        raise MirrorRefused(f"answered {exc.code}")
    except urllib.error.URLError as exc:
        if not (isinstance(exc.reason, ssl.SSLCertVerificationError) and meta.get("sha256")):
            raise MirrorRefused(type(exc.reason).__name__ if exc.reason is not None else "URLError")
    except OSError as exc:
        raise MirrorRefused(type(exc).__name__)
    try:
        blob = _fetch(url, referer=referer, limit=limit, jar=jar, verify=False)
    except (urllib.error.URLError, OSError) as exc:
        raise MirrorRefused(f"certificate chain incomplete, then {type(exc).__name__}")
    print(f"[maps] {key}: certificate chain incomplete; the file is taken on lvlworld's"
          " published sha256", flush=True)
    return blob


def lvl_download(meta, jar):
    """(zip, mirror) from the first mirror that hands one over.

    Mirrors go in the page's order, except that one which refused recently
    goes last - so an install does not wait on a host known to be down, and
    no host is ever given up on. When none serves a zip, every reason is in
    the error.
    """
    now = time.time()
    order = sorted(meta["mirrors"], key=lambda m: now - _mirror_down.get(m, 0) < MIRROR_RETRY)
    reasons = []
    for key in order:
        try:
            blob = _mirror_blob(meta, key, jar)
        except MirrorRefused as exc:
            reasons.append(f"{key} {exc}")
        else:
            if blob.startswith(b"PK"):
                _mirror_down.pop(key, None)
                return blob, key
            reasons.append(f"{key} sent a page rather than a zip")
        _mirror_down[key] = now
    raise ValueError("lvlworld refused the download from every mirror: " + "; ".join(reasons))


def install_from_lvl(ref, force=False):
    meta, jar = _lvl_session(ref)
    blob, mirror = lvl_download(meta, jar)

    digest = hashlib.sha256(blob).hexdigest()
    if meta["sha256"] and digest != meta["sha256"]:
        raise ValueError(f"sha256 mismatch: the download from {mirror} does not match lvlworld's hash")

    return {"meta": meta, "mirror": mirror, **install_blob(blob, meta["filename"], force)}


# --------------------------------------------------------------- installing
def screen_pk3(path):
    """Missing-shader report from tools/check-map.py, or None if unavailable."""
    if not config.CHECK_MAP.exists():
        return None
    result = subprocess.run(
        ["python3", str(config.CHECK_MAP), "--base", str(config.BASE_PAKS),
         "--max-missing", str(config.MISSING_SHADER_LIMIT), str(path)],
        capture_output=True, text=True, timeout=300)
    missing = sum(int(n) for n in re.findall(r"(\d+) missing", result.stdout))
    return {"ok": result.returncode == 0, "missing": missing,
            "detail": result.stdout.strip()}


def index_in_manifest(name, size, crc):
    """Record a pak in the served manifest, replacing any earlier entry.

    Without this a pak is on disk but invisible: the manifest is what the game
    server reads at start and what every browser client downloads from, and
    sync-assets.py only rebuilds it when the container starts. Miss this and an
    upload appears to work, shows up in the console catalog, and then does
    nothing until somebody restarts the container.
    """
    entry = {"name": f"{config.FS_GAME}/{name}", "compressed": size, "checksum": crc}
    with _manifest_lock:
        manifest_path = config.ASSETS / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for existing in manifest:
            if existing["name"] == entry["name"]:
                existing.update(entry)
                break
        else:
            manifest.append(entry)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return entry


def install_pk3(name, data, force=False):
    """Place one pk3 in the served assets and index it in the manifest.

    Shared by the lvlworld installer and console uploads, so a map added either
    way is screened, named and indexed identically.
    """
    if not config.MAP_RE.match(name):
        raise ValueError("refusing suspicious pk3 name: %r" % (name,))

    staged = pathlib.Path("/tmp") / name
    staged.write_bytes(data)
    try:
        report = screen_pk3(staged)
        if report and not report["ok"] and not force:
            raise ValueError(
                f"{name}: {report['missing']} shaders missing from this server - "
                "more than Quake 3's own maps miss, so parts of it would render as placeholders. Install with force to accept that.")

        crc = zlib.crc32(data) & 0xFFFFFFFF
        target_dir = config.ASSETS / config.FS_GAME
        target_dir.mkdir(parents=True, exist_ok=True)
        # The checksum is part of the filename, so a changed file lands beside
        # the old one unless the previous copy goes first.
        for stale in target_dir.glob(f"*-{name}"):
            stale.unlink()
        (target_dir / f"{crc}-{name}").write_bytes(data)
    finally:
        staged.unlink(missing_ok=True)

    index_in_manifest(name, len(data), crc)

    with zipfile.ZipFile(io.BytesIO(data)) as pk3:
        maps = [pathlib.PurePath(n.lower()).stem for n in pk3.namelist()
                if n.lower().startswith("maps/") and n.lower().endswith(".bsp")]
    forget()
    return {"file": name, "maps": sorted(set(maps)), "screening": report}


def pk3s_in(blob, fallback_name, limit=None):
    """The pk3s carried by a download or an upload.

    Map sites hand out a zip with the pk3 inside, but a pk3 is itself a zip, so
    the two are told apart by what the archive contains rather than by name.
    An inner pk3 is held to the same size limit as the archive itself, before
    it is inflated: the outer zip is capped on the wire, and this is what keeps
    the cap meaning something.
    """
    limit = config.MAX_ZIP_BYTES if limit is None else limit
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ValueError("not a zip archive; expected a .pk3 or a .zip containing one")
    members = [n for n in archive.namelist() if n.lower().endswith(".pk3")]
    if members:
        return [(pathlib.PurePath(m).name, _read_member(archive, m, limit)) for m in members]
    if not any(n.lower().startswith("maps/") and n.lower().endswith(".bsp")
               for n in archive.namelist()):
        raise ValueError("archive holds neither a .pk3 nor any maps/*.bsp")
    name = pathlib.PurePath(fallback_name or "upload.pk3").name
    if not name.lower().endswith(".pk3"):
        name = pathlib.PurePath(name).stem + ".pk3"
    return [(name, blob)]


def install_blob(blob, filename, force=False):
    """Install every pk3 in one archive, reporting what became playable."""
    if len(blob) > config.MAX_ZIP_BYTES:
        raise ValueError(f"file exceeds the {config.MAX_ZIP_BYTES // 1048576} MiB limit")
    maps, reports, files = [], [], []
    for name, data in pk3s_in(blob, filename):
        result = install_pk3(name, data, force)
        maps += result["maps"]
        files.append(result["file"])
        if result["screening"]:
            reports.append(result["screening"])
    if not maps:
        raise ValueError("no maps/*.bsp found, so there is nothing to play")
    return {"files": files, "maps": sorted(set(maps)), "screening": reports}


def install_base_pak(staged, name):
    """Install one of the game's own paks from a staged upload.

    Base paks are the game content everything else is measured against, so they
    skip the missing-shader screening a map goes through - there is nothing to
    screen them against. Nothing is read into memory either: retail pak0.pk3 is
    457 MB, and the checksum the manifest needs can be computed a chunk at a
    time.

    Quake 3's retail paks are licensed content that cannot be redistributed, so
    they are never shipped in the image. This is how an operator supplies their
    own copy; EXTRA_PAKS is the same thing fetched from a URL.
    """
    checksum = 0
    size = 0
    with staged.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            checksum = zlib.crc32(chunk, checksum)
            size += len(chunk)
    crc = checksum & 0xFFFFFFFF

    with zipfile.ZipFile(staged) as archive:
        entries = archive.namelist()
    maps = sorted({pathlib.PurePath(n).stem.lower() for n in entries
                   if n.lower().startswith("maps/") and n.lower().endswith(".bsp")})
    bots = set()
    for entry in entries:
        if entry.lower().endswith("scripts/bots.txt"):
            with zipfile.ZipFile(staged) as archive:
                bots |= set(config.BOT_NAME_RE.findall(
                    _read_member(archive, entry).decode("latin-1")))

    target_dir = config.ASSETS / config.FS_GAME
    target_dir.mkdir(parents=True, exist_ok=True)
    for stale in target_dir.glob(f"*-{name}"):
        stale.unlink()
    staged.replace(target_dir / f"{crc}-{name}")
    index_in_manifest(name, size, crc)

    forget()
    return {"files": [name], "maps": maps, "bots": sorted(bots),
            "bytes": size, "screening": []}


def uninstall_map(name):
    """Remove a console-installed map. Bundled maps stay put."""
    pak = pk3_for_map(name)
    if pak is None:
        raise ValueError("%r is not provided by an installed pk3" % name)
    if pak.name in bundled_map_files():
        raise ValueError("%s ships with the image and cannot be removed" % pak.name)
    if is_base_pak(pak.name):
        raise ValueError(
            "%s is one of the game's own paks, not an installed map; removing it "
            "would take every map and bot it provides" % pak.name)
    manifest_path = config.ASSETS / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    stem = pak.name.split("-", 1)[-1] if "-" in pak.name else pak.name
    manifest = [e for e in manifest if e["name"] != "%s/%s" % (config.FS_GAME, stem)]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    pak.unlink()
    forget()
    return {"removed": pak.name, "maps": sorted(set(settings.saved_rotation()) - {name})}
