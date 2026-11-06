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


def grab(rid, slug, kind="rom", learn=False):
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
            "hash": t["hash"], "name": t["name"], "progress": t.get("progress", 0), "size": t.get("size", 0),
            "added": t.get("added_on", 0),
            "eta": t.get("eta"), "dlspeed": t.get("dlspeed", 0), "seeds": t.get("num_seeds", 0),
            "peers": t.get("num_leechs", 0), "seeds_total": t.get("num_complete", 0),
            "peers_total": t.get("num_incomplete", 0), "qstate": t.get("state", ""),
            "platform": slug, "kind": kind,
            "state": state, "detail": st.get("detail", ""),
        })
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
# Admins (ADMIN_GROUPS, matched against the forward-auth Remote-Groups header)
# get a per-game menu: metadata lookup/apply, manual edits, unmatch, remove.
ADMIN_GROUPS = {g.strip() for g in os.environ.get("ADMIN_GROUPS", "admins").split(",") if g.strip()}


def is_admin(headers):
    groups = {g.strip() for g in (headers.get("Remote-Groups") or "").split(",")}
    return bool(groups & ADMIN_GROUPS)


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
            if u.path == "/api/firmware":
                slug = (qs.get("platform") or [""])[0]
                if slug not in SLUGS:
                    return self.send(400, {"error": "unknown platform"})
                return self.send(200, bios_entries(slug))
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
                return self.send(200, {"romm": ROMM_PUBLIC, "linked": bool(ROMM_TOKEN), "admin": is_admin(self.headers),
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
            if u.path in ("/api/game/meta", "/api/game/torrents"):
                if not is_admin(self.headers):
                    return self.send(403, {"error": "admins only"})
                g = lambda k: (qs.get(k) or [""])[0]
                library_entry(g("platform"), g("name"))
                if u.path == "/api/game/torrents":
                    return self.send(200, game_torrents(g("platform"), g("name")))
                return self.send(200, meta_search(g("platform"), g("name"), g("rom_id"), g("q")))
            if u.path == "/api/collections":
                return self.send(200, load_collections())
            if u.path == "/api/bios-files":
                slug = (qs.get("platform") or [""])[0]
                if slug not in SLUGS:
                    return self.send(400, {"error": "unknown platform"})
                return self.send(200, platform_firmware(slug))
            if u.path == "/healthz":
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
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
            body = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
            if self.path.startswith("/api/game/"):
                if not is_admin(self.headers):
                    return self.send(403, {"error": "admins only"})
                who = self.headers.get("Remote-User", "?")
                act = self.path.rsplit("/", 1)[-1]
                rid = body.get("rom_id")
                if act == "remove":
                    r = remove_game(str(body.get("platform", "")), str(body.get("name", "")), rid,
                                    bool(body.get("delete_torrent")))
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
                log("  by", who)
                return self.send(200, r)
            if self.path in ("/api/collections", "/api/collections/delete"):
                if not is_admin(self.headers):
                    return self.send(403, {"error": "admins only"})
                r = save_collection(body) if self.path == "/api/collections" else delete_collection(str(body.get("id", "")))
                log("  by", self.headers.get("Remote-User", "?"))
                return self.send(200, r)
            if self.path == "/api/cancel":
                if not is_admin(self.headers):
                    return self.send(403, {"error": "admins only"})
                r = cancel_download(str(body.get("hash", "")).lower())
                log("  by", self.headers.get("Remote-User", "?"))
                return self.send(200, r)
            if self.path == "/api/peek":
                return self.send(200, peek(str(body.get("id", ""))))
            if self.path == "/api/grab":
                grab(str(body.get("id", "")), str(body.get("platform", "")),
                     str(body.get("kind") or "rom"), learn=bool(body.get("learn")))
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
    load_titles(); load_learned()
    threading.Thread(target=importer_loop, daemon=True).start()
    threading.Thread(target=titles_loop, daemon=True).start()
    log(f"romseek on :{PORT}; library {LIBRARY}; category {CATEGORY}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
