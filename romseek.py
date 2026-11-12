#!/usr/bin/env python3
"""romseek: search Prowlarr for console games, send picks to qBittorrent,
and link finished downloads into RomM's library (optionally after a virus scan).

Stdlib only (runs on plain python:3.12-alpine like import-notify).
Sign-in: local accounts (first admin made by a one-time setup code in the log),
LLDAP passwords, or a trusted forward-auth proxy (PROXY_AUTH=true). Groups decide
which platforms and game ratings people see and whether they can download.

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
import shutil
import subprocess
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
    ("ngc", "GameCube", r"gamecube|\bngc\b|\bgc\b|\bgcm\b|\bgcz\b|(moon|stinky|purple)cube"),
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
# Systems whose files must come from the user's own console: no search presets,
# only "dump it yourself" guidance and the Upload button.
BIOS_DUMP_ONLY = {"switch", "wiiu", "3ds"}
# Firmware RomM wants as the zip itself (e.g. neogeo.zip), never extracted.
KEEP_ZIP = {"neogeoaes", "neogeomvs", "arcade",
            "switch"}  # Switch emulators install firmware from the zip itself
# A BIOS zip is only unpacked when it is small; anything bigger is a firmware
# pack/collection and is kept as the archive (hardlinked: no extra disk space).
EXTRACT_MAX_MEMBERS = 64
EXTRACT_MAX_BYTES = 512 * 1024 * 1024
# A torrent with this many archives is a collection (e.g. one zip per firmware
# version): keep every archive whole instead of merging their contents.
PACK_MIN_ARCHIVES = 3
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
                    "genres": (m.get("genres") or [])[:6],
                    "summary": (r.get("summary") or "")[:2500],
                    "identified": bool(r.get("is_identified")),
                    "rating": rating_tier(m.get("age_ratings")),
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
                  ".pbp", ".vpk", ".gcm", ".cdi", ".wux", ".wud"}

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
    # "_" and "." act as spaces in release names (Mario_Party_7_USA_NGC-STINKYCUBE)
    t = re.sub(r"[_.]+", " ", title.lower()) + " " + title.lower()
    for slug, _, rx in PLATFORMS:
        if re.search(rx, t):
            return slug
    return ""


class PlatformMissing(ValueError):
    pass


def platform_exists(slug):
    return os.path.isdir(os.path.join(LIBRARY, slug))


def bios_entries(slug):
    """Top-level firmware entries with sizes, for the Manage BIOS popup."""
    d = os.path.join(FIRMWARE, slug)
    out = []
    for name in bios_files(slug):
        p = os.path.join(d, name)
        try:
            if os.path.isdir(p):
                size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs)
                out.append({"name": name, "size": size, "dir": True})
            else:
                out.append({"name": name, "size": os.path.getsize(p), "dir": False})
        except OSError:
            continue
    return out


def bios_size(slug):
    return sum(e["size"] for e in bios_entries(slug))


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
        "bios_size": bios_size(slug),
        "games": game_count(slug),
        "emulators": [{"name": n, "url": u} for n, u in EMULATORS.get(slug, [])],
        "romm_id": romm_index()["platforms"].get(slug),
        "bios_query": BIOS_QUERY.get(slug, ""), "browser": slug in BROWSER,
        "bios_note": BIOS_NOTE.get(slug, ""),
        "bios_official": dict(zip(("label", "url"), BIOS_OFFICIAL[slug])) if slug in BIOS_OFFICIAL else None,
        "bios_dump": slug in BIOS_DUMP_ONLY,
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
        return {"romm_id": None, "rating": "Unrated"}
    return {"romm_id": r["id"], "title": r["name"], "fs_name": r["fs_name"],
            "cover": ("api/img?p=" + urllib.parse.quote(r["cover"], safe="")) if r["cover"] else "",
            "year": r["year"], "genres": r["genres"], "summary": r["summary"],
            "identified": r["identified"], "rating": r.get("rating", "Unrated")}


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
    detect(out)
    # ROM-specific indexer first, then anything we could place on a platform
    # (generic-"Console" hits are usually miscategorised PC releases), then seeds.
    out.sort(key=lambda r: (r["indexer"] != "BlueRoms", not r["platform"], -r["seeders"]))
    return out


# Words that describe a release, not the game, stripped before comparing titles.
_RELEASE_WORDS = set("""usa us u eur europe pal ntsc ntscu ntscj jap japan jpn multi multi2 multi3
multi4 multi5 multi6 en fr de es it nl eng english rare original rars rar zip 7z iso rom roms
nkit rvz wbfs gcm gcz ciso cso chd nsp xci repack proper fixed working full game
nintendo sony sega microsoft gamecube gc ngc wii wiiu switch nds ds 3ds gba gbc gb n64 snes
nes psx ps1 ps2 ps3 psp vita xbox dreamcast genesis megadrive saturn and the of for or""".split())


def title_key(title):
    """Normalized game title: drop [groups]/(tags), punctuation, release words."""
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", title.lower())
    t = re.sub(r"\bv?\d+(\.\d+)+\b", " ", t)          # version numbers
    words = [w for w in re.split(r"[^a-z0-9]+", t) if w and w not in _RELEASE_WORDS]
    return " ".join(words)


def suggest_from_siblings(results):
    """For results with no detected platform, suggest the platform most other
    results with the same game title were detected as (same search, so e.g.
    'Super Mario Sunshine NTSC' follows 'Super Mario Sunshine - Nintendo GameCube')."""
    votes = {}
    for r in results:
        if r["platform"]:
            k = title_key(r["title"])
            if k:
                votes.setdefault(k, {}).setdefault(r["platform"], 0)
                votes[k][r["platform"]] += 1 + (r["indexer"] == "BlueRoms")
    for r in results:
        r["suggested"] = ""
        if r["platform"]:
            continue
        k = title_key(r["title"])
        tally = votes.get(k)
        if not tally:   # fall back to titles whose words contain / are contained in this one
            tally, kw = {}, set(k.split())
            for vk, v in votes.items():
                vw = set(vk.split())
                nums = lambda ws: {w for w in ws if w.isdigit()}
                # sequels differ by number: "mario party 5" must not follow "mario party"
                if k and nums(kw) == nums(vw) and (kw <= vw or vw <= kw) and min(len(kw), len(vw)) >= 2:
                    for pl, n in v.items():
                        tally[pl] = tally.get(pl, 0) + n
        if tally:
            best = max(tally.items(), key=lambda kv: kv[1])
            if best[1] > sum(tally.values()) / 2:    # clear majority only
                r["suggested"] = best[0]


def result(rid):
    with LOCK:
        x = RESULTS.get(rid)
    if not x:
        raise ValueError("result expired, search again")
    return x


def grab(x, slug, kind="rom", learn=False, tags=()):
    """Send a Prowlarr result (the stored dict) to qBittorrent. Adds the platform if needed."""
    if slug not in SLUGS:
        raise ValueError("pick a platform")
    if kind not in ("rom", "bios"):
        raise ValueError("bad kind")
    add_platform(slug)
    fields = {"category": CATEGORY, "tags": ",".join(["romseek", f"{kind}:{slug}", *tags]), "savepath": SAVE_PATH}
    kind_, link_ = resolve_link(x)
    if kind_ == "torrent":
        qbit("torrents/add", fields, files={"torrents": ("rom.torrent", link_)})
    else:
        fields["urls"] = link_
        qbit("torrents/add", fields)
    log(f"grabbed ({kind_})", kind, slug, x.get("title"))
    if learn and kind == "rom":
        remember_choice(x.get("title", ""), slug)


def resolve_link(x):
    """("torrent", bytes) or ("magnet", str) for a Prowlarr result.

    Prowlarr puts the link in different fields per indexer: a real magnet in
    magnetUrl or guid, or a Prowlarr proxy URL in magnetUrl/downloadUrl that
    answers with a .torrent body or a redirect to a magnet."""
    magnet = next((v for v in (x.get("magnetUrl"), x.get("guid")) if (v or "").startswith("magnet:")), "")
    if magnet:
        return "magnet", magnet
    last_err = None
    for url in [v for v in (x.get("downloadUrl"), x.get("magnetUrl")) if (v or "").startswith("http")]:
        # Prowlarr builds proxy links as http://localhost:9696/... — point at its container.
        dl = re.sub(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?", PROWLARR_URL, url)
        try:
            content = http(dl, follow=False, timeout=60).read()
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "")
            if e.code in (301, 302, 303, 307, 308) and loc.startswith("magnet:"):
                return "magnet", loc
            last_err = ValueError(f"indexer refused the download (HTTP {e.code})")
            continue
        if content[:1] == b"d":
            return "torrent", content
        if content.startswith(b"magnet:"):
            return "magnet", content.decode(errors="ignore").strip()
        last_err = ValueError("indexer did not return a torrent or magnet")
    if x.get("infoHash"):
        # Last resort: bare info-hash magnet (DHT finds peers, just slower).
        return "magnet", f"magnet:?xt=urn:btih:{x['infoHash']}&dn={urllib.parse.quote(x.get('title', ''))}"
    raise last_err or ValueError("no usable download link")


# ---------------------------------------------------------------- detection
# (1) game-title database, (2) remembered choices, (3) file-list peek, (4) size checks.

# (1) No-Intro / Redump title lists from libretro-database: which systems each
# game came out on. Downloaded in the background, refreshed every 30 days.
TITLE_DB_URL = os.environ.get("TITLE_DB_URL", "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat")
TITLE_DATS = {
    "nes": ["no-intro/Nintendo - Nintendo Entertainment System"],
    "snes": ["no-intro/Nintendo - Super Nintendo Entertainment System"],
    "n64": ["no-intro/Nintendo - Nintendo 64"],
    "gb": ["no-intro/Nintendo - Game Boy"], "gbc": ["no-intro/Nintendo - Game Boy Color"],
    "gba": ["no-intro/Nintendo - Game Boy Advance"], "nds": ["no-intro/Nintendo - Nintendo DS"],
    "3ds": ["no-intro/Nintendo - Nintendo 3DS", "no-intro/Nintendo - Nintendo 3DS (Digital)"],
    "ngc": ["redump/Nintendo - GameCube"],
    "wii": ["redump/Nintendo - Wii", "no-intro/Nintendo - Wii (Digital)"],
    "wiiu": ["no-intro/Nintendo - Wii U (Digital)"],
    "genesis": ["no-intro/Sega - Mega Drive - Genesis"], "sms": ["no-intro/Sega - Master System - Mark III"],
    "gamegear": ["no-intro/Sega - Game Gear"], "sega32": ["no-intro/Sega - 32X"],
    "segacd": ["redump/Sega - Mega-CD - Sega CD"], "saturn": ["redump/Sega - Saturn"],
    "dc": ["redump/Sega - Dreamcast"], "psx": ["redump/Sony - PlayStation"],
    "ps2": ["redump/Sony - PlayStation 2"], "ps3": ["redump/Sony - PlayStation 3", "no-intro/Sony - PlayStation 3 (PSN)"],
    "psp": ["redump/Sony - PlayStation Portable", "no-intro/Sony - PlayStation Portable"],
    "psvita": ["no-intro/Sony - PlayStation Vita", "no-intro/Sony - PlayStation Vita (PSN)"],
    "xbox": ["redump/Microsoft - Xbox"], "atari2600": ["no-intro/Atari - 2600"],
    "tg16": ["no-intro/NEC - PC Engine - TurboGrafx 16"],
}
TITLES_FILE = os.path.join(STATE_DIR, "titles.json")
# Systems the lists don't cover (or only partly: Wii U is digital-only there).
NO_TITLE_DB = (SLUGS - set(TITLE_DATS)) | {"wiiu"}
_TITLES, _BY_FIRST, _TITLES_META = {}, {}, {"t": 0, "count": 0}


_BY_WORD = {}


def _index_titles(index):
    _TITLES.clear(); _BY_FIRST.clear(); _BY_WORD.clear()
    for k, plats in index.items():
        _TITLES[k] = set(plats)
        _BY_FIRST.setdefault(k.split()[0], []).append(k)
        for w in set(k.split()):
            _BY_WORD.setdefault(w, []).append(k)


def load_titles():
    try:
        with open(TITLES_FILE) as f:
            d = json.load(f)
        _index_titles(d["index"]); _TITLES_META.update(t=d["t"], count=len(d["index"]))
    except (OSError, ValueError, KeyError):
        pass


def refresh_titles(force=False):
    if not force and _TITLES and time.time() - _TITLES_META["t"] < 30 * 86400:
        return
    index, ok = {}, 0
    for slug, dats in TITLE_DATS.items():
        for dat in dats:
            url = f"{TITLE_DB_URL}/{urllib.parse.quote(dat)}.dat"
            try:
                text = http(url, headers={"User-Agent": "romseek"}, timeout=60).read().decode("utf-8", "replace")
            except Exception as e:
                log("title db: could not fetch", dat, repr(e)); continue
            ok += 1
            for name in re.findall(r'^\s*name "([^"]+)"', text, re.M)[1:]:   # [0] is the header
                k = title_key(name)
                if k:
                    index.setdefault(k, set()).add(slug)
    if not ok:
        return
    index = {k: sorted(v) for k, v in index.items()}
    tmp = TITLES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"t": int(time.time()), "index": index}, f)
    os.replace(tmp, TITLES_FILE)
    _index_titles(index); _TITLES_META.update(t=int(time.time()), count=len(index))
    log(f"title db: {len(index)} titles from {ok} lists")


def titles_loop():
    while True:
        try:
            refresh_titles()
        except Exception:
            log("title db error:", traceback.format_exc().strip().splitlines()[-1])
        time.sleep(86400)


def _nums(words):
    return {w for w in words if w.isdigit()}


def db_platforms(title):
    """Systems the game in this release name came out on ([] if not found).
    Exact title first; else the longest known title whose words are all in the
    release name (numbers must match exactly, so sequels don't collide)."""
    k = title_key(title)
    if not k or not _TITLES:
        return []
    if k in _TITLES:
        return sorted(_TITLES[k])
    words = k.split(); ws = set(words); n = _nums(words)
    best, best_len = set(), 2
    for first in ws:
        for cand in _BY_FIRST.get(first, ()):
            cw = cand.split()
            # only close partial matches: a known title of 3+ words, with at most
            # 2 leftover words (group tags etc.); "super mario party" must not
            # match "mario party" or "super mario"
            if len(cw) < best_len or len(cw) < 3 or len(ws) - len(set(cw)) > 2 \
                    or not set(cw) <= ws or _nums(cw) != n:
                continue
            if len(cw) > best_len:
                best, best_len = set(), len(cw)
            best |= _TITLES[cand]
    if not best and len(ws) >= 3:
        # release name dropped a word ("Zelda Ocarina of Time" vs "Legend of
        # Zelda, The - Ocarina of Time"): known title = release words + 1 extra
        rare = min(ws, key=lambda w: len(_BY_WORD.get(w, ())))
        for cand in _BY_WORD.get(rare, ()):
            cw = set(cand.split())
            if ws <= cw and len(cw) - len(ws) <= 1 and _nums(cw) == n:
                best |= _TITLES[cand]
    return sorted(best)


# (2) Platforms the user picked for titles RomSeek couldn't place.
LEARNED_FILE = os.path.join(STATE_DIR, "learned.json")
_LEARNED = {}


def load_learned():
    try:
        with open(LEARNED_FILE) as f:
            _LEARNED.update(json.load(f))
    except (OSError, ValueError):
        pass


def remember_choice(title, slug):
    k = title_key(title)
    if not k or slug not in SLUGS:
        return
    with LOCK:
        _LEARNED[k] = slug
        tmp = LEARNED_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_LEARNED, f)
        os.replace(tmp, LEARNED_FILE)


# (4) Size sanity. Generous upper bounds for ONE game (archives, multi-disc).
MAX_GAME_MB = {"nes": 8, "snes": 16, "gb": 8, "gbc": 16, "gba": 64, "n64": 128, "sms": 8,
               "gamegear": 8, "genesis": 16, "sega32": 16, "atari2600": 1, "tg16": 8,
               "nds": 1024, "ngc": 1600, "segacd": 1500, "dc": 1600, "psx": 3500, "saturn": 3000,
               "psp": 2500}
PACK_RX = re.compile(r"\b(pack|collection|complete|romset|rom ?set|full ?set|all games|\d{2,}\s*(games|roms|in ?1)|anthology|library|bundle)\b", re.I)


def size_ok(slug, size, title):
    m = MAX_GAME_MB.get(slug)
    return not m or not size or size <= m * 1024 * 1024 or bool(PACK_RX.search(title))


def detect(results):
    """Fill platform/suggestions for search results using all signals."""
    for r in results:
        title, size = r["title"], r["size"]
        db = [p for p in db_platforms(title) if size_ok(p, size, title)]
        r["candidates"], r["source"], r["suggested"], r["suggest_src"], r["size_warn"] = db, "", "", "", ""
        if r["platform"]:
            r["source"] = "title"
        elif len(db) == 1:
            r["platform"], r["source"] = db[0], "db"          # only ever released on one system
        if r["platform"] and not size_ok(r["platform"], size, title):
            r["size_warn"] = f"{size // (1024*1024)} MB is too big for one {LABEL[r['platform']]} game"
    suggest_from_siblings(results)                             # sets r["suggested"]
    for r in results:
        if r["platform"]:
            continue
        cands = r["candidates"]
        # the database can't rule out systems it doesn't cover (Switch, Wii U discs, ...)
        ruled_out = cands and r["suggested"] not in cands and r["suggested"] not in NO_TITLE_DB
        if r["suggested"] and (ruled_out or not size_ok(r["suggested"], r["size"], r["title"])):
            r["suggested"] = ""                               # siblings disagree with the database/size
        if r["suggested"]:
            r["suggest_src"] = "matching results"
        learned = _LEARNED.get(title_key(r["title"]))
        if learned:
            r["suggested"], r["suggest_src"] = learned, "your earlier choice"


