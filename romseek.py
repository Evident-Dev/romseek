#!/usr/bin/env python3
"""romseek: search Prowlarr for console games, send picks to qBittorrent,
and link finished downloads into RomM's library (optionally after a virus scan).

Stdlib only (runs on plain python:3.12-alpine like import-notify).
Put it behind an authenticating reverse proxy (e.g. Caddy/Traefik + Authelia
forward-auth). The app itself does no auth yet, so never publish its port.

Flow:
  search  -> Prowlarr /api/v1/search, categories=1000 (Console)
  grab    -> fetch .torrent/magnet server-side, add to qBittorrent with
             category "roms" and tag "rom:<romm platform slug>"
  import  -> every POLL_SECONDS: completed "roms" torrents whose files are
             older than the optional SCAN_STATE cutoff (i.e. scanned
             and not deleted), and contain no Windows PE files, are hardlinked
             into /data/media/roms/roms/<slug>/. RomM's filesystem watcher
             picks them up. Torrent gets tag "imported" (or "blocked").
"""
import hashlib
import html
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
PROWLARR_KEY = os.environ["PROWLARR_KEY"]
QBIT_URL = os.environ.get("QBIT_URL", "http://gluetun:8080").rstrip("/")
CATEGORY = os.environ.get("QBIT_CATEGORY", "roms")
SAVE_PATH = os.environ.get("QBIT_SAVE_PATH", "/data/downloads/roms")
LIBRARY = os.environ.get("ROMM_LIBRARY", "/data/media/roms/roms")
# Optional virus-scan gate: path to a file whose mtime is the cutoff your scanner
# has covered (files older than it are scanned). Empty = import immediately.
SCAN_STATE = os.environ.get("SCAN_STATE", "")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
PORT = int(os.environ.get("PORT", "8000"))

# RomM folder slug -> label, plus title regexes used to pre-select the platform.
PLATFORMS = [
    ("nes", "NES", r"\bnes\b|famicom|\.nes\b"),
    ("snes", "SNES", r"\bsnes\b|super nintendo|super famicom|\.sfc\b|\.smc\b"),
    ("n64", "Nintendo 64", r"\bn64\b|nintendo 64|\.z64\b|\.v64\b"),
    ("gb", "Game Boy", r"\bgame ?boy\b(?! (color|advance))|\.gb\b"),
    ("gbc", "Game Boy Color", r"\bgbc\b|game ?boy colou?r|\.gbc\b"),
    ("gba", "Game Boy Advance", r"\bgba\b|game ?boy advance|\.gba\b"),
    ("nds", "Nintendo DS", r"\bnds\b|nintendo ds\b|\bds\b|\.nds\b"),
    ("3ds", "Nintendo 3DS", r"\b3ds\b|\.cia\b"),
    ("ngc", "GameCube", r"gamecube|\bngc\b|\bgc\b"),
    ("wii", "Wii", r"\bwii\b(?! ?u)|\bwbfs\b"),
    ("wiiu", "Wii U", r"wii ?u\b"),
    ("switch", "Switch", r"\bswitch\b|\bnsp\b|\bxci\b"),
    ("genesis", "Genesis / Mega Drive", r"genesis|mega ?drive|\.md\b|\.gen\b"),
    ("sms", "Master System", r"master system|\.sms\b"),
    ("gamegear", "Game Gear", r"game ?gear|\.gg\b"),
    ("segacd", "Sega CD", r"sega ?cd|mega ?cd"),
    ("sega32", "32X", r"\b32x\b"),
    ("saturn", "Saturn", r"\bsaturn\b"),
    ("dc", "Dreamcast", r"dreamcast|\bgdi\b"),
    ("psx", "PlayStation", r"\bpsx\b|\bps1\b|playstation(?! ?[2-5])"),
    ("ps2", "PlayStation 2", r"\bps2\b|playstation ?2"),
    ("psp", "PSP", r"\bpsp\b"),
    ("psvita", "PS Vita", r"\bvita\b"),
    ("ps3", "PlayStation 3", r"\bps3\b|playstation ?3"),
    ("xbox", "Xbox", r"\bxbox\b(?! ?(360|one|series))"),
    ("atari2600", "Atari 2600", r"atari ?2600|\.a26\b"),
    ("tg16", "TurboGrafx-16 / PC Engine", r"turbografx|pc ?engine|\.pce\b"),
    ("neogeoaes", "Neo Geo", r"neo ?geo"),
    ("arcade", "Arcade (MAME)", r"\bmame\b|arcade"),
]
SLUGS = {s for s, _, _ in PLATFORMS}
LABEL = {s: l for s, l, _ in PLATFORMS}