# (3) Peek at a result's file list (torrent metadata only, nothing downloaded).
EXT_PLATFORM = {".nes": "nes", ".fds": "nes", ".sfc": "snes", ".smc": "snes", ".z64": "n64", ".n64": "n64",
                ".v64": "n64", ".gb": "gb", ".gbc": "gbc", ".gba": "gba", ".nds": "nds", ".3ds": "3ds",
                ".cia": "3ds", ".cci": "3ds", ".gcm": "ngc", ".gcz": "ngc", ".wbfs": "wii", ".wad": "wii",
                ".wud": "wiiu", ".wux": "wiiu", ".rpx": "wiiu", ".nsp": "switch", ".xci": "switch",
                ".nsz": "switch", ".cso": "psp", ".pbp": "psp", ".gdi": "dc", ".cdi": "dc", ".md": "genesis",
                ".gen": "genesis", ".sms": "sms", ".gg": "gamegear", ".32x": "sega32", ".pce": "tg16",
                ".a26": "atari2600", ".vpk": "psvita", ".xiso": "xbox"}


def _bdecode(b, i=0):
    c = b[i:i+1]
    if c == b"i":
        e = b.index(b"e", i); return int(b[i+1:e]), e + 1
    if c == b"l":
        i += 1; out = []
        while b[i:i+1] != b"e":
            v, i = _bdecode(b, i); out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1; out = {}
        while b[i:i+1] != b"e":
            k, i = _bdecode(b, i); v, i = _bdecode(b, i); out[k] = v
        return out, i + 1
    colon = b.index(b":", i); n = int(b[i:colon]); s0 = colon + 1
    return b[s0:s0+n], s0 + n


def _torrent_files(data):
    info = _bdecode(data)[0][b"info"]
    if b"files" in info:
        return [("/".join(p.decode("utf-8", "replace") for p in f[b"path"]), f[b"length"]) for f in info[b"files"]]
    return [(info[b"name"].decode("utf-8", "replace"), info[b"length"])]


def _magnet_hash(magnet):
    m = re.search(r"btih:([A-Za-z0-9]+)", magnet)
    if not m:
        raise ValueError("magnet has no info-hash")
    h = m.group(1)
    if len(h) == 32:
        import base64
        h = base64.b32decode(h.upper()).hex()
    return h.lower()


def peek(rid):
    with LOCK:
        x = RESULTS.get(rid)
    if not x:
        raise ValueError("result expired, search again")
    kind_, link_ = resolve_link(x)
    if kind_ == "torrent":
        files = _torrent_files(link_)
    else:
        h = _magnet_hash(link_)
        existing = json.loads(qbit(f"torrents/info?hashes={h}"))
        if not existing:
            qbit("torrents/add", {"urls": link_, "savepath": SAVE_PATH + "/.peek", "tags": "romseek-peek",
                                  "stopCondition": "MetadataReceived", "addToTopOfQueue": "true"})
        try:
            if not existing:
                qbit("torrents/setForceStart", {"hashes": h, "value": "true"})
            files, deadline = [], time.time() + 45
            while time.time() < deadline:
                fl = json.loads(qbit(f"torrents/files?hash={h}") or b"[]")
                if fl:
                    files = [(f["name"], f["size"]) for f in fl]; break
                time.sleep(1.5)
        finally:
            if not existing:
                qbit("torrents/delete", {"hashes": h, "deleteFiles": "true"})
        if not files:
            raise ValueError("no peers answered in time; try again or pick the platform yourself")
    votes = {}
    for name, size in files:
        p = EXT_PLATFORM.get(os.path.splitext(name)[1].lower()) or guess_platform(os.path.basename(name))
        if p:
            votes[p] = votes.get(p, 0) + size + 1
    plats = sorted(votes, key=lambda p: -votes[p])
    top = sorted(files, key=lambda f: -f[1])[:8]
    return {"platform": plats[0] if len(plats) == 1 or (plats and votes[plats[0]] > 0.9 * sum(votes.values())) else "",
            "candidates": plats, "count": len(files),
            "files": [{"name": n, "size": s} for n, s in top]}


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
    """Firmware goes into library/bios/<slug>/.

    - Small BIOS zips (<= EXTRACT_MAX_MEMBERS files, <= EXTRACT_MAX_BYTES) are
      unpacked by basename only (no path traversal), executables skipped.
    - Packs (>= PACK_MIN_ARCHIVES archives), big zips and KEEP_ZIP platforms are
      kept whole and hardlinked, so a pack of many firmware zips stays as
      separate, installable zips and uses no extra disk space.
    """
    import zipfile
    dest = os.path.join(FIRMWARE, slug)
    os.makedirs(dest, exist_ok=True)
    archives = [p for _, p in wanted if os.path.splitext(p)[1].lower() in (".zip", ".7z", ".rar")]
    is_pack = len(archives) >= PACK_MIN_ARCHIVES
    n, kept, notes = 0, 0, []
    for name, p in wanted:
        ext = os.path.splitext(p)[1].lower()
        extract = ext == ".zip" and slug not in KEEP_ZIP and not is_pack
        if extract:
            try:
                with zipfile.ZipFile(p) as z:
                    members = [m for m in z.infolist() if not m.is_dir()]
                    if len(members) > EXTRACT_MAX_MEMBERS or sum(m.file_size for m in members) > EXTRACT_MAX_BYTES:
                        extract = False
                    else:
                        for m in members:
                            base = safe_name(os.path.basename(m.filename))
                            if not base or base.startswith(".") or os.path.splitext(base)[1].lower() in JUNK_EXT:
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
                continue
        if not extract:
            if ext in (".7z", ".rar") and not is_pack and slug not in KEEP_ZIP:
                notes.append(f"{os.path.basename(p)} needs extracting by hand")
            kept += link(p, os.path.join(dest, safe_name(os.path.basename(p))))
    parts = []
    if n:
        parts.append(f"{n} file(s) unpacked")
    if kept:
        parts.append(f"{kept} archive(s) kept whole" if (is_pack or archives) else f"{kept} file(s)")
    detail = (", ".join(parts) or "nothing new") + f" -> bios/{slug}" + (f" ({'; '.join(notes)})" if notes else "")
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

    roms, bios, extras = split_files(wanted)
    parts, notes = [], []
    sets, roms, set_notes = archive_sets(roms)
    notes += set_notes
    state, detail, done, kept = unpack_torrent(h, sets, slug)
    if state:
        return state, detail
    notes += detail
    for s in kept:  # archives that stay packed (cartridge zips etc.) link as before
        roms += list(zip(s["names"], s["volumes"]))
    for s, files in done:   # disguised executables in unpacked content: block, like downloads
        for rel, q in files:
            if os.path.splitext(rel)[1].lower() in EXEC_EXT:
                continue
            with open(q, "rb") as fh:
                head = fh.read(4)
            if head[:2] == b"MZ" or head == b"\x7fELF":
                return "blocked", f"Windows/Linux executable disguised inside {s['stem']}: {rel}"
    if bios:
        _, d = import_bios(bios, slug)
        parts.append(f"BIOS: {d}")
    if extras:
        notes.append(f"skipped {len(extras)} emulator/tool file(s)")
    games = set()
    linked = link_roms(t, roms, slug, games)
    if done:
        linked += link_unpacked(done, slug, games, notes)
        notes.insert(0, f"unpacked {len(done)} archive(s)")
        shutil.rmtree(os.path.join(STAGING, h), ignore_errors=True)  # library keeps hardlinks
    t["_games"] = sorted(games)
    parts.insert(0, f"{linked} file(s) -> {slug}")
    return "imported", "; ".join(parts) + (f" ({'; '.join(notes)})" if notes else "")


# Folders/files inside a game torrent that are not games. BIOS goes to
# library/bios/<slug>/ (RomM firmware); emulators/tools are never imported:
# they are unverified binaries and RomSeek links the official emulators.
BIOS_RX = re.compile(r"\b(bios|firmware|ipl ?rom|bootrom|syscard)\b", re.I)
EXTRA_DIR_RX = re.compile(r"\b(emulators?|emus?|tools?|utilit(y|ies)|frontends?|programs?|apps?|software|patch(es|er)?|cheats?)\b", re.I)
# Emulator names unambiguous enough to match a file name ("Ecco the Dolphin" is a game).
EXTRA_FILE_RX = re.compile(r"\b(mupen\w*|project ?64|retroarch|openemu|dolphin[- ]?(emu|x64)|pcsx\w*|epsxe|duckstation|"
                           r"snes9x|zsnes|visualboy\w*|mgba|desmume|melonds|ppsspp|redream|ryujinx|citra|cemu|emulator)\b", re.I)


def split_files(wanted):
    """-> (roms, bios, extras), judged by the folders below the torrent root
    and (more strictly) the file name."""
    roms, bios, extras = [], [], []
    for name, p in wanted:
        parts = name.replace("\\", "/").split("/")
        dirs = " ".join(parts[1:-1]).replace("_", " ")
        fname = os.path.splitext(parts[-1])[0].replace("_", " ").replace(".", " ")
        if EXTRA_DIR_RX.search(dirs) or EXTRA_FILE_RX.search(fname):
            extras.append((name, p))
        elif BIOS_RX.search(dirs) or BIOS_RX.search(fname):
            bios.append((name, p))
        else:
            roms.append((name, p))
    return roms, bios, extras


def link_roms(t, roms, slug, names=None):
    """One RomM game per file for single-file formats (packs are flattened);
    multi-file games (cue/bin, gdi, ...) become one folder each. In a pack with
    per-game subfolders, each subfolder is its own game."""
    names = set() if names is None else names
    dest_dir = os.path.join(LIBRARY, slug)
    linked = 0
    groups = {}
    for name, p in roms:
        parts = name.replace("\\", "/").split("/")
        if len(roms) == 1 or os.path.splitext(p)[1].lower() in STANDALONE_EXT:
            top = safe_name(os.path.basename(p))
            linked += link(p, os.path.join(dest_dir, top))
            names.add(top)
        else:
            groups.setdefault(tuple(parts[:-1]), []).append((parts, p))
    if not groups:
        return linked
    # Per-subfolder games only when it clearly is a pack of game folders.
    subdirs = {k[1] for k in groups if len(k) >= 2}
    per_dir = len(subdirs) >= PACK_MIN_ARCHIVES and bool(PACK_RX.search(t["name"]))
    for key, files in groups.items():
        for parts, p in files:
            if per_dir and len(parts) >= 3:
                game, rel = parts[1], parts[2:]
            else:
                game, rel = t["name"], (parts[1:] if len(parts) > 1 else parts)
            linked += link(p, os.path.join(dest_dir, safe_name(game), *[safe_name(x) for x in rel]))
            names.add(safe_name(game))
    return linked


# ---------------------------------------------------------------- archives
# Disc images and multi-part/RAR archives are useless to emulators while packed,
# so they are unpacked into a staging folder under the downloads folder (on the
# same disk, so the library gets hardlinks). Single .zip/.7z files that only
# hold cartridge ROMs stay packed: emulators and RomM read those directly.
# Unpacked files pass the same virus-scan gate as downloads (when SCAN_STATE is
# set) plus an executable check before anything is linked.
# Must be on the same filesystem as the library (hardlinks) and inside the folder
# the virus scanner watches, so unpacked files get scanned like downloads.
STAGING = os.environ.get("EXTRACT_DIR", "/data/downloads/roms/.unpacked")
EXTRACT_MAX_GB = float(os.environ.get("EXTRACT_MAX_GB", "200"))        # per torrent
EXTRACT_FREE_MARGIN_GB = float(os.environ.get("EXTRACT_FREE_MARGIN_GB", "20"))
CART_EXT = {".nes", ".fds", ".unf", ".sfc", ".smc", ".z64", ".n64", ".v64", ".gb", ".gbc",
            ".gba", ".nds", ".md", ".gen", ".smd", ".sms", ".gg", ".32x", ".pce", ".a26",
            ".a78", ".lnx", ".ngp", ".ngc", ".ws", ".wsc", ".col", ".int", ".vb", ".min", ".rom"}
EXEC_EXT = {".exe", ".dll", ".scr", ".com", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".js",
            ".jar", ".lnk", ".sh", ".appimage", ".so", ".dylib"}
_PART_RAR = re.compile(r"^(.*)\.part0*(\d+)\.rar$", re.I)
_OLD_RAR = re.compile(r"^(.*)\.(rar|r\d{2,3})$", re.I)
_NUMBERED = re.compile(r"^(.*\.(?:7z|zip|rar))\.0*(\d+)$", re.I)
_SPLIT_ZIP = re.compile(r"^(.*)\.(zip|z\d{2})$", re.I)
_SINGLE = re.compile(r"^(.*)\.(7z|zip)$", re.I)


def _archive_key(p):
    """-> (set key, is_first_volume, stem) or None when p is not an archive volume."""
    d, b = os.path.dirname(p), os.path.basename(p)
    m = _PART_RAR.match(b)
    if m:
        return (d, m.group(1).lower(), "partrar"), int(m.group(2)) == 1, m.group(1)
    m = _NUMBERED.match(b)
    if m:
        stem = re.sub(r"\.(7z|zip|rar)$", "", m.group(1), flags=re.I)
        return (d, m.group(1).lower(), "numbered"), int(m.group(2)) == 1, stem
    m = _OLD_RAR.match(b)
    if m:
        return (d, m.group(1).lower(), "rar"), m.group(2).lower() == "rar", m.group(1)
    m = _SPLIT_ZIP.match(b)
    if m and m.group(2).lower() != "zip":
        return (d, m.group(1).lower(), "zip"), False, m.group(1)
    m = _SINGLE.match(b)
    if m:
        kind = "zip" if m.group(2).lower() == "zip" else "7z"
        return (d, m.group(1).lower(), kind), True, m.group(1)
    return None


def archive_sets(files):
    """Group (name, path) pairs into archive sets. -> (sets, other_files, notes)."""
    sets, other, notes = {}, [], []
    for name, p in files:
        k = _archive_key(p)
        if not k:
            other.append((name, p))
            continue
        key, first, stem = k
        s = sets.setdefault(key, {"stem": stem, "first": None, "volumes": [], "names": []})
        s["volumes"].append(p)
        s["names"].append(name)
        if first:
            s["first"] = p
    out = []
    for s in sets.values():
        if not s["first"]:
            notes.append(f"incomplete archive set {s['stem']} (first volume missing)")
            continue
        s["multi"] = len(s["volumes"]) > 1
        s["tool"] = "rar" if s["first"].lower().endswith(".rar") else "7z"
        out.append(s)
    return out, other, notes


def _run(cmd, timeout):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          errors="replace", timeout=timeout)


def list_archive(s):
    """-> [(member path, size)] files only, or None if unreadable/encrypted."""
    if s["tool"] == "rar":
        r = _run(["unrar", "lt", "-p-", s["first"]], 600)
        if r.returncode != 0:
            return None
        out, cur = [], {}
        for line in r.stdout.splitlines() + ["        Name: "]:
            k, _, v = line.strip().partition(":")
            k, v = k.strip(), v.strip()
            if k == "Name":
                if cur.get("Name") and cur.get("Type", "File") == "File":
                    out.append((cur["Name"], int(cur.get("Size", "0") or 0)))
                cur = {"Name": v}
            elif k in ("Type", "Size"):
                cur[k] = v
        return out
    r = _run(["7zz", "l", "-slt", "-ba", "-pnopassword", s["first"]], 600)
    if r.returncode != 0:
        return None
    out, cur = [], {}
    for line in r.stdout.splitlines() + ["Path = "]:
        k, _, v = line.partition(" = ")
        if k == "Path":
            if cur.get("Path") and cur.get("Folder") != "+":
                out.append((cur["Path"], int(cur.get("Size", "0") or 0)))
            cur = {"Path": v}
        elif k in ("Folder", "Size"):
            cur[k] = v
    return out


# Platforms whose games emulators want as the zip itself (MAME/Neo Geo sets).
KEEP_PACKED = {"neogeoaes", "neogeomvs", "arcade"}


def needs_unpack(s, members, slug):
    if slug in KEEP_PACKED:
        return False
    if s["multi"] or s["tool"] == "rar":
        return True
    exts = {os.path.splitext(m)[1].lower() for m, _ in members} - JUNK_EXT
    return not exts <= CART_EXT   # disc images, folders of files, nested archives...


def _unpack(s, outdir):
    tmp = outdir + ".part"
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    if s["tool"] == "rar":
        cmd = ["unrar", "x", "-p-", "-o+", "-y", "-idq", s["first"], tmp + "/"]
    else:
        cmd = ["7zz", "x", "-y", "-pnopassword", "-bso0", "-bsp0", f"-o{tmp}", s["first"]]
    r = _run(cmd, 4 * 3600)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return (r.stderr or r.stdout).strip().splitlines()[-1:] or ["unpack failed"]
    real = os.path.realpath(tmp)
    now = time.time()
    for root, dirs, fnames in os.walk(tmp):
        for n in dirs + fnames:
            q = os.path.join(root, n)
            if os.path.islink(q) or not os.path.realpath(q).startswith(real + os.sep):
                os.unlink(q)          # never follow links out of the staging folder
            elif n in fnames:
                os.utime(q, (now, now))  # fresh mtime: the virus scanner covers it next run
    os.rename(tmp, outdir)
    return None


def unpack_torrent(h, sets, slug):
    """Unpack what needs it into STAGING/<hash>/<n>/.
    -> (status, detail, [(set, files)]) with status None when ready to link."""
    stage = os.path.join(STAGING, h)
    todo, kept, notes = [], [], []
    for s in sets:
        members = list_archive(s)
        if members is None:
            notes.append(f"can't read {os.path.basename(s['first'])} (damaged or password-protected), kept packed")
            kept.append(s)
        elif needs_unpack(s, members, slug):
            # Split RAR5 members can under-report; never estimate below the packed size.
            s["size"] = max(sum(sz for _, sz in members),
                            sum(os.path.getsize(v) for v in s["volumes"]))
            todo.append(s)
        else:
            kept.append(s)
    if not todo:
        return None, notes, [], kept
    total = sum(s["size"] for s in todo)
    if total > EXTRACT_MAX_GB * 1e9:
        return "blocked", f"archives unpack to {total / 1e9:.0f} GB, over the {EXTRACT_MAX_GB:.0f} GB limit", [], kept
    for i, s in enumerate(todo):
        s["out"] = os.path.join(stage, str(i))
        if os.path.isdir(s["out"]):
            continue
        os.makedirs(stage, exist_ok=True)
        free = shutil.disk_usage(stage).free
        if free - s["size"] < EXTRACT_FREE_MARGIN_GB * 1e9:
            return "waiting", f"not enough disk space to unpack {s['stem']} ({s['size'] / 1e9:.1f} GB)", [], kept
        set_status(h, "unpacking", f"unpacking {s['stem']} ({i + 1}/{len(todo)}, {s['size'] / 1e9:.1f} GB)")
        log("unpacking", s["first"])
        err = _unpack(s, s["out"])
        if err:
            notes.append(f"unpack failed for {s['stem']}: {err[0][:120]}, kept packed")
            s["failed"] = True
    todo_ok = [s for s in todo if not s.get("failed")]
    kept += [s for s in todo if s.get("failed")]
    done = []
    newest = 0
    for s in todo_ok:
        files = []
        for root, _, fnames in os.walk(s["out"]):
            for n in fnames:
                q = os.path.join(root, n)
                files.append((os.path.relpath(q, s["out"]), q))
                newest = max(newest, os.path.getmtime(q))
        done.append((s, files))
    if SCAN_STATE and done:
        try:
            if newest > os.path.getmtime(SCAN_STATE):
                return "waiting", f"unpacked {len(done)} archive(s), waiting for virus scan", [], kept
        except OSError:
            return "waiting", "virus scanner state file not found", [], kept
    return None, notes, done, kept


def link_unpacked(done, slug, names, notes):
    """Each unpacked archive = one game: a lone ROM file is linked flat,
    anything else becomes a folder named after the archive (or its single top folder)."""
    dest_dir = os.path.join(LIBRARY, slug)
    linked = 0
    for s, files in done:
        keep = []
        for rel, q in files:
            ext = os.path.splitext(rel)[1].lower()
            if ext in JUNK_EXT or os.path.basename(rel).startswith("."):
                continue
            if ext in EXEC_EXT:
                notes.append(f"skipped program {os.path.basename(rel)}")
                continue
            keep.append((rel, q))
        roms, bios, extras = split_files([(f"x/{rel}", q) for rel, q in keep])
        if extras:
            notes.append(f"skipped {len(extras)} emulator/tool file(s) in {s['stem']}")
        if bios:
            _, d = import_bios(bios, slug)
            notes.append(f"BIOS: {d}")
        rels = [(n[2:], q) for n, q in roms]
        if not rels:
            continue
        tops = {r.split("/", 1)[0] for r, _ in rels}
        game = s["stem"]
        if len(tops) == 1 and all("/" in r for r, _ in rels):
            game = tops.pop()
            rels = [(r.split("/", 1)[1], q) for r, q in rels]
        if len(rels) == 1 and os.path.splitext(rels[0][0])[1].lower() in STANDALONE_EXT | CART_EXT:
            top = safe_name(os.path.basename(rels[0][0]))
            linked += link(rels[0][1], os.path.join(dest_dir, top))
            names.add(top)
            continue
        for r, q in rels:
            linked += link(q, os.path.join(dest_dir, safe_name(game), *[safe_name(x) for x in r.split("/")]))
        names.add(safe_name(game))
    return linked


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
                    req_imported(tags, t, slug, kind, state, detail)
            apply_pending_meta()
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
            "hash": t["hash"], "name": t["name"], "progress": t.get("progress", 0), "size": t.get("size", 0),
            "added": t.get("added_on", 0),
            "eta": t.get("eta"), "dlspeed": t.get("dlspeed", 0), "seeds": t.get("num_seeds", 0),
            "peers": t.get("num_leechs", 0), "seeds_total": t.get("num_complete", 0),
            "peers_total": t.get("num_incomplete", 0), "qstate": t.get("state", ""),
            "platform": slug, "kind": kind,
            "state": state, "detail": st.get("detail", ""),
        })
    return out


def stats():
    """Counts for dashboard widgets. Download numbers are None when qBittorrent is unreachable."""
    out = {"roms": len(library()),
           "platforms": sum(1 for s_, _, _ in PLATFORMS if platform_exists(s_)),
           "downloading": None, "waiting": None, "pending": None, "speed": None,
           "imported_7d": None, "blocked": None}
    try:
        torrents = json.loads(qbit(f"torrents/info?category={CATEGORY}"))
    except Exception:
        return out
    week = time.time() - 7 * 86400
    dl = wait = imp = blk = speed = 0
    for t in torrents:
        tags = {x.strip() for x in t.get("tags", "").split(",")}
        speed += t.get("dlspeed", 0) or 0
        if "blocked" in tags:
            blk += 1
        elif "imported" in tags:
            imp += (t.get("completion_on") or 0) >= week
        elif not finished(t):
            dl += 1
        else:
            wait += 1  # finished, waiting for the virus scan / import
    out.update(downloading=dl, waiting=wait, pending=dl + wait, speed=speed, imported_7d=imp, blocked=blk)
    return out