# RomM structure A: library/roms/<slug> for games, library/bios/<slug> for firmware.
# A platform "exists" in RomM once its roms/<slug> folder exists: creating it
# triggers RomM's watcher to run a full scan that registers the platform. The
# watcher ignores new games on platforms RomM doesn't know yet, which is why a
# game grab requires the platform first.
FIRMWARE = os.environ.get("ROMM_FIRMWARE", os.path.join(os.path.dirname(LIBRARY), "bios"))

# BIOS/firmware needs for RomM's in-browser player (EmulatorJS).
BIOS = {
    "segacd": "required", "saturn": "required", "neogeoaes": "required",
    "neogeomvs": "required", "ps2": "required", "dc": "recommended",
    "psx": "recommended", "arcade": "some games",
    "gba": "optional", "nds": "optional",
    "xbox": "required", "ps3": "required", "psvita": "required", "switch": "required",
    "wiiu": "some games", "3ds": "some games",
}
# One-line explanation shown on the platform card.
BIOS_NOTE = {
    "ps2": "PCSX2 needs a PS2 BIOS dump (e.g. SCPH-39001).",
    "dc": "Flycast has a built-in BIOS that runs most games; the real dc_boot.bin + dc_flash.bin fix the rest.",
    "xbox": "xemu needs the MCPX boot ROM, a flash BIOS image and a hard-disk image.",
    "ps3": "RPCS3 needs Sony's PS3 system software (PS3UPDAT.PUP). It's a free download from Sony.",
    "psvita": "Vita3K needs Sony's PS Vita firmware (PSVUPDAT.PUP). It's a free download from Sony.",
    "switch": "Needs system files dumped from your own Switch. Follow your emulator's setup guide.",
    "wiiu": "Encrypted games need files dumped from your own Wii U (see Cemu's setup guide); decrypted dumps play without them.",
    "3ds": "Encrypted games need files dumped from your own 3DS (see Azahar's setup guide); decrypted games play without them.",
    "segacd": "Needs the Sega CD / Mega-CD BIOS for your region.",
    "saturn": "Needs the Saturn BIOS (sega_101.bin / mpr-17933.bin).",
    "psx": "Most games run without it; some need a PS1 BIOS (e.g. scph5501.bin).",
    "neogeoaes": "Needs neogeo.zip.", "neogeomvs": "Needs neogeo.zip.",
    "arcade": "Some arcade sets need BIOS zips (e.g. neogeo.zip).",
    "gba": "Optional: games run without it; the real BIOS improves compatibility.",
    "nds": "Optional: games run without it; the real BIOS improves compatibility.",
}
# Firmware the maker gives away: link to the official page instead of searching torrents.
BIOS_OFFICIAL = {
    "ps3": ("Get PS3 system software from Sony", "https://www.playstation.com/en-us/support/hardware/ps3/system-software/"),
    "psvita": ("Get PS Vita firmware from Sony", "https://www.playstation.com/en-us/support/hardware/psvita/system-software/"),
}
BIOS_QUERY = {
    "psx": "psx bios", "ps2": "ps2 bios", "segacd": "sega cd bios",
    "saturn": "sega saturn bios", "dc": "dreamcast bios", "neogeoaes": "neogeo bios",
    "neogeomvs": "neogeo bios", "arcade": "mame bios", "gba": "gba bios", "nds": "nds bios",
    "xbox": "xbox bios mcpx",
}
# Firmware RomM wants as the zip itself (e.g. neogeo.zip), never extracted.
KEEP_ZIP = {"neogeoaes", "neogeomvs", "arcade"}
# Platforms EmulatorJS can play in the browser; the rest are library/download-only.
BROWSER = {"nes", "snes", "n64", "gb", "gbc", "gba", "nds", "genesis", "sms",
           "gamegear", "segacd", "sega32", "saturn", "psx", "psp", "atari2600",
           "tg16", "arcade", "neogeoaes", "neogeomvs"}

# Download-only platforms: official emulator download pages (no hosted copies).
EMULATORS = {
    "ps2": [("PCSX2", "https://pcsx2.net/downloads")],
    "ngc": [("Dolphin", "https://dolphin-emu.org/download/")],
    "wii": [("Dolphin", "https://dolphin-emu.org/download/")],
    "wiiu": [("Cemu", "https://cemu.info/")],
    "ps3": [("RPCS3", "https://rpcs3.net/download")],
    "psvita": [("Vita3K", "https://vita3k.org/")],
    "xbox": [("xemu", "https://xemu.app/")],
    "3ds": [("Azahar", "https://azahar-emu.org/")],
    "dc": [("Flycast", "https://github.com/flyinghead/flycast/releases")],
}

# RomM API (optional): a client API token with roms.read + platforms.read lets
# the Library link each game straight to its RomM page / player.
ROMM_URL = os.environ.get("ROMM_URL", "http://romm:8080").rstrip("/")
ROMM_PUBLIC = os.environ.get("ROMM_PUBLIC_URL", "").rstrip("/")  # e.g. https://romm.example.com
ROMM_TOKEN = os.environ.get("ROMM_API_TOKEN", "").strip()
_ROMM_CACHE = {"t": 0, "platforms": {}, "roms": {}, "error": ""}


def romm_index():
    """{fs_slug: platform_id}, {(platform_id, fs_name): rom_id}; cached 60s."""
    c = _ROMM_CACHE
    if not ROMM_TOKEN or time.time() - c["t"] < 60:
        return c
    hdr = {"Authorization": f"Bearer {ROMM_TOKEN}"}
    try:
        plats = json.loads(http(f"{ROMM_URL}/api/platforms", headers=hdr, timeout=20).read())
        if isinstance(plats, dict):
            plats = plats.get("items", [])
        pmap = {p["fs_slug"]: p["id"] for p in plats}
        roms, offset = {}, 0
        while True:
            q = urllib.parse.urlencode({"limit": 1000, "offset": offset, "with_char_index": "false",
                                        "with_filter_values": "false", "with_rom_id_index": "false"})
            page = json.loads(http(f"{ROMM_URL}/api/roms?{q}", headers=hdr, timeout=30).read())
            items = page.get("items", []) if isinstance(page, dict) else page
            for r in items:
                m = r.get("metadatum") or {}
                ms = m.get("first_release_date")
                roms[(r.get("platform_id"), r.get("fs_name"))] = {
                    "id": r["id"], "name": r.get("name") or "", "fs_name": r.get("fs_name") or "",
                    "cover": (r.get("path_cover_large") or r.get("path_cover_small") or "").strip(),
                    "year": time.gmtime(ms / 1000).tm_year if isinstance(ms, (int, float)) and ms > 0 else None,
                    "genres": (m.get("genres") or [])[:3],
                    "summary": (r.get("summary") or "")[:400],
                    "identified": bool(r.get("is_identified")),
                }
            offset += len(items)
            if not items or not isinstance(page, dict) or offset >= (page.get("total") or 0):
                break
        c.update(t=time.time(), platforms=pmap, roms=roms, error="")
    except Exception as e:
        c.update(t=time.time(), error=str(e))
        log("RomM API error:", repr(e))
    return c
# Newznab console subcategories that map cleanly to one RomM platform.
CAT_PLATFORM = {1010: "nds", 1020: "psp", 1030: "wii", 1040: "xbox", 1080: "ps3",
                1110: "3ds", 1120: "psvita", 1130: "wiiu"}
# Indexer names (as shown in Prowlarr) to hide from results, comma-separated.
# LimeTorrents is excluded by default: it files PC releases under Console and
# has served executables disguised as media.
EXCLUDED_INDEXERS = set(filter(None, os.environ.get("EXCLUDED_INDEXERS", "LimeTorrents").split(",")))

# Files never copied into the library.
JUNK_EXT = {".nfo", ".txt", ".jpg", ".jpeg", ".png", ".gif", ".url", ".sfv",
            ".md5", ".sha1", ".htm", ".html", ".db", ".ini", ".log", ".torrent"}
# Single-file ROM formats: a torrent made only of these is a pack of separate
# games and is linked flat; anything else (cue/bin, multi-part) becomes one
# folder = one multi-file game in RomM.
STANDALONE_EXT = {".zip", ".7z", ".nes", ".fds", ".sfc", ".smc", ".z64", ".n64",
                  ".v64", ".gb", ".gbc", ".gba", ".nds", ".3ds", ".cia", ".md",
                  ".gen", ".bin_md", ".sms", ".gg", ".32x", ".pce", ".a26",
                  ".iso", ".chd", ".cso", ".rvz", ".wbfs", ".wua", ".nsp", ".xci",
                  ".pbp", ".vpk"}

LOCK = threading.Lock()
RESULTS = {}          # result id -> prowlarr result (server-side so the client can't inject URLs)
STATUS = {}           # torrent hash -> {"state":..., "detail":...}
STATUS_FILE = os.path.join(STATE_DIR, "status.json")


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def load_status():
    try:
        with open(STATUS_FILE) as f:
            STATUS.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_status():
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(STATUS, f)
    os.replace(tmp, STATUS_FILE)


# ---------------------------------------------------------------- HTTP helpers
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def http(url, data=None, headers=None, timeout=60, follow=True):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    return opener.open(req, timeout=timeout)