def cancel_download(h):
    """Cancel a RomSeek download that hasn't finished: delete the torrent and its
    partial data. Finished ones (waiting for the virus scan) can't be cancelled."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", h or ""):
        raise ValueError("bad torrent id")
    ts = json.loads(qbit("torrents/info?" + urllib.parse.urlencode({"category": CATEGORY, "hashes": h})))
    if not ts:
        raise ValueError("that download isn't in RomSeek's queue any more")
    t = ts[0]
    if finished(t):
        raise ValueError("already downloaded; it's waiting for the virus scan and will be imported")
    qbit("torrents/delete", {"hashes": h, "deleteFiles": "true"})
    STATUS.pop(h, None); save_status()
    log("download cancelled", t["name"])
    return {"ok": True, "name": t["name"]}


# ---------------------------------------------------------------- firmware upload
UPLOAD_MAX = int(os.environ.get("UPLOAD_MAX_MB", "1024")) * 1024 * 1024
# Never accept these as "firmware".
UPLOAD_BLOCK_EXT = {".exe", ".scr", ".com", ".pif", ".bat", ".cmd", ".msi", ".msp", ".hta",
                    ".cpl", ".jar", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1",
                    ".psm1", ".reg", ".lnk", ".url", ".dll", ".sh", ".html", ".htm", ".apk"}


def upload_firmware(slug, filename, length, stream, offset=0, total=None, upload_id="", stage=None):
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
    # stage: a holding folder for uploads that wait for approval (see requests)
    if stage:
        os.makedirs(stage, exist_ok=True)
        d = stage
    else:
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
            log("firmware uploaded" + (" (held for approval)" if stage else ""), slug, name, total)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return {"platform": slug, "file": name, "size": total, "done": done}


def delete_firmware(slug, filename):
    if slug not in SLUGS:
        raise ValueError("unknown platform")
    import shutil
    names = bios_files(slug) if filename == "*" else [os.path.basename(filename)]
    for name in names:
        if not name or name.startswith(".") or name not in bios_files(slug):
            raise ValueError("no such firmware file")
        p = os.path.join(FIRMWARE, slug, name)
        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
    log("firmware removed", slug, "ALL" if filename == "*" else names[0], f"({len(names)} entries)")
    return platform_info(slug)



# ---------------------------------------------------------------- admin: game management
# Admins (users in ADMIN_GROUPS or an admin group/flag; see "users, groups,
# permissions" below) get a per-game menu: metadata lookup/apply, manual edits, unmatch, remove.
ADMIN_GROUPS = {g.strip() for g in os.environ.get("ADMIN_GROUPS", "admins").split(",") if g.strip()}


def romm_call(method, path, fields=None, query=None, json_body=None, timeout=90):
    """Call the RomM API with the client token. `fields` -> multipart form."""
    if not ROMM_TOKEN:
        raise ValueError("RomM isn't linked (set ROMM_API_TOKEN)")
    url = ROMM_URL + path + ("?" + urllib.parse.urlencode(query) if query else "")
    hdr = {"Authorization": f"Bearer {ROMM_TOKEN}", "User-Agent": "romseek"}
    data = None
    if fields is not None:
        boundary = "----romseek" + hashlib.md5(os.urandom(8)).hexdigest()
        data = b"".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                        for k, v in fields.items()) + f"--{boundary}--\r\n".encode()
        hdr["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif json_body is not None:
        data, hdr["Content-Type"] = json.dumps(json_body).encode(), "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = None
        if e.code == 403:
            raise ValueError("RomM refused: the RomSeek API token needs the 'Edit ROMs' (roms.write) scope")
        raise ValueError(f"RomM: {detail or e.reason} ({e.code})")
    return json.loads(raw) if raw.strip() else {}


def romm_changed():
    _ROMM_CACHE["t"] = 0
    _FW_CACHE.clear()
    _IMG_CACHE.clear()


# GameTDB title lists: Wii/GameCube/Wii U/3DS/PS3/Switch releases are often named
# by disc/product ID (RZDP01, BLUS30443), which no metadata provider can match.
GAMETDB = {"wii": "wiitdb", "ngc": "wiitdb", "wiiu": "wiiutdb", "3ds": "3dstdb",
           "ps3": "ps3tdb", "switch": "switchtdb"}
GAMETDB_ART = {"wii": "wii", "ngc": "wii", "wiiu": "wiiu", "3ds": "3ds", "ps3": "ps3", "switch": "switch"}
_TDB, _TDB_T = {}, {}


def gametdb(db):
    if db in _TDB and time.time() - _TDB_T.get(db, 0) < 30 * 86400:
        return _TDB[db]
    path = os.path.join(STATE_DIR, db + ".txt")
    try:
        if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 30 * 86400:
            raw = http(f"https://www.gametdb.com/{db}.txt?LANG=EN", headers={"User-Agent": "romseek"}, timeout=60).read()
            if raw.startswith(b"TITLES"):
                with open(path + ".tmp", "wb") as f:
                    f.write(raw)
                os.replace(path + ".tmp", path)
        with open(path, encoding="utf-8", errors="replace") as f:
            _TDB[db] = dict(l.rstrip("\n").split(" = ", 1) for l in f if " = " in l and not l.startswith("TITLES"))
    except Exception as e:
        log("gametdb:", db, repr(e))
        _TDB.setdefault(db, {})
    _TDB_T[db] = time.time()
    return _TDB[db]


ID_RX = re.compile(r"\b([A-Z0-9]{6}|[A-Z]{4}-?\d{5}|[0-9A-F]{16})\b")


def disc_id_lookup(slug, name):
    """('RZDP01', 'The Legend of Zelda: Twilight Princess', cover_url) or None."""
    db = GAMETDB.get(slug)
    if not db:
        return None
    stem = os.path.splitext(name)[0].upper()
    tdb = gametdb(db)
    for m in ID_RX.finditer(stem):
        gid = m.group(1).replace("-", "")
        if gid.isalpha() or gid.isdigit():
            continue
        for key in ((gid, gid[:4]) if slug == "3ds" else (gid,)):
            if key in tdb:
                region = {"E": "US", "P": "EN", "J": "JA", "K": "KO", "D": "DE", "F": "FR"}.get(gid[3:4], "EN") \
                    if slug in ("wii", "ngc", "wiiu") else "US"
                return key, tdb[key], gametdb_cover(slug, key, region)
    return None


_COVER_OK = {}


def gametdb_cover(slug, key, region):
    """First GameTDB front cover that exists (region first, then US/EN)."""
    for reg in dict.fromkeys((region, "US", "EN")):
        url = f"https://art.gametdb.com/{GAMETDB_ART[slug]}/cover/{reg}/{key}.png"
        if url not in _COVER_OK:
            try:
                http(url, headers={"User-Agent": "romseek"}, timeout=10).read(64)
                _COVER_OK[url] = True
            except Exception:
                _COVER_OK[url] = False
        if _COVER_OK[url]:
            return url
    return ""


def _smart_title(k):
    small = {"of", "the", "a", "an", "and", "in", "on", "to", "vs"}
    return " ".join(w if w.isdigit() else (w if i and w in small else w.capitalize()) for i, w in enumerate(k.split()))


def title_suggestions(slug, term, limit=6):
    """Real game names close to `term` for this system: GameTDB (proper names) for
    Nintendo disc systems, otherwise the No-Intro/Redump title database.
    Scored mostly on how many of the typed words appear in the title, letters-only,
    so 'Windwaker' still finds 'The Wind Waker'."""
    words = set(title_key(term).split())
    if not words:
        return []
    def score(k):
        ws, compact = set(k.split()), k.replace(" ", "")
        cov = sum(1 for w in words if w in ws or (len(w) > 3 and w in compact)) / len(words)
        return cov, 0.75 * cov + 0.25 * len(ws & words) / len(ws | words)
    names = {}
    db = GAMETDB.get(slug)
    if db:
        for name in gametdb(db).values():
            name = re.sub(r"\s*\([^)]*\)", "", name).strip()     # drop (Demo), (Rev 1)…
            k = title_key(name)
            if k and (k not in names or len(name) < len(names[k])):
                names[k] = name
    else:
        names = {k: _smart_title(k) for k, plats in _TITLES.items() if slug in plats}
    scored = []
    for k, name in names.items():
        cov, sc = score(k)
        if cov >= 0.5:
            scored.append((sc, -len(k), name))
    scored.sort(reverse=True)
    out = []
    for sc, _, name in scored:
        if sc < 0.45 or len(out) >= limit:
            break
        if name.lower() != term.lower() and name not in out:
            out.append(name)
    return out


def library_entry(slug, name):
    if slug not in SLUGS or not name or "/" in name or name.startswith(".") or name in ("..",):
        raise ValueError("unknown game")
    path = os.path.join(LIBRARY, slug, name)
    if not os.path.lexists(path):
        raise ValueError("game not found on disk")
    return path


def clean_term(name):
    t = os.path.splitext(name)[0] if re.search(r"\.[A-Za-z0-9]{2,4}$", name) else name
    t = re.sub(r"[\[(][^\])]*[\])]", " ", t)
    t = re.sub(r"[._]+", " ", t)
    t = re.sub(r"\b(ps2|ps1|psx|ngc|wii|pal|ntsc|usa|eur|multi\d*|rare|iso|wbfs|rvz|nsp|xci)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -#")


PROVIDER_KEYS = [("igdb_id", "IGDB"), ("ss_id", "ScreenScraper"), ("moby_id", "MobyGames"),
                 ("launchbox_id", "LaunchBox"), ("flashpoint_id", "Flashpoint"), ("steam_id", "Steam"),
                 ("demozoo_id", "Demozoo"), ("libretro_id", "Libretro")]
RICH_KEYS = {"igdb_id", "ss_id", "moby_id", "launchbox_id", "flashpoint_id", "steam_id", "demozoo_id"}


def meta_search(slug, name, rom_id, term):
    hint = disc_id_lookup(slug, name)
    term = (term or "").strip() or (hint[1] if hint else clean_term(name))
    out, err = [], ""
    if hint:
        out.append({"source": "GameTDB", "name": hint[1], "cover": hint[2], "ids": {},
                    "summary": f"Disc / product ID {hint[0]}. GameTDB gives the title and cover only; "
                               "pick a LaunchBox/IGDB result for a description and details.",
                    "year": None, "platform": slug})
    def provider(q):
        try:
            return romm_call("GET", "/api/search/roms", query={"rom_id": rom_id, "search_term": q, "search_by": "name"}) or []
        except ValueError as e:
            nonlocal err
            err = str(e); return []
    suggestions = title_suggestions(slug, term)
    res = provider(term) if rom_id else []
    tried = [term]
    # Providers match names closely: if nothing came back, retry with the nearest real titles.
    for alt in suggestions:
        if not rom_id or sum(1 for r in res if any(r.get(k) for k in RICH_KEYS)) >= 3 or len(tried) >= 4:
            break
        res += provider(alt); tried.append(alt)
    seen = set()
    for r in res:
        if True:
            ids = {k: r[k] for k, _ in PROVIDER_KEYS if r.get(k)}
            key = tuple(sorted(ids.items()))
            if not ids or key in seen:
                continue
            seen.add(key)
            ms = r.get("first_release_date") or (r.get("launchbox_metadata") or {}).get("first_release_date")
            year = None
            if isinstance(ms, (int, float)) and ms > 0:
                year = time.gmtime(ms / 1000 if ms > 1e11 else ms).tm_year
            cover = r.get("url_cover") or next((r.get(k.split("_")[0] + "_url_cover") for k, _ in PROVIDER_KEYS
                                               if k in ids and r.get(k.split("_")[0] + "_url_cover")), "")
            out.append({"source": " + ".join(lbl for k, lbl in PROVIDER_KEYS if k in ids), "name": r.get("name") or "",
                        "cover": cover or "", "ids": ids, "summary": (r.get("summary") or "")[:600],
                        "year": year, "platform": slug})
    out.sort(key=lambda x: (not (set(x["ids"]) & RICH_KEYS)) if x["source"] != "GameTDB" else -1)
    return {"term": term, "results": out, "error": err, "tried": tried,
            "suggestions": [x for x in suggestions if x not in tried],
            "disc_id": {"id": hint[0], "title": hint[1]} if hint else None,
            "providers": romm_providers()}


_PROV = {"t": 0, "v": []}


def romm_providers():
    """Which metadata providers RomM has switched on (for the dialog's footer)."""
    if time.time() - _PROV["t"] < 600:
        return _PROV["v"]
    try:
        hb = json.loads(http(ROMM_URL + "/api/heartbeat", timeout=10).read())
        m = hb.get("METADATA_SOURCES") or {}
        _PROV["v"] = sorted(k.replace("_API_ENABLED", "").replace("_ENABLED", "").title()
                            for k, v in m.items() if v is True and k.endswith("API_ENABLED"))
    except Exception:
        pass
    _PROV["t"] = time.time()
    return _PROV["v"]


def meta_apply(rom_id, match):
    """Use a lookup result: provider IDs make RomM fetch full metadata itself."""
    rom_id = int(rom_id)
    fields = {k: str(v) for k, v in (match.get("ids") or {}).items() if k in dict(PROVIDER_KEYS)}
    if match.get("cover"):
        fields["url_cover"] = match["cover"]
    if not (set(fields) & RICH_KEYS) and match.get("name"):
        fields["name"] = match["name"]          # GameTDB / title-only result
    if not fields:
        raise ValueError("nothing to apply")
    r = romm_call("PUT", f"/api/roms/{rom_id}", fields=fields)
    romm_changed()
    log("metadata applied", rom_id, fields)
    return {"ok": True, "name": r.get("name")}


def meta_edit(rom_id, body):
    fields = {}
    for k in ("name", "summary", "url_cover"):
        if k in body:
            fields[k] = str(body[k] or "").strip()
    manual = {}
    if body.get("genres") is not None:
        manual["genres"] = [g.strip() for g in str(body["genres"]).split(",") if g.strip()]
    if body.get("year"):
        y = int(body["year"])
        if not 1970 <= y <= 2100:
            raise ValueError("year looks wrong")
        manual["first_release_date"] = int(time.mktime((y, 1, 1, 12, 0, 0, 0, 0, 0))) * 1000
    if manual:
        fields["raw_manual_metadata"] = json.dumps(manual)
    if not fields:
        raise ValueError("nothing to change")
    romm_call("PUT", f"/api/roms/{int(rom_id)}", fields=fields)
    romm_changed()
    log("metadata edited", rom_id, sorted(fields))
    return {"ok": True}


def meta_unmatch(rom_id):
    romm_call("PUT", f"/api/roms/{int(rom_id)}", fields={}, query={"unmatch_metadata": "true"})
    romm_changed()
    log("metadata unmatched", rom_id)
    return {"ok": True}


def game_torrents(slug, name):
    """qBittorrent torrents (category roms, tag rom:<slug>) that produced this library entry."""
    try:
        ts = json.loads(qbit("torrents/info?" + urllib.parse.urlencode({"category": CATEGORY, "tag": f"rom:{slug}"})))
    except Exception:
        return []
    out = []
    for t in ts:
        if t.get("name") == name or os.path.splitext(t.get("name", ""))[0] == os.path.splitext(name)[0]:
            out.append(t); continue
        try:
            files = json.loads(qbit("torrents/files?hash=" + t["hash"]))
        except Exception:
            continue
        if any(os.path.basename(f["name"]) == name or f["name"].split("/")[0] == name for f in files):
            out.append(t)
    return [{"hash": t["hash"], "name": t["name"]} for t in out]


def remove_game(slug, name, rom_id, delete_torrent):
    """Delete the library copy (and RomM's entry). Optionally drop the torrent + its download."""
    import shutil
    path = library_entry(slug, name)
    torrents = game_torrents(slug, name) if delete_torrent else []
    if rom_id:
        try:
            romm_call("POST", "/api/roms/delete", json_body={"roms": [int(rom_id)], "delete_from_fs": []})
        except ValueError as e:
            log("RomM delete failed (file still removed):", e)
    shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.remove(path)
    for t in torrents:
        qbit("torrents/delete", {"hashes": t["hash"], "deleteFiles": "true"})
        STATUS.pop(t["hash"], None)
    if torrents:
        save_status()
    romm_changed()
    log("game removed", slug, name, f"torrents: {len(torrents)}")
    return {"ok": True, "torrents": len(torrents)}



# ---------------------------------------------------------------- collections
# Admin-made collections ("The Legend of Zelda"): rules matched in the browser
# against the library (keywords in the title, systems, genres, year range).
COLLECTIONS_FILE = os.path.join(STATE_DIR, "collections.json")


def load_collections():
    try:
        with open(COLLECTIONS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_collection(body):
    cols = load_collections()
    name = str(body.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("give the collection a name")
    lst = lambda k, n=40: [str(x).strip()[:60] for x in (body.get(k) or []) if str(x).strip()][:n]
    yr = lambda k: int(body[k]) if str(body.get(k) or "").isdigit() and 1970 <= int(body[k]) <= 2100 else None
    col = {"id": str(body.get("id") or "") or hashlib.md5(os.urandom(8)).hexdigest()[:10],
           "name": name, "keywords": [k.lower() for k in lst("keywords")],
           "exclude": [k.lower() for k in lst("exclude")],
           "systems": [x for x in lst("systems") if x in SLUGS], "genres": lst("genres"),
           "year_from": yr("year_from"), "year_to": yr("year_to"), "updated": int(time.time())}
    if not (col["keywords"] or col["systems"] or col["genres"] or col["year_from"] or col["year_to"]):
        raise ValueError("add at least one rule (keyword, system, genre or year)")
    cols = [c for c in cols if c["id"] != col["id"]] + [col]
    cols.sort(key=lambda c: c["name"].lower())
    tmp = COLLECTIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cols, f, indent=1)
    os.replace(tmp, COLLECTIONS_FILE)
    log("collection saved", col["name"])
    return col


def delete_collection(cid):
    cols = load_collections()
    left = [c for c in cols if c["id"] != cid]
    if len(left) == len(cols):
        raise ValueError("no such collection")
    with open(COLLECTIONS_FILE + ".tmp", "w") as f:
        json.dump(left, f, indent=1)
    os.replace(COLLECTIONS_FILE + ".tmp", COLLECTIONS_FILE)
    log("collection deleted", cid)
    return {"ok": True}


_FW_CACHE = {}


def platform_firmware(slug):
    """BIOS files RomM knows for a system, with RomM download paths."""
    pid = romm_index()["platforms"].get(slug)
    if not pid or not ROMM_TOKEN:
        return []
    hit = _FW_CACHE.get(slug)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    items = romm_call("GET", "/api/firmware", query={"platform_id": pid}, timeout=30) or []
    out = [{"name": f["file_name"], "size": f.get("file_size_bytes") or 0,
            "path": f"/api/firmware/{f['id']}/content/{urllib.parse.quote(f['file_name'])}"}
           for f in items if not f.get("missing_from_fs")]
    out.sort(key=lambda f: f["name"].lower())
    _FW_CACHE[slug] = (time.time(), out)
    return out


# ---------------------------------------------------------------- users, groups, permissions
# Three ways in, all landing in one user list (state/users.json):
#   1. PROXY_AUTH=true: trust Remote-User/Remote-Groups from a forward-auth proxy
#      (Authelia, Authentik, ...). Users are created on first visit; their groups
#      follow the proxy on every request. Only enable when the app is reachable
#      solely through that proxy, or anyone can forge the headers.
#   2. LLDAP_URL: the sign-in form checks passwords against LLDAP; with
#      LLDAP_SYNC_USER/PASSWORD (a read-only account) every user is listed ahead
#      of their first sign-in.
#   3. Local accounts made by an admin (or the first-run setup) with a password.
# Groups grant what users see (platforms, game ratings) and do (download games,
# add platforms/BIOS). A user gets the union of their configured groups; users in
# no configured group get the "default" group.
import base64
import hmac
import secrets

PROXY_AUTH = os.environ.get("PROXY_AUTH", "").strip().lower() in ("1", "true", "yes", "on")
PROXY_USER_HDR = os.environ.get("PROXY_USER_HEADER", "Remote-User")
PROXY_GROUPS_HDR = os.environ.get("PROXY_GROUPS_HEADER", "Remote-Groups")
PROXY_NAME_HDR = os.environ.get("PROXY_NAME_HEADER", "Remote-Name")
PROXY_EMAIL_HDR = os.environ.get("PROXY_EMAIL_HEADER", "Remote-Email")
LLDAP_URL = os.environ.get("LLDAP_URL", "").rstrip("/")
LLDAP_LOGIN = bool(LLDAP_URL) and os.environ.get("LLDAP_LOGIN", "true").strip().lower() not in ("0", "false", "no", "off")
LLDAP_SYNC_USER = os.environ.get("LLDAP_SYNC_USER", "").strip()
LLDAP_SYNC_PASSWORD = os.environ.get("LLDAP_SYNC_PASSWORD", "")
LLDAP_SYNC_MINUTES = int(os.environ.get("LLDAP_SYNC_MINUTES", "15"))
# With the sync account set, LLDAP owns the group list: groups are made there, and
# RomSeek only sets what each one may see and do. Members of these LLDAP groups are
# service accounts (e.g. Authelia's bind user), not people, and are left out.
LLDAP_MANAGED = bool(LLDAP_URL and LLDAP_SYNC_USER)
LLDAP_SERVICE_GROUPS = {g.strip() for g in os.environ.get(
    "LLDAP_SERVICE_GROUPS", "lldap_strict_readonly,lldap_password_manager").split(",") if g.strip()}
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
COOKIE = "romseek_session"

USERS_FILE = os.path.join(STATE_DIR, "users.json")
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")
SETUP_FILE = os.path.join(STATE_DIR, "setup-code.txt")
AUTH_LOCK = threading.RLock()
_DB = {"users": {}, "groups": {}}
_SESS = {}
_SETUP = {"code": ""}
_FAILS = {}
_SYNC = {"t": 0, "error": "", "count": 0}

DEFAULT_GROUP = "default"
RATINGS = ["E", "E10+", "T", "M", "AO", "Unrated"]
RATING_HELP = {"E": "Everyone", "E10+": "Everyone 10+", "T": "Teen", "M": "Mature 17+",
               "AO": "Adults only 18+", "Unrated": "No rating yet (including games RomM hasn't matched)"}
_ESRB = {"EC": 0, "E": 0, "E10": 1, "E10+": 1, "T": 2, "M": 3, "AO": 4}
# Other boards IGDB reports (PEGI/USK/CERO/ACB/GRAC/ClassInd), mapped onto the ESRB tiers.
_OTHER = {"G": 0, "ALL": 0, "A": 0, "L": 0, "PG": 0, "B": 2, "C": 2, "D": 3, "Z": 3,
          "MA 15+": 3, "R18+": 3, "R 18+": 3, "RC": 4}
USERNAME_RX = re.compile(r"^[a-z0-9][a-z0-9._@-]{1,63}$")
GROUPNAME_RX = re.compile(r"^[\w][\w .+@-]{0,39}$")


class Denied(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def rating_tier(vals):
    """IGDB age ratings (mixed boards) -> one ESRB-style tier. ESRB wins when present."""
    esrb, other = None, None
    for v in vals or []:
        v = str(v).strip().upper()
        if v in _ESRB:
            esrb = max(esrb if esrb is not None else -1, _ESRB[v])
            continue
        t = _OTHER.get(v)
        m = re.fullmatch(r"(\d{1,2})\+?", v)
        if t is None and m:
            n = int(m[1])
            t = 0 if n <= 7 else 1 if n <= 11 else 2 if n <= 16 else 3
        if t is not None:
            other = max(other if other is not None else -1, t)
    t = esrb if esrb is not None else other
    return RATINGS[t] if t is not None else "Unrated"


def _write_private(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def save_db():
    with AUTH_LOCK:
        _write_private(USERS_FILE, _DB)


def save_sessions():
    with AUTH_LOCK:
        now = time.time()
        for k in [k for k, s in _SESS.items() if s["exp"] < now]:
            del _SESS[k]
        _write_private(SESSIONS_FILE, _SESS)


def load_auth():
    with AUTH_LOCK:
        try:
            with open(USERS_FILE) as f:
                d = json.load(f)
            _DB["users"], _DB["groups"] = d.get("users", {}), d.get("groups", {})
            for k in ("profiles", "ldap_groups", "ldap_ok_at"):
                if k in d:
                    _DB[k] = d[k]
        except FileNotFoundError:
            pass
        try:
            with open(SESSIONS_FILE) as f:
                _SESS.update(json.load(f))
        except (FileNotFoundError, ValueError):
            pass
        if DEFAULT_GROUP not in _DB["groups"]:
            # Fresh install: everyone sees everything and can download games;
            # adding platforms/BIOS is admin-only until an admin allows it.
            _DB["groups"][DEFAULT_GROUP] = {"platforms": None, "ratings": None,
                                            "download": True, "bios": False, "admin": False}
            _DB["profiles"] = [{"id": "admins", "name": "Admins: anything", "enabled": True, "who": [WHO_ADMINS],
                                "games": True, "bios": True, "platforms": None, "ratings": None}]
            save_db()
        if "profiles" not in _DB:
            # Upgrading from before requests existed: keep everyone's downloads instant
            # until an admin changes the profiles.
            _DB["profiles"] = [
                {"id": "admins", "name": "Admins: anything", "enabled": True, "who": [WHO_ADMINS],
                 "games": True, "bios": True, "platforms": None, "ratings": None},
                {"id": "everyone", "name": "Everyone: anything (as before requests)", "enabled": True,
                 "who": [WHO_ALL], "games": True, "bios": True, "platforms": None, "ratings": None}]
            save_db()
    refresh_setup_code()


def hash_password(pw):
    salt = os.urandom(16)
    it = 310000
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    return f"pbkdf2_sha256${it}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw, stored):
    try:
        algo, it, salt, dk = stored.split("$")
        assert algo == "pbkdf2_sha256"
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except Exception:
        return False


_DUMMY_HASH = hash_password(secrets.token_hex(8))


def check_new_password(pw):
    if len(pw) < 8:
        raise ValueError("use at least 8 characters for the password")
    if len(pw) > 256:
        raise ValueError("password is too long")


def user_groups(u):
    # SSO/LLDAP users: their provider is the source of truth. Only RomSeek accounts get RomSeek-set groups.
    if u.get("source", "local") != "local":
        return sorted(set(u.get("ext_groups") or []))
    return sorted(set(u.get("ext_groups") or []) | set(u.get("groups") or []))


def admin_groups():
    return ADMIN_GROUPS | {n for n, g in _DB["groups"].items() if g.get("admin") and n != DEFAULT_GROUP}


def user_is_admin(u):
    return bool(u.get("admin")) or bool(set(user_groups(u)) & admin_groups())


def perms_for(u):
    if user_is_admin(u):
        return {"admin": True, "platforms": None, "ratings": None, "download": True, "bios": True,
                "approve": True, "max_pending": None}
    cfg = [_DB["groups"][g] for g in user_groups(u) if g in _DB["groups"] and g != DEFAULT_GROUP]
    if not cfg:
        cfg = [_DB["groups"][DEFAULT_GROUP]]

    def union(k):
        if any(g.get(k) is None for g in cfg):
            return None  # some group allows all
        return sorted(set().union(*[set(g.get(k) or []) for g in cfg]))
    lims = [g.get("max_pending") for g in cfg]
    return {"admin": False, "platforms": union("platforms"), "ratings": union("ratings"),
            "download": any(g.get("download") for g in cfg), "bios": any(g.get("bios") for g in cfg),
            "approve": any(g.get("approve") for g in cfg),
            "max_pending": None if any(x is None for x in lims) else max(lims)}


def can_see_platform(p, slug):
    return p["platforms"] is None or slug in p["platforms"]


def can_see_game(p, g):
    return can_see_platform(p, g["platform"]) and (p["ratings"] is None or g.get("rating", "Unrated") in p["ratings"])


def needs_setup():
    """True until some admin can sign in. Not offered behind a trusted proxy (admins come from its groups)."""
    if PROXY_AUTH:
        return False
    with AUTH_LOCK:
        return not any(user_is_admin(u) for u in _DB["users"].values() if not u.get("disabled"))


def refresh_setup_code():
    """Fresh install: print a one-time code to the log so only whoever can read
    the server's log (or state folder) can create the first admin."""
    if not needs_setup():
        _SETUP["code"] = ""
        try:
            os.remove(SETUP_FILE)
        except OSError:
            pass
        return
    if not _SETUP["code"]:
        _SETUP["code"] = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        try:
            fd = os.open(SETUP_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(_SETUP["code"] + "\n")
        except OSError:
            pass
        log("=" * 60)
        log("No admin account yet. Open RomSeek and create one with setup code:")
        log("    " + _SETUP["code"])
        log(f"(also saved to {SETUP_FILE}; it changes on every restart until setup is done)")
        log("=" * 60)


def throttle(key, limit=8):
    now = time.time()
    with AUTH_LOCK:
        fails = [t for t in _FAILS.get(key, []) if now - t < 900]
        _FAILS[key] = fails
        if len(fails) >= limit:
            raise Denied(429, "too many attempts; try again in 15 minutes")


def failed(key):
    with AUTH_LOCK:
        _FAILS.setdefault(key, []).append(time.time())
        if len(_FAILS) > 5000:
            _FAILS.clear()


def new_session(uname):
    tok = secrets.token_urlsafe(32)
    with AUTH_LOCK:
        _SESS[hashlib.sha256(tok.encode()).hexdigest()] = {"user": uname, "exp": time.time() + SESSION_DAYS * 86400}
        save_sessions()
    return tok


def drop_sessions(uname=None, tok=None):
    with AUTH_LOCK:
        if tok:
            _SESS.pop(hashlib.sha256(tok.encode()).hexdigest(), None)
        if uname:
            for k in [k for k, s in _SESS.items() if s["user"] == uname]:
                del _SESS[k]
        save_sessions()


def session_user(tok):
    if not tok:
        return None
    with AUTH_LOCK:
        s = _SESS.get(hashlib.sha256(tok.encode()).hexdigest())
        if not s or s["exp"] < time.time():
            return None
        return s["user"]


def _hdr(h, name):
    # http.server decodes headers as latin-1; proxies send UTF-8 (e.g. accented display names).
    v = (h.get(name) or "").strip()
    try:
        return v.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return v


def touch_external(uname, source, name, email, groups):
    """Create or refresh a user from the proxy or LLDAP. Returns the record."""
    now = int(time.time())
    with AUTH_LOCK:
        u = _DB["users"].get(uname)
        changed = False
        if u is None:
            u = _DB["users"][uname] = {"source": source, "created": now, "groups": [], "admin": False, "disabled": False}
            changed = True
            log("user provisioned:", uname, f"({source})")
            audit("system", "user.provisioned", uname, source=source, groups=sorted(set(groups)))
        elif u.get("source") not in (source, "local") and source == "ldap":
            u["source"], changed = "ldap", True
        if not changed and u.get("ext_groups") is not None and sorted(set(groups)) != u.get("ext_groups"):
            audit("system", "user.groups_changed", uname, source=source, before=u.get("ext_groups"), after=sorted(set(groups)))
        for k, v in (("name", name or uname), ("email", email or ""), ("ext_groups", sorted(set(groups)))):
            if u.get(k) != v:
                u[k], changed = v, True
        if now - u.get("last_seen", 0) > 3600:
            u["last_seen"], changed = now, True
        if changed:
            save_db()
            if _SETUP["code"]:
                refresh_setup_code()
        return u


def proxy_user(h):
    uname = _hdr(h, PROXY_USER_HDR).lower()[:100]
    if not uname:
        return None
    groups = [g.strip() for g in re.split(r"[,|]", _hdr(h, PROXY_GROUPS_HDR)) if g.strip()]
    touch_external(uname, "proxy", _hdr(h, PROXY_NAME_HDR)[:100], _hdr(h, PROXY_EMAIL_HDR)[:200], groups)
    return uname


def cookie_value(h, name):
    for part in (h.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def resolve_user(h):
    """-> (username, record) or (None, None)."""
    uname = proxy_user(h) if PROXY_AUTH else None
    if not uname:
        uname = session_user(cookie_value(h, COOKIE))
    if not uname:
        return None, None
    with AUTH_LOCK:
        u = _DB["users"].get(uname)
        return (uname, dict(u)) if u else (None, None)


# --- LLDAP
def lldap_post(path, body, token=None):
    hdr = {"Content-Type": "application/json", "User-Agent": "romseek"}
    if token:
        hdr["Authorization"] = "Bearer " + token
    req = urllib.request.Request(LLDAP_URL + path, data=json.dumps(body).encode(), headers=hdr, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    if isinstance(d, dict) and d.get("errors"):
        raise ValueError("LLDAP: " + "; ".join(str(e.get("message")) for e in d["errors"]))
    return d


def lldap_token(user, password):
    try:
        return lldap_post("/auth/simple/login", {"username": user, "password": password})["token"]
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403, 404):
            return None
        raise


def lldap_login(uname, password):
    tok = lldap_token(uname, password)
    if not tok:
        return False
    q = "query($id:String!){user(userId:$id){id displayName email groups{displayName}}}"
    usr = (lldap_post("/api/graphql", {"query": q, "variables": {"id": uname}}, tok).get("data") or {}).get("user")
    if not usr:
        return False
    touch_external(usr["id"].lower(), "ldap", usr.get("displayName"), usr.get("email"),
                   [g["displayName"] for g in usr.get("groups") or []])
    return True


def lldap_sync():
    """List every LLDAP user (needs a read-only account). Users gone from LLDAP are removed."""
    if not (LLDAP_URL and LLDAP_SYNC_USER):
        return {"ok": False, "error": "set LLDAP_URL, LLDAP_SYNC_USER and LLDAP_SYNC_PASSWORD"}
    try:
        tok = lldap_token(LLDAP_SYNC_USER, LLDAP_SYNC_PASSWORD)
        if not tok:
            raise ValueError("LLDAP refused LLDAP_SYNC_USER's password")
        q = "{users{id displayName email groups{displayName}}}"
        users = lldap_post("/api/graphql", {"query": q}, tok)["data"]["users"]
        seen, real_groups = set(), set()
        for usr in users:
            uname = usr["id"].lower()
            groups = [g["displayName"] for g in usr.get("groups") or []]
            if uname == LLDAP_SYNC_USER.lower() or set(groups) & LLDAP_SERVICE_GROUPS:
                continue        # service account, not a person
            seen.add(uname)
            real_groups |= set(groups)
            touch_external(uname, "ldap", usr.get("displayName"), usr.get("email"), groups)
        with AUTH_LOCK:
            if _DB.get("ldap_groups") != sorted(real_groups):
                _DB["ldap_groups"] = sorted(real_groups)
                save_db()
        with AUTH_LOCK:
            gone = [n for n, u in _DB["users"].items() if u.get("source") == "ldap" and n not in seen]
            for n in gone:
                del _DB["users"][n]
                log("user removed (gone from LLDAP):", n)
                audit("system", "user.removed", n, reason="no longer in LLDAP (or a service account)")
            if gone:
                save_db()
        for n in gone:
            drop_sessions(n)
        _SYNC.update(t=time.time(), error="", count=len(seen))
        with AUTH_LOCK:
            if not _DB.get("ldap_ok_at"):
                _DB["ldap_ok_at"] = int(time.time())   # setup done: the sync password is never shown again
                save_db()
                audit("system", "settings.lldap_connected", LLDAP_SYNC_USER, users=len(seen))
    except Exception as e:
        _SYNC.update(t=time.time(), error=str(e))
        log("LLDAP sync error:", repr(e))
    return {"ok": not _SYNC["error"], "error": _SYNC["error"], "count": _SYNC["count"], "at": int(_SYNC["t"])}


def lldap_loop():
    while True:
        lldap_sync()
        time.sleep(max(1, LLDAP_SYNC_MINUTES) * 60)


# --- sign-in, setup, accounts
def login(uname, password, ip):
    uname = uname.strip().lower()
    key = f"{ip}|{uname}"
    throttle(key)
    throttle(ip, 20)
    throttle("user|" + uname, 30)  # X-Forwarded-For can be forged when the port is exposed
    with AUTH_LOCK:
        u = _DB["users"].get(uname)
        stored = (u or {}).get("password")
    if stored:
        ok = check_password(password, stored)
    elif LLDAP_LOGIN and (u is None or u.get("source") != "local"):
        try:
            ok = lldap_login(uname, password)
        except Exception as e:
            log("LLDAP login error:", repr(e))
            raise Denied(502, "couldn't reach LLDAP to check the password")
    else:
        check_password(password, _DUMMY_HASH)  # same timing as a real check
        ok = False
    if not ok:
        failed(key); failed(ip); failed("user|" + uname)
        log("sign-in failed:", uname, "from", ip)
        audit(uname, "auth.login_failed", uname)
        raise Denied(401, "wrong username or password")
    with AUTH_LOCK:
        u = _DB["users"][uname]
        if u.get("disabled"):
            audit(uname, "auth.login_failed", uname, reason="account disabled")
            raise Denied(403, "this account is disabled")
        u["last_seen"] = int(time.time())
        save_db()
    log("signed in:", uname)
    audit(uname, "auth.login", uname, via="LLDAP" if not stored else "password")
    return new_session(uname)


def setup_admin(body, ip):
    if not needs_setup() or not _SETUP["code"]:
        raise Denied(409, "setup is already done; sign in instead")
    throttle(ip)
    code = str(body.get("code") or "").strip().upper()
    if not hmac.compare_digest(code.encode(), _SETUP["code"].encode()):
        failed(ip)
        audit("", "auth.setup_failed", "", reason="wrong setup code")
        raise Denied(403, "wrong setup code; it's in the RomSeek server log")
    uname = str(body.get("username") or "").strip().lower()
    if not USERNAME_RX.match(uname):
        raise ValueError("username: 2-64 characters, letters, numbers, . _ - @")
    pw = str(body.get("password") or "")
    check_new_password(pw)
    with AUTH_LOCK:
        if uname in _DB["users"] and _DB["users"][uname].get("source") != "local":
            raise ValueError("that username belongs to an LLDAP account; pick another")
        _DB["users"][uname] = {"source": "local", "name": str(body.get("name") or uname).strip()[:100] or uname,
                               "email": str(body.get("email") or "").strip()[:200], "password": hash_password(pw),
                               "groups": [], "admin": True, "disabled": False,
                               "created": int(time.time()), "last_seen": int(time.time())}
        save_db()
    refresh_setup_code()
    log("setup done; first admin:", uname)
    audit(uname, "auth.setup", uname)
    return new_session(uname)


def change_password(uname, current, new):
    with AUTH_LOCK:
        u = _DB["users"].get(uname)
        if not u or not u.get("password"):
            raise ValueError("this account's password is managed by your sign-in provider")
        if not check_password(current, u["password"]):
            audit(uname, "auth.password_failed", uname)
            raise Denied(403, "current password is wrong")
        check_new_password(new)
        u["password"] = hash_password(new)
        save_db()
    drop_sessions(uname)
    log("password changed:", uname)
    audit(uname, "auth.password", uname)
    return new_session(uname)


def public_user(n, u):
    return {"username": n, "name": u.get("name") or n, "email": u.get("email") or "",
            "source": u.get("source", "local"), "groups": sorted(u.get("groups") or []),
            "ext_groups": sorted(u.get("ext_groups") or []), "admin": bool(u.get("admin")),
            "is_admin": user_is_admin(u), "disabled": bool(u.get("disabled")),
            "has_password": bool(u.get("password")), "created": u.get("created"), "last_seen": u.get("last_seen")}


def group_names():
    """Groups that have real people in them. With LLDAP managing groups that is LLDAP's
    list; otherwise the groups people arrive with plus RomSeek's own groups."""
    with AUTH_LOCK:
        names = set()
        for u in _DB["users"].values():
            names |= set(user_groups(u))
        if LLDAP_MANAGED:
            names |= set(_DB.get("ldap_groups") or [])
            names -= LLDAP_SERVICE_GROUPS
        else:
            names |= {n for n, g in _DB["groups"].items() if g.get("local")}
    names.discard(DEFAULT_GROUP)
    return sorted(names, key=str.lower)


def admin_overview():
    with AUTH_LOCK:
        users = sorted((public_user(n, u) for n, u in _DB["users"].items()), key=lambda x: x["username"])
        ext = {g for u in _DB["users"].values() for g in u.get("ext_groups") or []}
        members = {}
        for u in _DB["users"].values():
            for g in user_groups(u):
                members[g] = members.get(g, 0) + 1
        groups = []
        for n in [DEFAULT_GROUP] + group_names():
            g = _DB["groups"].get(n)
            groups.append({"name": n, "configured": g is not None, "external": n in ext or (LLDAP_MANAGED and n != DEFAULT_GROUP),
                           "local": bool(g and g.get("local")) and not LLDAP_MANAGED, "env_admin": n in ADMIN_GROUPS,
                           "members": members.get(n, 0) if n != DEFAULT_GROUP else
                           sum(1 for u in _DB["users"].values()
                               if not any(x in _DB["groups"] and x != DEFAULT_GROUP for x in user_groups(u))),
                           **{k: (g or {}).get(k, d) for k, d in (("platforms", []), ("ratings", []), ("download", False),
                                                                  ("bios", False), ("admin", False), ("approve", False),
                                                                  ("max_pending", None))}})
        profs = [dict(p) for p in profiles()]
    return {"users": users, "groups": groups, "ratings": [{"id": r, "label": RATING_HELP[r]} for r in RATINGS],
            "platforms": [{"slug": s, "label": l} for s, l, _ in PLATFORMS],
            "lldap": {"login": LLDAP_LOGIN, "sync": bool(LLDAP_URL and LLDAP_SYNC_USER), "error": _SYNC["error"],
                      "count": _SYNC["count"], "at": int(_SYNC["t"]), "managed": LLDAP_MANAGED},
            "profiles": profs, "gamedb": {"games": len(_LB["by_id"]), "at": _LB["t"], "error": _LB["error"],
                                          "building": _LB["building"]},
            "proxy": PROXY_AUTH, "admin_groups": sorted(ADMIN_GROUPS)}


def lldap_secret_shown():
    """The sync password may be shown to admins only until LLDAP has accepted it once."""
    return bool(LLDAP_MANAGED and LLDAP_SYNC_PASSWORD and not _DB.get("ldap_ok_at"))


def signin_overview():
    return {
        "proxy": {"on": PROXY_AUTH, "user_header": PROXY_USER_HDR, "groups_header": PROXY_GROUPS_HDR,
                  "name_header": PROXY_NAME_HDR, "email_header": PROXY_EMAIL_HDR},
        "lldap": {"url": LLDAP_URL, "login": LLDAP_LOGIN, "sync_user": LLDAP_SYNC_USER, "managed": LLDAP_MANAGED,
                  "minutes": LLDAP_SYNC_MINUTES, "error": _SYNC["error"], "count": _SYNC["count"], "at": int(_SYNC["t"]),
                  "connected_at": _DB.get("ldap_ok_at"), "secret_available": lldap_secret_shown(),
                  "service_groups": sorted(LLDAP_SERVICE_GROUPS)},
        "admin_groups": sorted(ADMIN_GROUPS), "local_login": True, "session_days": SESSION_DAYS,
        "gamedb": {"games": len(_LB["by_id"]), "at": _LB["t"], "error": _LB["error"], "building": _LB["building"]},
        "audit_days": AUDIT_DAYS,
    }


def lldap_secret(me):
    if not lldap_secret_shown():
        raise Denied(410, "LLDAP is set up, so the sync password is no longer shown. It's in romseek.env.")
    audit(me, "settings.secret_viewed", "LLDAP_SYNC_PASSWORD")
    return {"user": LLDAP_SYNC_USER, "password": LLDAP_SYNC_PASSWORD}


def _str_list(v, allowed):
    if v is None:
        return None
    if not isinstance(v, list):
        raise ValueError("expected a list")
    return sorted({str(x) for x in v if str(x) in allowed})


def save_group(body, me):
    name = str(body.get("name") or "").strip()
    if name != DEFAULT_GROUP and not GROUPNAME_RX.match(name):
        raise ValueError("group name: up to 40 letters, numbers, spaces, . + - @ _")
    lim = body.get("max_pending")
    if lim in ("", None):
        lim = None
    else:
        try:
            lim = int(lim)
        except (TypeError, ValueError):
            raise ValueError("pending request limit: a number, or blank for no limit")
        if not 0 <= lim <= 1000:
            raise ValueError("pending request limit: 0 to 1000, or blank for no limit")
    with AUTH_LOCK:
        existing = _DB["groups"].get(name)
        is_new = name not in group_names() and name != DEFAULT_GROUP
        if is_new and LLDAP_MANAGED:
            raise ValueError("groups come from LLDAP: create the group there and add people to it")
        if body.get("create") and (existing or not is_new):
            raise ValueError("a group with that name already exists")
        if is_new and not body.get("create"):
            raise ValueError("no such group")
        # null = all (including platforms added later); a missing key grants nothing
        g = {"platforms": _str_list(body.get("platforms", []), SLUGS), "ratings": _str_list(body.get("ratings", []), set(RATINGS)),
             "download": bool(body.get("download")), "bios": bool(body.get("bios")),
             "approve": bool(body.get("approve")), "max_pending": lim,
             "admin": bool(body.get("admin")) and name != DEFAULT_GROUP,
             "local": bool((existing or {}).get("local") or is_new)}
        before = user_is_admin(_DB["users"].get(me, {}))
        _DB["groups"][name] = g
        if before and not user_is_admin(_DB["users"].get(me, {})):
            if existing is None:
                del _DB["groups"][name]
            else:
                _DB["groups"][name] = existing
            raise ValueError("that would remove your own admin rights")
        save_db()
    refresh_setup_code()
    log("group saved:", name)
    audit(me, "group.created" if existing is None and is_new else "group.saved", name,
          changes={k: [(existing or {}).get(k), v] for k, v in g.items() if (existing or {}).get(k) != v and k != "local"})
    return {"ok": True}


def delete_group(name, me):
    if name == DEFAULT_GROUP:
        raise ValueError("the default group can't be removed")
    with AUTH_LOCK:
        g = _DB["groups"].get(name)
        if g is None:
            raise ValueError("that group has no settings to remove")
        before = user_is_admin(_DB["users"].get(me, {}))
        del _DB["groups"][name]
        if g.get("local") and not LLDAP_MANAGED:
            for u in _DB["users"].values():
                if name in (u.get("groups") or []):
                    u["groups"] = [x for x in u["groups"] if x != name]
        if before and not user_is_admin(_DB["users"].get(me, {})):
            _DB["groups"][name] = g
            raise ValueError("that would remove your own admin rights")
        save_db()
    log("group removed:", name)
    audit(me, "group.deleted" if g.get("local") and not LLDAP_MANAGED else "group.cleared", name)
    return {"ok": True}


def save_user(body, me):
    uname = str(body.get("username") or "").strip().lower()
    create = bool(body.get("create"))
    known = set(group_names())
    with AUTH_LOCK:
        u = _DB["users"].get(uname)
        if create:
            if not USERNAME_RX.match(uname):
                raise ValueError("username: 2-64 characters, letters, numbers, . _ - @")
            if u:
                raise ValueError("that username is taken")
            pw = str(body.get("password") or "")
            check_new_password(pw)
            u = {"source": "local", "password": hash_password(pw), "created": int(time.time()), "ext_groups": []}
        elif not u:
            raise ValueError("no such user")
        old = dict(u)
        if u.get("source") == "local":
            u["name"] = str(body.get("name") or uname).strip()[:100] or uname
            u["email"] = str(body.get("email") or "").strip()[:200]
            if not create and body.get("password"):
                check_new_password(str(body["password"]))
                u["password"] = hash_password(str(body["password"]))
        if u.get("source") == "local":
            u["groups"] = sorted({str(g) for g in body.get("groups") or [] if str(g) in known and str(g) != DEFAULT_GROUP})
        else:
            u["groups"] = []      # SSO/LLDAP users: groups are managed there
        u["admin"] = bool(body.get("admin"))
        u["disabled"] = bool(body.get("disabled"))
        if uname == me and (u["disabled"] or not user_is_admin(u)):
            _DB["users"][uname] = old
            raise ValueError("you can't disable yourself or take away your own admin rights")
        _DB["users"][uname] = u
        save_db()
    if u["disabled"] or (not create and body.get("password")):
        drop_sessions(uname)
    log("user", "created:" if create else "saved:", uname)
    ch = {k: [old.get(k), u.get(k)] for k in ("name", "email", "groups", "admin", "disabled") if old.get(k) != u.get(k)}
    if create:
        ch = {k: u.get(k) for k in ("groups", "admin") if u.get(k)}
    if not create and body.get("password") and u.get("source") == "local":
        ch["password"] = "reset"
    audit(me, "user.created" if create else "user.saved", uname, changes=ch)
    return public_user(uname, u)


def delete_user(uname, me):
    if uname == me:
        raise ValueError("you can't delete your own account")
    with AUTH_LOCK:
        if uname not in _DB["users"]:
            raise ValueError("no such user")
        del _DB["users"][uname]
        save_db()
    drop_sessions(uname)
    refresh_setup_code()
    log("user deleted:", uname)
    audit(me, "user.deleted", uname)
    return {"ok": True}


# ---------------------------------------------------------------- audit log
# One JSON object per line in state/audit.log: who did what, when, from where.
AUDIT_FILE = os.path.join(STATE_DIR, "audit.log")
AUDIT_DAYS = int(os.environ.get("AUDIT_DAYS", "90"))
AUDIT_LOCK = threading.Lock()
CTX = threading.local()   # per-request: client IP for audit entries


def audit(who, action, target="", **detail):
    e = {"t": int(time.time()), "user": who or "system", "action": action, "target": str(target or "")[:300]}
    ip = getattr(CTX, "ip", "")
    if ip:
        e["ip"] = ip
    d = {k: v for k, v in detail.items() if v not in (None, "", [], {})}
    if d:
        e["detail"] = d
    with AUDIT_LOCK:
        try:
            fd = os.open(AUDIT_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        except OSError as ex:
            log("audit write failed:", ex)


def _audit_lines():
    try:
        with open(AUDIT_FILE, encoding="utf-8") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def audit_prune():
    cutoff = time.time() - AUDIT_DAYS * 86400
    with AUDIT_LOCK:
        lines = _audit_lines()
        keep = []
        for ln in lines:
            try:
                if json.loads(ln)["t"] >= cutoff:
                    keep.append(ln)
            except (ValueError, KeyError, TypeError):
                continue
        if len(keep) != len(lines):
            tmp = AUDIT_FILE + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(x + "\n" for x in keep))
            os.replace(tmp, AUDIT_FILE)


def audit_loop():
    while True:
        try:
            audit_prune()
        except Exception as e:
            log("audit prune error:", repr(e))
        time.sleep(86400)


def audit_read(user="", action="", q="", before=0, limit=200):
    with AUDIT_LOCK:
        lines = _audit_lines()
    out, users, actions, q = [], set(), set(), q.strip().lower()
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        users.add(e.get("user", "")); actions.add(e.get("action", "").split(".")[0])
        if len(out) > limit:
            continue
        if before and e.get("t", 0) >= before:
            continue
        if user and e.get("user") != user:
            continue
        if action and not (e.get("action") == action or e.get("action", "").startswith(action + ".")):
            continue
        if q and q not in ln.lower():
            continue
        out.append(e)
    return {"entries": out[:limit], "more": len(out) > limit, "users": sorted(users), "actions": sorted(actions)}


# ---------------------------------------------------------------- game ratings before download
# LaunchBox's free games database (the one RomM uses) has ESRB ratings. RomSeek keeps a
# small index of it so a request can be checked against a group's ratings before anything
# is downloaded. Refreshed weekly.
# Ask which database game a download is before it starts (true by default). Off = only
# when an auto-approval profile depends on the rating, as before.
CONFIRM_MATCH = os.environ.get("CONFIRM_MATCH", "true").strip().lower() not in ("0", "false", "no", "off")
# Optional Discord webhook: a notice with the cover when a new game lands in RomM.
NOTIFY_WEBHOOK = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
LB_URL = os.environ.get("LAUNCHBOX_METADATA_URL", "https://gamesdb.launchbox-app.com/Metadata.zip")
LB_FILE = os.path.join(STATE_DIR, "launchbox.json")
LB_DAYS = int(os.environ.get("LAUNCHBOX_REFRESH_DAYS", "7"))
LB_PLATFORMS = {
    "nes": ["Nintendo Entertainment System"], "snes": ["Super Nintendo Entertainment System"],
    "n64": ["Nintendo 64"], "gb": ["Nintendo Game Boy"], "gbc": ["Nintendo Game Boy Color"],
    "gba": ["Nintendo Game Boy Advance"], "nds": ["Nintendo DS"], "3ds": ["Nintendo 3DS"],
    "ngc": ["Nintendo GameCube"], "wii": ["Nintendo Wii"], "wiiu": ["Nintendo Wii U"],
    "switch": ["Nintendo Switch"], "genesis": ["Sega Genesis"], "sms": ["Sega Master System"],
    "gamegear": ["Sega Game Gear"], "segacd": ["Sega CD", "Sega CD 32X"], "sega32": ["Sega 32X"],
    "saturn": ["Sega Saturn"], "dc": ["Sega Dreamcast"], "psx": ["Sony Playstation"],
    "ps2": ["Sony Playstation 2"], "psp": ["Sony PSP"], "psvita": ["Sony Playstation Vita"],
    "ps3": ["Sony Playstation 3"], "xbox": ["Microsoft Xbox"], "atari2600": ["Atari 2600"],
    "tg16": ["NEC TurboGrafx-16", "NEC TurboGrafx-CD"], "neogeoaes": ["SNK Neo Geo AES", "SNK Neo Geo MVS"],
    "arcade": ["Arcade"],
}
_LB_ESRB = {"EC": "E", "E": "E", "E10+": "E10+", "T": "T", "M": "M", "AO": "AO"}
_LB = {"by_plat": {}, "by_id": {}, "t": 0, "error": "", "building": False}


def lb_rating(v):
    return _LB_ESRB.get((v or "").split(" - ")[0].strip().upper(), "Unrated")


def lb_build():
    """Download LaunchBox Metadata.zip and keep only games on RomSeek's platforms."""
    import xml.etree.ElementTree as ET
    import zipfile
    part = os.path.join(STATE_DIR, "launchbox.zip.part")
    log("game database: downloading", LB_URL)
    with urllib.request.urlopen(urllib.request.Request(LB_URL, headers={"User-Agent": "romseek"}), timeout=120) as r, open(part, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    slug_of = {n: s for s, names in LB_PLATFORMS.items() for n in names}
    games, alts, covers, arts, details = {}, {}, {}, {}, {}
    rank = {"North America": 4, "United States": 4, None: 3, "World": 2}
    try:
        with zipfile.ZipFile(part) as z, z.open("Metadata.xml") as xf:
            it = ET.iterparse(xf, events=("start", "end"))
            _, root = next(it)
            n = 0
            for ev, el in it:
                if ev != "end" or el.tag not in ("Game", "GameAlternateName", "GameImage"):
                    continue
                if el.tag == "Game":
                    slug = slug_of.get(el.findtext("Platform"))
                    if slug:
                        try:
                            gid = int(el.findtext("DatabaseID"))
                        except (TypeError, ValueError):
                            gid = None
                        if gid:
                            y = el.findtext("ReleaseYear") or (el.findtext("ReleaseDate") or "")[:4]
                            games[gid] = [gid, el.findtext("Name") or "", slug, lb_rating(el.findtext("ESRB")),
                                          int(y) if y.isdigit() else None, "", []]
                            details[gid] = _lb_detail_row(gid, el)
                elif el.tag == "GameAlternateName":
                    try:
                        alts.setdefault(int(el.findtext("DatabaseID")), []).append(el.findtext("AlternateName") or "")
                    except (TypeError, ValueError):
                        pass
                elif el.findtext("Type") in ART_TYPES:
                    try:
                        gid = int(el.findtext("DatabaseID"))
                    except (TypeError, ValueError):
                        gid = None
                    sc = ART_TYPES[el.findtext("Type")] * 10 + rank.get(el.findtext("Region"), 1)
                    if gid and sc > arts.get(gid, (0, ""))[0]:
                        arts[gid] = (sc, el.findtext("FileName") or "")
                elif el.findtext("Type") == "Box - Front":
                    try:
                        gid = int(el.findtext("DatabaseID"))
                    except (TypeError, ValueError):
                        gid = None
                    sc = rank.get(el.findtext("Region"), 1)
                    if gid and sc > covers.get(gid, (0, ""))[0]:
                        covers[gid] = (sc, el.findtext("FileName") or "")
                el.clear()
                n += 1
                if n % 5000 == 0:
                    root.clear()
    finally:
        try:
            os.remove(part)
        except OSError:
            pass
    for gid, g in games.items():
        g[5] = covers.get(gid, (0, ""))[1]
        g[6] = [a for a in alts.get(gid, []) if a and a != g[1]][:8]
    tmp = LB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"t": int(time.time()), "games": list(games.values())}, f, ensure_ascii=False)
    os.replace(tmp, LB_FILE)
    lb_write_details(details, arts)
    log(f"game database: {len(games)} games indexed")


# Extra per-game details for notices (description, genres, players, background art). Kept in
# SQLite and read one game at a time, so the in-memory index stays small.
LB_DETAILS = os.path.join(STATE_DIR, "launchbox-details.sqlite")
LB_IMG = "https://images.launchbox-app.com/"
ART_TYPES = {"Fanart - Background": 3, "Screenshot - Gameplay": 2, "Screenshot - Game Title": 1}


def _lb_detail_row(gid, el):
    def num(tag):
        try:
            return float(el.findtext(tag) or "")
        except ValueError:
            return None
    overview = re.sub(r"\s+", " ", el.findtext("Overview") or "").strip()
    return (gid, overview[:900], (el.findtext("Genres") or "").replace(";", ","), el.findtext("Developer") or "",
            el.findtext("Publisher") or "", num("CommunityRating"), el.findtext("MaxPlayers") or "",
            el.findtext("ESRB") or "", "")


def lb_write_details(details, arts):
    import sqlite3
    tmp = LB_DETAILS + ".tmp"
    try:
        os.remove(tmp)
    except OSError:
        pass
    db = sqlite3.connect(tmp)
    db.execute("create table d (id integer primary key, overview text, genres text, developer text, publisher text,"
               " community real, players text, esrb text, art text)")
    db.executemany("insert into d values (?,?,?,?,?,?,?,?,?)",
                   [row[:8] + (arts.get(gid, (0, ""))[1],) for gid, row in details.items()])
    db.commit()
    db.close()
    os.replace(tmp, LB_DETAILS)


def lb_details(gid):
    """{overview, genres, developer, publisher, community, players, esrb, art (full URL)} or {}."""
    import sqlite3
    try:
        db = sqlite3.connect(f"file:{LB_DETAILS}?mode=ro", uri=True)
        try:
            row = db.execute("select overview, genres, developer, publisher, community, players, esrb, art"
                             " from d where id=?", (int(gid),)).fetchone()
        finally:
            db.close()
    except Exception:
        return {}
    if not row:
        return {}
    d = dict(zip(("overview", "genres", "developer", "publisher", "community", "players", "esrb", "art"), row))
    d["art"] = LB_IMG + d["art"] if d["art"] else ""
    return d


_DISC_RX = re.compile(r"\b(disc|disk|cd|dvd)\s*\d+\b|\b\d+\s*(discs|disks|cds)\b", re.I)


def _lb_key(s):
    return title_key(_DISC_RX.sub(" ", s))


def _usable(k):
    # Non-Latin alternate names reduce to e.g. "64"; a key needs a real word.
    return any(len(w) >= 3 and not w.isdigit() for w in k.split()) or (k and not k.isdigit() and len(k.split()) > 1)


def _title_words(title, slug):
    """A download's name as match words, without the system's name ("... - Nintendo 64")."""
    t = _DISC_RX.sub(" ", re.sub(r"\.[A-Za-z0-9]{2,4}$", "", title.strip()))
    for name in sorted({*LB_PLATFORMS.get(slug, []), LABEL.get(slug, ""), slug}, key=len, reverse=True):
        if name:
            t = re.sub(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", " ", t, flags=re.I)
    out = []
    for w in title_key(t).split():
        if w not in out:
            out.append(w)
    return out


def lb_load():
    try:
        with open(LB_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, ValueError):
        return False
    by_plat, by_id = {}, {}
    for gid, name, slug, rating, year, cover, alts in d.get("games", []):
        g = {"id": gid, "name": name, "platform": slug, "rating": rating, "year": year,
             "cover": f"https://images.launchbox-app.com/{cover}" if cover else "",
             "keys": [k for k in {_lb_key(x) for x in [name, *alts]} if _usable(k)]}
        by_plat.setdefault(slug, []).append(g)
        by_id[gid] = g
    _LB.update(by_plat=by_plat, by_id=by_id, t=d.get("t", 0))
    return True


def lb_loop():
    lb_load()
    while True:
        if time.time() - _LB["t"] > LB_DAYS * 86400 or not os.path.exists(LB_DETAILS):
            _LB["building"] = True
            try:
                lb_build()
                lb_load()
                _LB["error"] = ""
            except Exception as e:
                _LB["error"] = str(e)
                log("game database error:", repr(e))
            _LB["building"] = False
        time.sleep(6 * 3600 if not _LB["error"] else 3600)


def _nums(ws):
    return {w for w in ws if w.isdigit() and len(w) < 4}


def lb_score(g, tt, compact, nums, qt=()):
    """How well a database game fits a download's name (0 = not allowed)."""
    best = 0
    for k in g["keys"]:
        kw = k.split()
        kc = k.replace(" ", "")
        if qt and not all(w in kw or (len(w) > 3 and w in kc) for w in qt):
            continue
        if _nums(kw) != nums:          # "Mario Party 5" is not "Mario Party"
            continue
        cov = sum(1 for w in kw if w in tt or (len(w) > 3 and w in compact)) / len(kw)
        if cov < 0.6:
            continue
        back = sum(1 for w in tt if w in kw) / max(1, len(tt))
        best = max(best, 0.7 * cov + 0.3 * back)
    return best


def lb_candidates(slug, title, query="", limit=12):
    """Database games that fit the download's name. `query` narrows them but may only use
    words from that name, so nobody can match a different (lower-rated) game."""
    tw = _title_words(title, slug)
    tt, compact = set(tw), "".join(tw)
    qt = _lb_key(query).split() if query else []
    bad = [w for w in qt if w not in tt and not (len(w) > 3 and w in compact)]
    if bad:
        raise ValueError(f"search with words from the download's name (not: {', '.join(bad)})")
    nums = _nums(tw)
    out = []
    for g in _LB["by_plat"].get(slug, []):
        sc = lb_score(g, tt, compact, nums, qt)
        if sc:
            out.append((sc, g))
    out.sort(key=lambda x: (-x[0], len(x[1]["name"])))
    return [g for _, g in out[:limit]]


def lb_fits(slug, title, gid):
    g = _LB["by_id"].get(gid)
    if not g or g["platform"] != slug:
        return None
    tw = _title_words(title, slug)
    return g if lb_score(g, set(tw), "".join(tw), _nums(tw)) else None


def lb_auto(slug, title):
    """A single obvious match (the download's name is the game's name), else None."""
    k = " ".join(_title_words(title, slug))
    exact = [g for g in _LB["by_plat"].get(slug, []) if k in g["keys"]]
    if exact and len({g["rating"] for g in exact}) == 1:
        return exact[0]
    return None


def lb_public(g):
    return {"id": g["id"], "name": g["name"], "year": g["year"], "cover": g["cover"], "rating": g["rating"]}


# ---------------------------------------------------------------- requests + auto-approval profiles
# Every download (game, BIOS), new platform and firmware upload is a request. Profiles,
# checked in priority order, approve it automatically; the first that matches wins.
# Otherwise it waits in the approval queue for an admin or a group allowed to approve.
REQ_FILE = os.path.join(STATE_DIR, "requests.json")
UPLOAD_DIR = os.path.join(STATE_DIR, "uploads")
REQ_LOCK = threading.RLock()
_REQ = []
_UPLOADS = {}           # upload id -> holding folder (None = straight into place)
REQ_KEEP = 1000         # finished requests kept for history
WHO_ALL, WHO_ADMINS = "@everyone", "@admins"
KIND_LABEL = {"rom": "Game", "bios": "BIOS download", "platform": "New platform", "upload": "Firmware upload"}


def load_requests():
    with REQ_LOCK:
        try:
            with open(REQ_FILE) as f:
                _REQ[:] = json.load(f)
        except (FileNotFoundError, ValueError):
            pass


def save_requests():
    with REQ_LOCK:
        done = [r for r in _REQ if r["status"] != "pending"]
        if len(done) > REQ_KEEP:
            drop = {r["id"] for r in done[:len(done) - REQ_KEEP]}
            _REQ[:] = [r for r in _REQ if r["id"] not in drop]
        _write_private(REQ_FILE, _REQ)


def profiles():
    return _DB.setdefault("profiles", [])


def profile_applies(pr, u):
    who = set(pr.get("who") or [])
    mine = set(user_groups(u))
    if DEFAULT_GROUP in who and not any(g in _DB["groups"] and g != DEFAULT_GROUP for g in mine):
        return True     # people in no set-up group
    return WHO_ALL in who or (WHO_ADMINS in who and user_is_admin(u)) or bool(who & mine)


def decide(u, kind, slug, rating, new_platform=False):
    """-> ("approve", profile) | ("need_rating", None) | ("queue", None).
    rating None = not looked up yet; asked for only when a profile depends on it."""
    with AUTH_LOCK:
        for pr in profiles():
            if not pr.get("enabled", True) or not profile_applies(pr, u):
                continue
            if kind == "rom":
                if not pr.get("games") or (new_platform and not pr.get("bios")):
                    continue
            elif not pr.get("bios"):
                continue
            if pr.get("platforms") is not None and slug not in pr["platforms"]:
                continue
            if kind == "rom" and pr.get("ratings") is not None:
                if rating is None:
                    return "need_rating", None
                if rating not in pr["ratings"]:
                    continue
            return "approve", pr["name"]
    return "queue", None


def pending_count(uname):
    with REQ_LOCK:
        return sum(1 for r in _REQ if r["status"] == "pending" and r["user"] == uname)


def check_limit(uname, perms):
    lim = perms.get("max_pending")
    if lim is not None and pending_count(uname) >= lim:
        raise Denied(429, f"you already have {lim} request{'s' if lim != 1 else ''} waiting for approval; "
                          "wait for an admin, or cancel one in the Requests tab")


def _req_id():
    return secrets.token_hex(6)


def make_request(uname, u, perms, body):
    """A game/BIOS download or new platform. Approved straight away when a profile
    matches; otherwise queued. Returns {"status": approved|pending|need_match, ...}."""
    kind = str(body.get("kind") or "rom")
    slug = str(body.get("platform") or "")
    if kind not in ("rom", "bios", "platform"):
        raise ValueError("bad kind")
    if slug not in SLUGS:
        raise ValueError("pick a platform")
    if kind == "rom" and not perms.get("download"):
        raise Denied(403, "you don't have permission to request games")
    if kind != "rom" and not perms.get("bios"):
        raise Denied(403, "you don't have permission to request platforms or BIOS files")
    x = None
    if kind == "platform":
        if platform_exists(slug):
            return {"status": "approved", "already": True}
        title = LABEL[slug]
    else:
        x = result(str(body.get("id") or ""))
        title = x.get("title", "")
    new_platform = kind == "rom" and not platform_exists(slug)
    key = (x or {}).get("guid") or (x or {}).get("downloadUrl") or ""
    with REQ_LOCK:
        if any(r["status"] == "pending" and r["user"] == uname and r["kind"] == kind and r["platform"] == slug
               and (kind == "platform" or r.get("key") == key) for r in _REQ):
            raise ValueError("you've already asked for this; it's waiting for approval")
    rating, match, note = None, None, ""
    verdict, prof = decide(u, kind, slug, None, new_platform)
    # Every game is matched to the database before it downloads (CONFIRM_MATCH), so RomM
    # gets the right cover and details on import, not only when a profile needs the rating.
    if kind == "rom" and (CONFIRM_MATCH or verdict == "need_rating"):
        mid = body.get("match")
        if mid == "none":
            rating, note = "Unrated", "not in the game database (requester)"
        elif mid:
            try:
                g = lb_fits(slug, title, int(mid))
            except (TypeError, ValueError):
                g = None
            if not g:
                raise ValueError("pick one of the listed matches")
            match, rating, note = lb_public(g), g["rating"], "matched by requester"
        elif PACK_RX.search(title):
            rating, note = "Unrated", "a pack of several games"
        elif not _LB["by_id"]:
            rating, note = "Unrated", "game database not loaded yet"
        elif not _LB["by_plat"].get(slug):
            rating, note = "Unrated", "platform not in the game database"
        else:
            g = lb_auto(slug, title)
            if g and not CONFIRM_MATCH:
                match, rating, note = lb_public(g), g["rating"], "matched automatically"
            else:
                # Ask the requester to confirm; the obvious match (if any) is listed first.
                found = [lb_public(c) for c in lb_candidates(slug, title)]
                if g:
                    best = dict(lb_public(g), best=True)
                    found = [best] + [c for c in found if c["id"] != g["id"]]
                return {"status": "need_match", "term": clean_term(title), "title": title, "platform": slug,
                        "results": found, "confirm": CONFIRM_MATCH}
        verdict, prof = decide(u, kind, slug, rating, new_platform)
    if verdict != "approve":
        check_limit(uname, perms)
    rec = {"id": _req_id(), "t": int(time.time()), "user": uname, "kind": kind, "platform": slug, "title": title,
           "key": key, "result": x, "size": (x or {}).get("size") or 0, "indexer": (x or {}).get("indexer", ""),
           "seeders": (x or {}).get("seeders") or 0, "info": (x or {}).get("infoUrl") or "",
           "new_platform": new_platform, "match": match, "rating": rating, "rating_note": note,
           "learn": bool(body.get("learn")), "status": "pending", "profile": prof}
    if verdict == "approve":
        rec.update(status="approved", decided_by="auto", decided_t=rec["t"])
        try:
            execute(rec)
        except Exception as e:
            rec.update(status="failed", error=str(e))
            _store(rec)
            audit(uname, "request.failed", title, kind=kind, platform=slug, error=str(e))
            raise
        _store(rec)
        audit(uname, "request.auto_approved", title, kind=kind, platform=slug, profile=prof, rating=rating,
              match=(match or {}).get("name"))
        notify_request(rec)
    else:
        _store(rec)
        audit(uname, "request.created", title, kind=kind, platform=slug, rating=rating, match=(match or {}).get("name"))
        notify_request(rec)
        log("request queued:", uname, kind, slug, title)
    return {"status": rec["status"], "id": rec["id"], "profile": prof, "rating": rating, "match": match,
            "new_platform": new_platform}


def _store(rec):
    with REQ_LOCK:
        _REQ.append(rec)
        save_requests()


def request_upload(uname, slug, stage, info):
    rec = {"id": _req_id(), "t": int(time.time()), "user": uname, "kind": "upload", "platform": slug,
           "title": info["file"], "size": info["size"], "status": "pending", "stage": stage,
           "upload": {"file": info["file"], "size": info["size"]}}
    _store(rec)
    audit(uname, "request.created", info["file"], kind="upload", platform=slug)
    return rec


def execute(rec):
    """Carry out an approved request."""
    kind, slug = rec["kind"], rec["platform"]
    if kind in ("rom", "bios"):
        grab(rec["result"], slug, kind, learn=rec.get("learn"), tags=[f"req:{rec['id']}"])
        if rec.get("learn") and kind == "rom":
            remember_choice(rec["title"], slug)
    elif kind == "platform":
        add_platform(slug)
    elif kind == "upload":
        src = os.path.join(rec["stage"], rec["upload"]["file"])
        if not os.path.isfile(src):
            raise ValueError("the uploaded file is gone; ask them to upload it again")
        import shutil
        add_platform(slug)
        shutil.move(src, os.path.join(FIRMWARE, slug, rec["upload"]["file"]))
        _drop_stage(rec)


def _drop_stage(rec):
    import shutil
    st = rec.get("stage") or ""
    if st.startswith(UPLOAD_DIR + os.sep):
        shutil.rmtree(st, ignore_errors=True)


def find_request(rid):
    with REQ_LOCK:
        r = next((r for r in _REQ if r["id"] == rid), None)
    if not r:
        raise ValueError("no such request")
    return r


def decide_request(rid, approve, me, note=""):
    r = find_request(rid)
    with REQ_LOCK:
        if r["status"] != "pending":
            raise ValueError(f"this request was already {r['status']}")
        r["status"] = "working"
    note = str(note or "").strip()[:500]
    try:
        if approve:
            execute(r)
        elif r["kind"] == "upload":
            _drop_stage(r)
    except Exception as e:
        # Stays in the queue so it can be retried or denied.
        with REQ_LOCK:
            r.update(status="pending", error=str(e))
            save_requests()
        audit(me, "request.failed", r["title"], kind=r["kind"], platform=r["platform"], requested_by=r["user"], error=str(e))
        raise ValueError(f"couldn't start it: {e}. It's still in the queue; try again later or deny it.")
    r.update(status="approved" if approve else "denied", decided_by=me, decided_t=int(time.time()), note=note, error=None)
    with REQ_LOCK:
        save_requests()
    notify_request(r)
    audit(me, "request." + r["status"], r["title"], kind=r["kind"], platform=r["platform"], requested_by=r["user"], note=note)
    return public_request(r, True)


def cancel_request(rid, me, can_approve):
    r = find_request(rid)
    if r["user"] != me and not can_approve:
        raise Denied(403, "that isn't your request")
    with REQ_LOCK:
        if r["status"] != "pending":
            raise ValueError(f"this request was already {r['status']}")
        r.update(status="cancelled", decided_by=me, decided_t=int(time.time()))
        if r["kind"] == "upload":
            _drop_stage(r)
        save_requests()
    notify_request(r)
    audit(me, "request.cancelled", r["title"], kind=r["kind"], platform=r["platform"], requested_by=r["user"])
    return {"ok": True}


def public_request(r, full):
    with AUTH_LOCK:
        u = _DB["users"].get(r["user"]) or {}
    out = {k: r.get(k) for k in ("id", "t", "user", "kind", "platform", "title", "size", "indexer", "seeders", "info",
                                 "new_platform", "match", "rating", "rating_note", "status", "decided_t", "note",
                                 "profile", "error", "imported")}
    out["user_name"] = u.get("name") or r["user"]
    out["decided_by"] = r.get("decided_by") if full or r.get("decided_by") == "auto" else ("admin" if r.get("decided_by") else None)
    return out


def list_requests(uname, can_approve):
    with REQ_LOCK:
        mine = [r for r in _REQ if can_approve or r["user"] == uname]
    pending = [public_request(r, can_approve) for r in mine if r["status"] in ("pending", "working")]
    done = [public_request(r, can_approve) for r in reversed(mine) if r["status"] not in ("pending", "working")][:200]
    return {"pending": pending, "history": done, "can_approve": can_approve}


# ---------------------------------------------------------------- Discord notices
# NOTIFY_WEBHOOK_URL gets two kinds of card, laid out like Havoc's movie notices:
#   "New Request — Game": posted when a game is requested; its Status field is edited in
#   place (waiting → downloading → in RomM / denied / blocked).
#   "New Game": posted once the game is in RomM with its database match applied.
# One worker thread sends them in order, so an edit never overtakes the post it edits.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")   # e.g. https://roms.example.com
_NQ = None
_UA = "RomSeek (+https://github.com/Evident-Dev/romseek)"   # Discord's Cloudflare blocks Python's default UA


def _notify_worker():
    while True:
        job = _NQ.get()
        try:
            job()
        except Exception as e:
            log("discord notice failed:", repr(e))


def _queue(job):
    global _NQ
    if not NOTIFY_WEBHOOK:
        return
    if _NQ is None:
        import queue
        _NQ = queue.Queue()
        threading.Thread(target=_notify_worker, daemon=True).start()
    _NQ.put(job)


def _buttons(*links):
    """Link buttons under the card: (label, url, emoji). with_components=true lets a plain webhook send them."""
    row = [{"type": 2, "style": 5, "label": label, "url": url, **({"emoji": {"name": emoji}} if emoji else {})}
           for label, url, emoji in links if url]
    return [{"type": 1, "components": row}] if row else []


def _discord(embed, msg_id=None, buttons=()):
    """Post (returns the message id) or edit an earlier message."""
    body = json.dumps({"embeds": [embed], "components": list(buttons), "allowed_mentions": {"parse": []}}).encode()
    hdr = {"Content-Type": "application/json", "User-Agent": _UA}
    base = NOTIFY_WEBHOOK.split("?")[0]
    if msg_id:
        req = urllib.request.Request(f"{base}/messages/{msg_id}?with_components=true", data=body, headers=hdr,
                                     method="PATCH")
        urllib.request.urlopen(req, timeout=20).read()
        return msg_id
    return json.loads(http(base + "?wait=true&with_components=true", body, hdr, timeout=20).read()).get("id")


def _trim(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def _who(user):
    with AUTH_LOCK:
        return ((_DB.get("users") or {}).get(user) or {}).get("name") or user or "Someone"


def _game_card(author, color, footer, title, url, match, platform, user, fallback_rating=None):
    """Shared layout: cover thumbnail, description, background art, then fields."""
    d = lb_details(match["id"]) if match and match.get("id") else {}
    year = (match or {}).get("year")
    e = {"author": {"name": author}, "title": _trim(f"{title} ({year})" if year and str(year) not in title else title, 250),
         "color": color, "fields": [], "footer": {"text": footer},
         "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if url:
        e["url"] = url
    if d.get("overview"):
        e["description"] = _trim(d["overview"], 350)
    if (match or {}).get("cover"):
        e["thumbnail"] = {"url": match["cover"]}
    if d.get("art"):
        e["image"] = {"url": d["art"]}

    def add(name, value, inline=True):
        if value:
            e["fields"].append({"name": name, "value": str(value)[:1024], "inline": inline})
    add("🙋 Requested by", _who(user))
    add("🕹️ Platform", LABEL.get(platform, platform))
    add("🔞 Rated", d.get("esrb") or (match or {}).get("rating") or fallback_rating)
    add("🎭 Genres", ", ".join(g.strip() for g in (d.get("genres") or "").split(",")[:3] if g.strip()))
    add("👥 Players", d.get("players"))
    if d.get("community"):
        add("⭐ Rating", f"{d['community']:.1f}/5")
    add("🏢 Developer", d.get("developer"))
    return e, add


def req_status_text(r):
    st, imp = r.get("status"), r.get("imported")
    if imp == "imported":
        return "✅ In RomM"
    if imp:
        return "🛑 Blocked by the virus scan"
    note = f"\n“{_trim(r['note'], 200)}”" if r.get("note") else ""
    return {"pending": "⏳ Waiting for approval", "working": "⏳ Waiting for approval",
            "approved": "⬇️ Approved · downloading", "denied": "❌ Denied" + note,
            "cancelled": "🚫 Cancelled", "failed": "⚠️ Couldn't start the download"}.get(st, st or "?")


def notify_request(r):
    """Post or update the request card for a game request."""
    if r.get("kind") != "rom" or not NOTIFY_WEBHOOK:
        return
    snap = json.loads(json.dumps(r))

    def job():
        m = snap.get("match")
        e, add = _game_card("🙋 New Request — Game", 0xF5A623, "Havoc Society • Game Requests",
                            (m or {}).get("name") or clean_term(snap["title"]) or snap["title"],
                            f"{PUBLIC_URL}/#requests" if PUBLIC_URL else None, m, snap["platform"], snap["user"],
                            snap.get("rating"))
        if not m:
            add("📦 Download", _trim(snap["title"], 200), False)
        add("📊 Status", req_status_text(snap), False)
        btn = _buttons(("View Request", f"{PUBLIC_URL}/#requests" if PUBLIC_URL else "", "📋"))
        mid = _discord(e, snap.get("discord"), btn)
        if mid and not snap.get("discord"):
            with REQ_LOCK:
                live = next((x for x in _REQ if x["id"] == snap["id"]), None)
                if live is not None:
                    live["discord"] = mid
                    save_requests()
    _queue(job)


def notify_new_game(r, name, rom_id=None, games=1):
    """The "New Game" card once it's in RomM."""
    if not NOTIFY_WEBHOOK:
        return
    snap = json.loads(json.dumps(r))

    def job():
        url = (f"{ROMM_PUBLIC}/rom/{rom_id}" if rom_id else ROMM_PUBLIC) if ROMM_PUBLIC else None
        e, add = _game_card("🎮 New Game", 0x5B9BD5, "Havoc Society • New Games", name, url,
                            snap.get("match"), snap["platform"], snap["user"], snap.get("rating"))
        if games > 1:
            add("📦 Pack", f"{games} games")
        btn = []
        if url and rom_id:
            btn = _buttons(("Play in Browser", f"{url}/ejs" if snap["platform"] in BROWSER else "", "🎮"),
                           ("View in RomM", url, "🕹️"))
        elif url:
            btn = _buttons(("Open RomM", url, "🕹️"))
        _discord(e, buttons=btn)
        log("notified new game:", name)
    _queue(job)


def req_imported(tags, t, slug, kind, state, detail):
    """Importer hook: note the result on the request, queue the approved match for RomM."""
    rid = next((x[4:] for x in tags if x.startswith("req:")), "")
    who = ""
    if rid:
        with REQ_LOCK:
            r = next((r for r in _REQ if r["id"] == rid), None)
            if r:
                who = r["user"]
                r["imported"] = state
                games = t.get("_games") or []
                if state == "imported" and r.get("match") and kind == "rom" and len(games) == 1:
                    # The New Game notice waits for the match to be applied (apply_pending_meta).
                    r["apply"] = {"fs_name": games[0], "since": int(time.time())}
                elif state == "imported" and kind == "rom":
                    notify_new_game(r, clean_term(r["title"]) or r["title"], games=max(len(games), 1))
                save_requests()
                notify_request(r)
    audit("system", "download." + state, t["name"], platform=slug, kind=kind, requested_by=who, detail=detail)


def apply_pending_meta():
    """Point RomM at the database match the request was approved with, once RomM has scanned the game."""
    if not ROMM_TOKEN:
        return
    with REQ_LOCK:
        todo = [r for r in _REQ if r.get("apply")]
    if not todo:
        return
    idx = romm_index()
    for r in todo:
        a = r["apply"]
        rom = idx["roms"].get((idx["platforms"].get(r["platform"]), a["fs_name"]))
        if rom:
            name = r["match"].get("name") or a["fs_name"]
            try:
                res = meta_apply(rom["id"], {"ids": {"launchbox_id": r["match"]["id"]}})
                name = res.get("name") or name
                audit("system", "meta.apply", a["fs_name"], platform=r["platform"], match=r["match"]["name"], request=r["id"])
            except Exception as e:
                log("couldn't apply approved match:", a["fs_name"], repr(e))
            notify_new_game(r, name, rom["id"])
        if rom or time.time() - a["since"] > 2 * 86400:
            with REQ_LOCK:
                r.pop("apply", None)
                save_requests()


def save_profile(body, me):
    pid = str(body.get("id") or "")
    name = str(body.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("give the profile a name")
    who = body.get("who")
    if not isinstance(who, list) or not who:
        raise ValueError("choose who the profile applies to")
    known = set(group_names()) | {WHO_ALL, WHO_ADMINS, DEFAULT_GROUP}
    who = sorted({str(w) for w in who if str(w) in known})
    if not who:
        raise ValueError("choose who the profile applies to")
    pr = {"name": name, "enabled": bool(body.get("enabled", True)), "who": who,
          "games": bool(body.get("games")), "bios": bool(body.get("bios")),
          "platforms": _str_list(body.get("platforms", []), SLUGS),
          "ratings": _str_list(body.get("ratings", []), set(RATINGS))}
    if not (pr["games"] or pr["bios"]):
        raise ValueError("tick games, platforms/BIOS, or both")
    with AUTH_LOCK:
        ps = profiles()
        cur = next((p for p in ps if p["id"] == pid), None) if pid else None
        if pid and not cur:
            raise ValueError("no such profile")
        if cur:
            before = dict(cur)
            cur.update(pr)
            changes = {k: [before.get(k), v] for k, v in pr.items() if before.get(k) != v}
        else:
            pr["id"] = secrets.token_hex(4)
            ps.append(pr)
            changes = None
        save_db()
    audit(me, "profile.saved" if cur else "profile.created", name, changes=changes)
    return {"ok": True}


def delete_profile(pid, me):
    with AUTH_LOCK:
        ps = profiles()
        cur = next((p for p in ps if p["id"] == pid), None)
        if not cur:
            raise ValueError("no such profile")
        ps.remove(cur)
        save_db()
    audit(me, "profile.deleted", cur["name"])
    return {"ok": True}


def order_profiles(order, me):
    with AUTH_LOCK:
        ps = profiles()
        if not isinstance(order, list) or sorted(map(str, order)) != sorted(p["id"] for p in ps):
            raise ValueError("profile list changed; reload and try again")
        pos = {pid: i for i, pid in enumerate(map(str, order))}
        ps.sort(key=lambda p: pos[p["id"]])
        save_db()
    audit(me, "profile.reordered", "", order=[p["name"] for p in ps])
    return {"ok": True}


# ---------------------------------------------------------------- web
PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8").read()


class H(BaseHTTPRequestHandler):
    # Reachable without signing in. /api/stats is counts only, for dashboard widgets.
    PUBLIC = {"/", "/healthz", "/api/stats", "/api/auth/state", "/api/auth/login", "/api/auth/setup"}

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", cache=None, cookie=None):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype if ctype.startswith("image/") else ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        if cookie is not None:
            secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
            age = SESSION_DAYS * 86400 if cookie else 0
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax{secure}")
        if ctype == "text/html":
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(body)

    def ip(self):
        fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return fwd or self.client_address[0]

    def auth(self, path):
        """Sets self.who/self.u/self.p; raises Denied when the route needs a user."""
        CTX.ip = self.ip()
        self.who, self.u = resolve_user(self.headers)
        self.p = perms_for(self.u) if self.u else None
        if self.u and self.u.get("disabled"):
            self.who, self.u, self.p = None, None, None
            if path not in self.PUBLIC:
                raise Denied(403, "this account is disabled")
        if not self.u and path not in self.PUBLIC:
            raise Denied(401, "sign in first")

    def need(self, what):
        if not self.p or not self.p.get(what):
            raise Denied(403, {"admin": "admins only", "download": "you don't have permission to request games",
                               "bios": "you don't have permission to request platforms or BIOS files",
                               "approve": "only admins and approvers can do that"}[what])

    def need_platform(self, slug):
        if slug not in SLUGS:
            raise ValueError("unknown platform")
        if not can_see_platform(self.p, slug):
            raise Denied(403, "that platform isn't available to you")

    def by(self):
        log("  by", self.who or "?")

    def audit(self, action, target="", **detail):
        audit(self.who, action, target, **detail)

    def me(self):
        if not self.u:
            return None
        return {**public_user(self.who, self.u), "perms": self.p}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        g = lambda k, d="": (qs.get(k) or [d])[0]
        try:
            self.auth(u.path)
            if u.path == "/":
                plats = json.dumps([[s, l] for s, l, _ in PLATFORMS])
                return self.send(200, PAGE.replace("__PLATFORMS__", plats), "text/html")
            if u.path == "/healthz":
                return self.send(200, {"ok": True})
            if u.path == "/api/stats":
                # For dashboard widgets (e.g. Homepage "customapi"): small, cheap counts.
                return self.send(200, stats())
            if u.path == "/api/auth/state":
                return self.send(200, {"user": self.me(), "setup": needs_setup(), "local_login": True,
                                       "ldap_login": LLDAP_LOGIN, "proxy": PROXY_AUTH})
            if u.path == "/api/config":
                return self.send(200, {"romm": ROMM_PUBLIC, "linked": bool(ROMM_TOKEN), "admin": self.p["admin"],
                                       "ldap_managed": LLDAP_MANAGED,
                                       "perms": self.p, "user": self.me(), "romm_error": romm_index()["error"]})
            if u.path == "/api/search":
                bios = g("kind") == "bios"
                self.need("bios" if bios else "download")
                q = g("q").strip()
                if len(q) < 2:
                    return self.send(400, {"error": "type at least 2 characters"})
                return self.send(200, search(q, any_category=bios))
            if u.path == "/api/platforms":
                # All platforms (grabs into hidden ones still work); the page shows only visible ones.
                return self.send(200, [{**x, "visible": can_see_platform(self.p, x["slug"])} for x in platforms()])
            if u.path == "/api/firmware":
                self.need_platform(g("platform"))
                return self.send(200, bios_entries(g("platform")))
            if u.path == "/api/img":
                try:
                    data, ctype = romm_asset(g("p"))
                except (ValueError, urllib.error.URLError):
                    return self.send(404, {"error": "no image"})
                return self.send(200, data, ctype, cache="private, max-age=86400")
            if u.path.startswith("/api/logo/"):
                try:
                    data, ctype = platform_logo(u.path.rsplit("/", 1)[-1])
                except (ValueError, urllib.error.URLError):
                    return self.send(404, {"error": "no logo"})
                return self.send(200, data, ctype, cache="private, max-age=604800")
            if u.path == "/api/library":
                return self.send(200, [x for x in library() if can_see_game(self.p, x)])
            if u.path == "/api/queue":
                return self.send(200, queue())
            if u.path in ("/api/game/meta", "/api/game/torrents"):
                self.need("admin")
                library_entry(g("platform"), g("name"))
                if u.path == "/api/game/torrents":
                    return self.send(200, game_torrents(g("platform"), g("name")))
                return self.send(200, meta_search(g("platform"), g("name"), g("rom_id"), g("q")))
            if u.path == "/api/collections":
                return self.send(200, load_collections())
            if u.path == "/api/bios-files":
                self.need_platform(g("platform"))
                return self.send(200, platform_firmware(g("platform")))
            if u.path == "/api/admin/users":
                self.need("admin")
                return self.send(200, admin_overview())
            if u.path == "/api/admin/signin":
                self.need("admin")
                return self.send(200, signin_overview())
            if u.path == "/api/admin/audit":
                self.need("admin")
                return self.send(200, audit_read(g("user"), g("action"), g("q"), int(g("before", "0") or 0)))
            if u.path == "/api/requests":
                return self.send(200, list_requests(self.who, bool(self.p["approve"])))
            if u.path == "/api/request/match":
                self.need("download")
                x = result(g("id"))
                if g("platform") not in SLUGS:
                    raise ValueError("pick a platform")
                return self.send(200, {"term": g("q") or clean_term(x.get("title", "")), "title": x.get("title", ""),
                                       "results": [lb_public(m) for m in lb_candidates(g("platform"), x.get("title", ""), g("q"))]})
            return self.send(404, {"error": "not found"})
        except Denied as e:
            return self.send(e.code, {"error": str(e)})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:
            log("GET error:", u.path, repr(e))
            return self.send(502, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        try:
            # Cross-site form posts: the cookie is SameSite=Lax, and this rejects any foreign Origin.
            origin = self.headers.get("Origin")
            if origin:
                host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
                if urllib.parse.urlparse(origin).netloc != host.split(",")[0].strip():
                    raise Denied(403, "cross-site request refused")
            self.auth(path)
            n = int(self.headers.get("Content-Length") or 0)
            if path == "/api/firmware":
                self.need("bios")
                qs = urllib.parse.parse_qs(u.query)
                g = lambda k, d="": (qs.get(k) or [d])[0]
                slug = g("platform")
                self.need_platform(slug)
                fname = urllib.parse.unquote(self.headers.get("X-Filename") or "")
                offset, total = int(g("offset", "0")), int(g("total", str(n)))
                uid = re.sub(r"[^A-Za-z0-9]", "", g("id"))[:40] or secrets.token_hex(8)
                key = f"{self.who}|{uid}"
                if offset == 0:
                    # Decided once per upload: straight into RomM, or held until approved.
                    verdict, _ = decide(self.u, "upload", slug, None)
                    if verdict != "approve":
                        check_limit(self.who, self.p)
                    _UPLOADS[key] = None if verdict == "approve" else os.path.join(UPLOAD_DIR, secrets.token_hex(8))
                if key not in _UPLOADS:
                    raise ValueError("upload out of order, please retry")
                stage = _UPLOADS[key]
                info = upload_firmware(slug, fname, n, self.rfile, offset=offset, total=total, upload_id=uid, stage=stage)
                if info["done"]:
                    _UPLOADS.pop(key, None)
                    if stage:
                        rec = request_upload(self.who, slug, stage, info)
                        info["request"] = {"status": "pending", "id": rec["id"]}
                    else:
                        self.audit("firmware.uploaded", info["file"], platform=slug, size=info["size"])
                    self.by()
                return self.send(200, info)
            body = json.loads(self.rfile.read(min(n, 200000)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            s = lambda k: str(body.get(k) or "")

            # --- sign-in
            if path == "/api/auth/login":
                tok = login(s("username"), s("password"), self.ip())
                return self.send(200, {"ok": True}, cookie=tok)
            if path == "/api/auth/setup":
                tok = setup_admin(body, self.ip())
                return self.send(200, {"ok": True}, cookie=tok)
            if path == "/api/auth/logout":
                if self.who:
                    self.audit("auth.logout", self.who)
                drop_sessions(tok=cookie_value(self.headers, COOKIE))
                return self.send(200, {"ok": True}, cookie="")
            if path == "/api/auth/password":
                tok = change_password(self.who, s("current"), s("new"))
                return self.send(200, {"ok": True}, cookie=tok)

            # --- admin: users and groups
            if path.startswith("/api/admin/"):
                self.need("admin")
                act = path[len("/api/admin/"):]
                if act == "users":
                    r = save_user(body, self.who)
                elif act == "users/delete":
                    r = delete_user(s("username"), self.who)
                elif act == "groups":
                    r = save_group(body, self.who)
                elif act == "groups/delete":
                    r = delete_group(s("name"), self.who)
                elif act == "sync":
                    r = lldap_sync()
                    self.audit("user.sync", "LLDAP", ok=r.get("ok"), count=r.get("count"), error=r.get("error"))
                elif act == "lldap/secret":
                    r = lldap_secret(self.who)
                elif act == "profiles":
                    r = save_profile(body, self.who)
                elif act == "profiles/delete":
                    r = delete_profile(s("id"), self.who)
                elif act == "profiles/order":
                    r = order_profiles(body.get("order"), self.who)
                else:
                    return self.send(404, {"error": "not found"})
                self.by()
                return self.send(200, r)

            if path.startswith("/api/game/"):
                self.need("admin")
                act = path.rsplit("/", 1)[-1]
                rid = body.get("rom_id")
                if act == "remove":
                    r = remove_game(s("platform"), s("name"), rid, bool(body.get("delete_torrent")))
                elif not rid:
                    raise ValueError("RomM hasn't scanned this game yet; try again in a minute")
                elif act == "apply":
                    r = meta_apply(rid, body.get("match") or {})
                elif act == "edit":
                    r = meta_edit(rid, body)
                elif act == "unmatch":
                    r = meta_unmatch(rid)
                else:
                    return self.send(404, {"error": "not found"})
                self.by()
                self.audit({"remove": "game.removed"}.get(act, "meta." + act), s("name") or str(rid), platform=s("platform"),
                           match=(body.get("match") or {}).get("name"), delete_torrent=body.get("delete_torrent"))
                return self.send(200, r)
            if path in ("/api/collections", "/api/collections/delete"):
                self.need("admin")
                r = save_collection(body) if path == "/api/collections" else delete_collection(s("id"))
                self.by()
                self.audit("collection." + ("saved" if path == "/api/collections" else "deleted"), s("name") or s("id"))
                return self.send(200, r)
            if path == "/api/cancel":
                self.need("admin")
                r = cancel_download(s("hash").lower())
                self.by()
                self.audit("download.cancelled", s("name") or s("hash"))
                return self.send(200, r)
            if path == "/api/peek":
                if not (self.p["download"] or self.p["bios"]):
                    self.need("download")
                return self.send(200, peek(s("id")))
            if path in ("/api/request", "/api/grab"):
                r = make_request(self.who, self.u, self.p, body)
                self.by()
                return self.send(200, r)
            if path == "/api/platforms":
                r = make_request(self.who, self.u, self.p, {"kind": "platform", "platform": s("platform")})
                return self.send(200, {**r, "platform": platform_info(s("platform"))})
            if path in ("/api/request/approve", "/api/request/deny"):
                self.need("approve")
                return self.send(200, decide_request(s("id"), path.endswith("approve"), self.who, s("note")))
            if path == "/api/request/cancel":
                return self.send(200, cancel_request(s("id"), self.who, bool(self.p["approve"])))
            if path == "/api/firmware/delete":
                self.need("approve")
                self.need_platform(s("platform"))
                info = delete_firmware(s("platform"), s("file"))
                self.by()
                self.audit("firmware.deleted", s("file"), platform=s("platform"))
                return self.send(200, info)
            return self.send(404, {"error": "not found"})
        except Denied as e:
            return self.send(e.code, {"error": str(e)})
        except PlatformMissing as e:
            return self.send(409, {"error": "platform_missing", "platform": platform_info(str(e)),
                                   "can_add": bool(self.p and self.p["bios"])})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:
            log("POST error:", repr(e))
            return self.send(502, {"error": str(e)})


def cli_passwd(uname):
    """Locked out? `docker exec -it romseek python3 /app/romseek.py passwd NAME`, then restart."""
    import getpass
    load_auth()
    uname = uname.strip().lower()
    pw = getpass.getpass(f"New password for {uname}: ")
    check_new_password(pw)
    if getpass.getpass("Again: ") != pw:
        raise SystemExit("Passwords don't match.")
    with AUTH_LOCK:
        u = _DB["users"].get(uname)
        if u and u.get("source") != "local" and not u.get("password"):
            raise SystemExit(f"{uname} signs in through {u.get('source')}; change the password there.")
        if not u:
            u = _DB["users"][uname] = {"source": "local", "name": uname, "email": "", "groups": [], "created": int(time.time())}
        u.update(password=hash_password(pw), admin=True, disabled=False)
        save_db()
    print(f"{uname} is now an admin with the new password. Restart RomSeek: docker restart romseek")


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "passwd":
        cli_passwd(sys.argv[2])
        raise SystemExit(0)
    os.makedirs(STATE_DIR, exist_ok=True)
    load_status()
    load_titles(); load_learned()
    load_auth()
    load_requests()
    os.makedirs(UPLOAD_DIR, mode=0o700, exist_ok=True)
    threading.Thread(target=importer_loop, daemon=True).start()
    threading.Thread(target=audit_loop, daemon=True).start()
    threading.Thread(target=lb_loop, daemon=True).start()
    threading.Thread(target=titles_loop, daemon=True).start()
    if LLDAP_URL and LLDAP_SYNC_USER:
        threading.Thread(target=lldap_loop, daemon=True).start()
    log(f"romseek on :{PORT}; library {LIBRARY}; category {CATEGORY}; "
        f"sign-in: local{' + LLDAP' if LLDAP_LOGIN else ''}{' + proxy headers' if PROXY_AUTH else ''}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