def qbit(path, fields=None, files=None):
    """POST/GET to qBittorrent API. Auth is bypassed for the docker subnet."""
    url = f"{QBIT_URL}/api/v2/{path}"
    if files:
        boundary = "----romseek" + hashlib.md5(os.urandom(8)).hexdigest()
        body = b""
        for k, v in (fields or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        for k, (name, content) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\n"
                     "Content-Type: application/x-bittorrent\r\n\r\n").encode() + content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        r = http(url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}",
                             "Referer": QBIT_URL})
    elif fields is not None:
        r = http(url, urllib.parse.urlencode(fields).encode(), {"Referer": QBIT_URL})
    else:
        r = http(url, headers={"Referer": QBIT_URL})
    return r.read()


def ensure_category():
    cats = json.loads(qbit("torrents/categories"))
    if CATEGORY not in cats:
        qbit("torrents/createCategory", {"category": CATEGORY, "savePath": SAVE_PATH})
        log("created qBittorrent category", CATEGORY, SAVE_PATH)


# ---------------------------------------------------------------- search / grab
def guess_platform(title):
    t = title.lower()
    for slug, _, rx in PLATFORMS:
        if re.search(rx, t):
            return slug
    return ""


class PlatformMissing(ValueError):
    pass


def platform_exists(slug):
    return os.path.isdir(os.path.join(LIBRARY, slug))


def bios_files(slug):
    d = os.path.join(FIRMWARE, slug)
    try:
        return sorted(f for f in os.listdir(d) if not f.startswith("."))
    except OSError:
        return []


def platform_info(slug):
    return {
        "slug": slug, "label": LABEL[slug], "exists": platform_exists(slug),
        "bios": BIOS.get(slug, ""), "bios_files": len(bios_files(slug)),
        "bios_names": bios_files(slug)[:50],
        "games": game_count(slug),
        "emulators": [{"name": n, "url": u} for n, u in EMULATORS.get(slug, [])],
        "romm_id": romm_index()["platforms"].get(slug),
        "bios_query": BIOS_QUERY.get(slug, ""), "browser": slug in BROWSER,
        "bios_note": BIOS_NOTE.get(slug, ""),
        "bios_official": dict(zip(("label", "url"), BIOS_OFFICIAL[slug])) if slug in BIOS_OFFICIAL else None,
    }


def platforms():
    return [platform_info(s) for s, _, _ in PLATFORMS]


def game_count(slug):
    try:
        return sum(1 for e in os.scandir(os.path.join(LIBRARY, slug)) if not e.name.startswith("."))
    except OSError:
        return 0


def romm_meta(r):
    """RomM metadata for a library entry (empty when RomM hasn't scanned it yet)."""
    if not r:
        return {"romm_id": None}
    return {"romm_id": r["id"], "title": r["name"], "fs_name": r["fs_name"],
            "cover": ("api/img?p=" + urllib.parse.quote(r["cover"], safe="")) if r["cover"] else "",
            "year": r["year"], "genres": r["genres"], "summary": r["summary"],
            "identified": r["identified"]}


_IMG_CACHE = {}


def romm_asset(path):
    """Fetch a RomM static asset (covers, platform logos) server-side, so images
    work even where RomM's assets need auth. Only /assets/ paths are allowed."""
    path = "/" + path.lstrip("/")
    if not path.startswith("/assets/") or ".." in path:
        raise ValueError("bad asset path")
    hit = _IMG_CACHE.get(path)
    if hit and time.time() - hit[2] < 3600:
        return hit[0], hit[1]
    hdr = {"Authorization": f"Bearer {ROMM_TOKEN}"} if ROMM_TOKEN else {}
    r = http(ROMM_URL + urllib.parse.quote(path, safe="/?=&%:"), headers=hdr, timeout=20)
    data, ctype = r.read(), (r.headers.get("Content-Type") or "application/octet-stream").split(";")[0]
    if not ctype.startswith("image/"):
        raise ValueError("not an image")
    if len(_IMG_CACHE) > 500:
        _IMG_CACHE.clear()
    _IMG_CACHE[path] = (data, ctype, time.time())
    return data, ctype


def platform_logo(slug):
    if slug not in SLUGS:
        raise ValueError("unknown platform")
    for name in (slug, {"sega32": "32x", "neogeomvs": "neogeoaes"}.get(slug, slug)):
        for ext in ("svg", "ico", "png"):
            try:
                return romm_asset(f"/assets/platforms/{name}.{ext}")
            except (urllib.error.HTTPError, ValueError):
                continue
    raise ValueError("no logo")


def library():
    """Games on disk: each top-level file or folder in roms/<slug>/ is one game in RomM."""
    out = []
    for slug, label, _ in PLATFORMS:
        d = os.path.join(LIBRARY, slug)
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    size, files = 0, 0
                    for root, _, fs in os.walk(e.path):
                        for f in fs:
                            size += os.path.getsize(os.path.join(root, f)); files += 1
                else:
                    size, files = e.stat().st_size, 1
                added = e.stat().st_mtime
            except OSError:
                continue
            idx = romm_index()
            out.append({"name": e.name, "platform": slug, "size": size, "files": files,
                        "folder": e.is_dir(), "added": int(added),
                        **romm_meta(idx["roms"].get((idx["platforms"].get(slug), e.name)))})
    out.sort(key=lambda g: -g["added"])
    return out


def add_platform(slug):
    if slug not in SLUGS:
        raise ValueError("unknown platform")
    os.makedirs(os.path.join(LIBRARY, slug), exist_ok=True)
    if slug in BIOS:
        os.makedirs(os.path.join(FIRMWARE, slug), exist_ok=True)
    log("platform added", slug)
    return platform_info(slug)


def search(query, any_category=False):
    # BIOS packs are rarely filed under Console, so firmware searches go uncategorised.
    params = {"query": query, "type": "search", "limit": "100"}
    if not any_category:
        params["categories"] = "1000"
    q = urllib.parse.urlencode(params)
    r = http(f"{PROWLARR_URL}/api/v1/search?{q}", headers={"X-Api-Key": PROWLARR_KEY}, timeout=90)
    out = []
    with LOCK:
        if len(RESULTS) > 5000:
            RESULTS.clear()
        for x in json.loads(r.read()):
            if x.get("indexer") in EXCLUDED_INDEXERS:
                continue
            cats = {c.get("id") for c in x.get("categories", [])}
            # Title first: BlueRoms files e.g. SNES games under 1010 (NDS).
            plat = guess_platform(x.get("title", "")) \
                or next((CAT_PLATFORM[c] for c in cats if c in CAT_PLATFORM), "")
            rid = hashlib.sha1((x.get("guid", "") + str(x.get("indexerId"))).encode()).hexdigest()[:16]
            RESULTS[rid] = x
            out.append({
                "id": rid, "title": x.get("title", ""), "indexer": x.get("indexer", ""),
                "size": x.get("size") or 0, "seeders": x.get("seeders") or 0,
                "age": x.get("age"), "info": x.get("infoUrl") or "",
                "platform": plat,
            })
    # ROM-specific indexer first, then anything we could place on a platform
    # (generic-"Console" hits are usually miscategorised PC releases), then seeds.
    out.sort(key=lambda r: (r["indexer"] != "BlueRoms", not r["platform"], -r["seeders"]))
    return out


def grab(rid, slug, kind="rom"):
    if slug not in SLUGS:
        raise ValueError("pick a platform")
    if kind not in ("rom", "bios"):
        raise ValueError("bad kind")
    with LOCK:
        x = RESULTS.get(rid)
    if not x:
        raise ValueError("result expired, search again")
    if kind == "rom" and not platform_exists(slug):
        raise PlatformMissing(slug)
    if kind == "bios":
        add_platform(slug)
    fields = {"category": CATEGORY, "tags": f"romseek,{kind}:{slug}", "savepath": SAVE_PATH}
    # Prowlarr puts the link in different fields per indexer: a real magnet in
    # magnetUrl or guid, or a Prowlarr proxy URL in magnetUrl/downloadUrl that
    # answers with a .torrent body or a redirect to a magnet.
    magnet = next((v for v in (x.get("magnetUrl"), x.get("guid")) if (v or "").startswith("magnet:")), "")
    if not magnet:
        proxies = [v for v in (x.get("downloadUrl"), x.get("magnetUrl")) if (v or "").startswith("http")]
        last_err = None
        for url in proxies:
            # Prowlarr builds proxy links as http://localhost:9696/... — point at its container.
            dl = re.sub(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?", PROWLARR_URL, url)
            try:
                content = http(dl, follow=False, timeout=60).read()
            except urllib.error.HTTPError as e:
                loc = e.headers.get("Location", "")
                if e.code in (301, 302, 303, 307, 308) and loc.startswith("magnet:"):
                    magnet = loc
                    break
                last_err = ValueError(f"indexer refused the download (HTTP {e.code})")
                continue
            if content[:1] == b"d":
                qbit("torrents/add", fields, files={"torrents": ("rom.torrent", content)})
                log("grabbed (torrent)", kind, slug, x.get("title"))
                return
            if content.startswith(b"magnet:"):
                magnet = content.decode(errors="ignore").strip()
                break
            last_err = ValueError("indexer did not return a torrent or magnet")
        if not magnet and x.get("infoHash"):
            # Last resort: bare info-hash magnet (DHT finds peers, just slower).
            magnet = f"magnet:?xt=urn:btih:{x['infoHash']}&dn={urllib.parse.quote(x.get('title', ''))}"
        if not magnet:
            raise last_err or ValueError("no usable download link")
    fields["urls"] = magnet
    qbit("torrents/add", fields)
    log("grabbed (magnet)", slug, x.get("title"))


# ---------------------------------------------------------------- importer
def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip(" .")
    return s[:180] or "game"


def link(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        return False
    try:
        os.link(src, dst)
    except OSError:
        import shutil
        shutil.copy2(src, dst)
    return True


def set_status(h, state, detail=""):
    STATUS[h] = {"state": state, "detail": detail, "t": int(time.time())}
    save_status()


def import_bios(wanted, slug):
    """Firmware goes flat into library/bios/<slug>/. Zip packs are unpacked
    (basenames only, so no path traversal) unless RomM wants the zip itself."""
    import zipfile
    dest = os.path.join(FIRMWARE, slug)
    os.makedirs(dest, exist_ok=True)
    n, notes = 0, []
    for name, p in wanted:
        ext = os.path.splitext(p)[1].lower()
        if ext == ".zip" and slug not in KEEP_ZIP:
            try:
                with zipfile.ZipFile(p) as z:
                    for m in z.infolist():
                        base = safe_name(os.path.basename(m.filename))
                        if m.is_dir() or not base or base.startswith(".") \
                                or os.path.splitext(base)[1].lower() in JUNK_EXT:
                            continue
                        if m.file_size > 512 * 1024 * 1024:
                            notes.append(f"skipped oversized {base}")
                            continue
                        out = os.path.join(dest, base)
                        if os.path.exists(out):
                            continue
                        data = z.read(m)
                        if data[:2] == b"MZ":
                            notes.append(f"skipped executable {base}")
                            continue
                        with open(out, "wb") as fh:
                            fh.write(data)
                        n += 1
            except zipfile.BadZipFile:
                notes.append(f"bad zip {os.path.basename(p)}")
        else:
            if ext in (".7z", ".rar"):
                notes.append(f"{os.path.basename(p)} needs extracting by hand")
            n += link(p, os.path.join(dest, safe_name(os.path.basename(p))))
    detail = f"{n} firmware file(s) -> bios/{slug}" + (f" ({'; '.join(notes)})" if notes else "")
    return "imported", detail


def import_torrent(t, slug, kind="rom"):
    h = t["hash"]
    files = json.loads(qbit(f"torrents/files?hash={h}"))
    base = t["save_path"]
    wanted = []
    for f in files:
        if f.get("priority", 1) == 0:
            continue
        p = os.path.join(base, f["name"])
        if not os.path.isfile(p):
            continue
        if os.path.splitext(p)[1].lower() in JUNK_EXT:
            continue
        wanted.append((f["name"], p))
    if not wanted:
        # Nothing left: ClamAV deleted it, or it was all junk.
        return "blocked", "no usable files (deleted by virus scanner, or only junk)"

    # Scan gate: the scanner covers files with mtime <= its last-run cutoff.
    if SCAN_STATE:
        try:
            cutoff = os.path.getmtime(SCAN_STATE)
        except OSError:
            return "waiting", "virus scanner state file not found"
        newest = max(os.path.getmtime(p) for _, p in wanted)
        if newest > cutoff:
            return "waiting", "waiting for virus scan"

    for name, p in wanted:
        with open(p, "rb") as fh:
            if fh.read(2) == b"MZ":
                return "blocked", f"Windows executable inside: {name}"

    if kind == "bios":
        return import_bios(wanted, slug)

    dest_dir = os.path.join(LIBRARY, slug)
    linked = 0
    exts = {os.path.splitext(p)[1].lower() for _, p in wanted}
    if len(wanted) == 1 or exts <= STANDALONE_EXT:
        for _, p in wanted:
            linked += link(p, os.path.join(dest_dir, safe_name(os.path.basename(p))))
    else:
        game = os.path.join(dest_dir, safe_name(t["name"]))
        for name, p in wanted:
            parts = name.split("/")
            rel = parts[1:] if len(parts) > 1 else parts
            linked += link(p, os.path.join(game, *[safe_name(x) for x in rel]))
    return "imported", f"{linked} file(s) -> {slug}"


def finished(t):
    """Done = every *selected* file is complete. Overall progress stays below 100%
    when qBittorrent skipped files (e.g. excluded *.exe), so check amount_left."""
    if t.get("progress", 0) >= 1:
        return True
    return t.get("amount_left", 1) == 0 and not t.get("state", "").endswith(("DL", "checkingResumeData"))


def tag_target(tags):
    for x in tags:
        k, _, s = x.partition(":")
        if k in ("rom", "bios") and s:
            return k, s
    return "rom", ""


def importer_loop():
    while True:
        try:
            ensure_category()
            torrents = json.loads(qbit(f"torrents/info?category={CATEGORY}"))
            for t in torrents:
                h = t["hash"]
                tags = [x.strip() for x in t.get("tags", "").split(",")]
                kind, slug = tag_target(tags)
                if "imported" in tags or "blocked" in tags or not finished(t):
                    continue
                if slug not in SLUGS:
                    set_status(h, "blocked", "no rom:/bios:<platform> tag")
                    qbit("torrents/addTags", {"hashes": h, "tags": "blocked"})
                    continue
                if kind == "rom" and not platform_exists(slug):
                    add_platform(slug)  # e.g. folder removed after grab; RomM needs it
                state, detail = import_torrent(t, slug, kind)
                if STATUS.get(h, {}).get("detail") != detail:
                    log(state, t["name"], "-", detail)
                set_status(h, state, detail)
                if state in ("imported", "blocked"):
                    qbit("torrents/addTags", {"hashes": h, "tags": state})
        except Exception:
            log("importer error:", traceback.format_exc().strip().splitlines()[-1])
        time.sleep(POLL_SECONDS)


def queue():
    torrents = json.loads(qbit(f"torrents/info?category={CATEGORY}&sort=added_on&reverse=true"))
    out = []
    for t in torrents[:50]:
        tags = [x.strip() for x in t.get("tags", "").split(",")]
        st = STATUS.get(t["hash"], {})
        if "imported" in tags:
            state = "imported"
        elif "blocked" in tags:
            state = "blocked"
        elif not finished(t):
            state = "downloading"
        else:
            state = st.get("state", "waiting")
        kind, slug = tag_target(tags)
        out.append({
            "name": t["name"], "progress": t.get("progress", 0), "size": t.get("size", 0),
            "added": t.get("added_on", 0),
            "eta": t.get("eta"), "dlspeed": t.get("dlspeed", 0), "seeds": t.get("num_seeds", 0),
            "peers": t.get("num_leechs", 0), "seeds_total": t.get("num_complete", 0),
            "peers_total": t.get("num_incomplete", 0), "qstate": t.get("state", ""),
            "platform": slug, "kind": kind,
            "state": state, "detail": st.get("detail", ""),
        })
    return out


# ---------------------------------------------------------------- firmware upload
UPLOAD_MAX = int(os.environ.get("UPLOAD_MAX_MB", "1024")) * 1024 * 1024
# Never accept these as "firmware".
UPLOAD_BLOCK_EXT = {".exe", ".scr", ".com", ".pif", ".bat", ".cmd", ".msi", ".msp", ".hta",
                    ".cpl", ".jar", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1",
                    ".psm1", ".reg", ".lnk", ".url", ".dll", ".sh", ".html", ".htm", ".apk"}


def upload_firmware(slug, filename, length, stream, offset=0, total=None, upload_id=""):
    """Receive an uploaded firmware file (optionally in chunks) into library/bios/<slug>/.

    Chunked uploads keep each request under Cloudflare's 100 MB request limit:
    the browser sends pieces with offset/total/upload_id; the file is checked on
    its first bytes and moved into place atomically once the last piece lands.
    """
    if slug not in SLUGS:
        raise ValueError("unknown platform")
    name = safe_name(os.path.basename(filename.replace("\\", "/")))
    if not name or name.startswith("."):
        raise ValueError("bad file name")
    if os.path.splitext(name)[1].lower() in UPLOAD_BLOCK_EXT:
        raise ValueError("that file type isn't allowed as firmware")
    total = length if total is None else total
    if length <= 0 or total <= 0:
        raise ValueError("empty file")
    if total > UPLOAD_MAX:
        raise ValueError(f"file is larger than {UPLOAD_MAX // (1024*1024)} MB")
    if offset < 0 or offset + length > total:
        raise ValueError("bad chunk")
    uid = re.sub(r"[^A-Za-z0-9]", "", upload_id)[:40] or f"{os.getpid()}{threading.get_ident()}"
    add_platform(slug)
    d = os.path.join(FIRMWARE, slug)
    dest, tmp = os.path.join(d, name), os.path.join(d, f".upload-{uid}")
    if offset == 0 and os.path.exists(tmp):
        os.remove(tmp)
    if offset and (not os.path.exists(tmp) or os.path.getsize(tmp) != offset):
        raise ValueError("upload out of order, please retry")
    left, done = length, False
    try:
        with open(tmp, "ab") as fh:
            while left:
                chunk = stream.read(min(left, 1024 * 1024))
                if not chunk:
                    raise ValueError("upload interrupted")
                if offset == 0 and left == length:
                    if chunk[:2] == b"MZ" or chunk[:4] == b"\x7fELF":
                        raise ValueError("that's a program, not firmware")
                fh.write(chunk)
                left -= len(chunk)
        if offset + length == total:
            os.replace(tmp, dest)
            done = True
            log("firmware uploaded", slug, name, total)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return {"platform": slug, "file": name, "size": total, "done": done}


def delete_firmware(slug, filename):
    if slug not in SLUGS:
        raise ValueError("unknown platform")
    name = os.path.basename(filename)
    if not name or name.startswith(".") or name not in bios_files(slug):
        raise ValueError("no such firmware file")
    os.remove(os.path.join(FIRMWARE, slug, name))
    log("firmware removed", slug, name)
    return platform_info(slug)


# ---------------------------------------------------------------- web
PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8").read()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", cache=None):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype if ctype.startswith("image/") else ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                user = html.escape(self.headers.get("Remote-Name") or self.headers.get("Remote-User") or "")
                plats = json.dumps([[s, l] for s, l, _ in PLATFORMS])
                return self.send(200, PAGE.replace("__PLATFORMS__", plats).replace("__USER__", user), "text/html")
            if u.path == "/api/search":
                q = (qs.get("q") or [""])[0].strip()
                if len(q) < 2:
                    return self.send(400, {"error": "type at least 2 characters"})
                return self.send(200, search(q, any_category=(qs.get("kind") or [""])[0] == "bios"))
            if u.path == "/api/platforms":
                return self.send(200, platforms())
            if u.path == "/api/img":
                try:
                    data, ctype = romm_asset((qs.get("p") or [""])[0])
                except (ValueError, urllib.error.URLError):
                    return self.send(404, {"error": "no image"})
                return self.send(200, data, ctype, cache="private, max-age=86400")
            if u.path.startswith("/api/logo/"):
                try:
                    data, ctype = platform_logo(u.path.rsplit("/", 1)[-1])
                except (ValueError, urllib.error.URLError):
                    return self.send(404, {"error": "no logo"})
                return self.send(200, data, ctype, cache="private, max-age=604800")
            if u.path == "/api/config":
                return self.send(200, {"romm": ROMM_PUBLIC, "linked": bool(ROMM_TOKEN),
                                       "romm_error": romm_index()["error"]})
            if u.path == "/api/stats":
                # For dashboard widgets (e.g. Homepage "customapi"): small, cheap counts.
                try:
                    pending = sum(1 for q in queue() if q["state"] in ("downloading", "waiting"))
                except Exception:
                    pending = None
                return self.send(200, {"pending": pending, "roms": len(library()),
                                       "platforms": sum(1 for s_, _, _ in PLATFORMS if platform_exists(s_))})
            if u.path == "/api/library":
                return self.send(200, library())
            if u.path == "/api/queue":
                return self.send(200, queue())
            if u.path == "/healthz":
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            log("GET error:", u.path, repr(e))
            return self.send(502, {"error": str(e)})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/firmware":
                qs = urllib.parse.parse_qs(u.query)
                fname = urllib.parse.unquote(self.headers.get("X-Filename") or "")
                g = lambda k, d="": (qs.get(k) or [d])[0]
                info = upload_firmware(g("platform"), fname, n, self.rfile,
                                       offset=int(g("offset", "0")), total=int(g("total", str(n))),
                                       upload_id=g("id"))
                if info["done"]:
                    log("  by", self.headers.get("Remote-User", "?"))
                return self.send(200, info)
            body = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
            if self.path == "/api/grab":
                grab(str(body.get("id", "")), str(body.get("platform", "")),
                     str(body.get("kind") or "rom"))
                log("  by", self.headers.get("Remote-User", "?"))
                return self.send(200, {"ok": True})
            if self.path == "/api/firmware/delete":
                info = delete_firmware(str(body.get("platform", "")), str(body.get("file", "")))
                log("  by", self.headers.get("Remote-User", "?"))
                return self.send(200, info)
            if self.path == "/api/platforms":
                info = add_platform(str(body.get("platform", "")))
                log("  by", self.headers.get("Remote-User", "?"))
                return self.send(200, info)
            return self.send(404, {"error": "not found"})
        except PlatformMissing as e:
            return self.send(409, {"error": "platform_missing", "platform": platform_info(str(e))})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:
            log("POST error:", repr(e))
            return self.send(502, {"error": str(e)})


if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    load_status()
    threading.Thread(target=importer_loop, daemon=True).start()
    log(f"romseek on :{PORT}; library {LIBRARY}; category {CATEGORY}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
