<img src="logo.svg" width="96" alt="RomSeek logo">

# RomSeek

A small web app that adds a "Sonarr/Radarr for games" workflow on top of [RomM](https://github.com/rommapp/romm):

- **Download games:** search your [Prowlarr](https://prowlarr.com) indexers (Console category), pick the platform, and grab. Torrents go to qBittorrent.
- **Platforms & BIOS:** set up RomM platforms, and search for/grab BIOS firmware for the ones that need it.
- **Library:** see what's downloading and what's in RomM, per platform. Browser-playable games link straight to RomM's player; download-only platforms (PS2, GameCube, …) link to the game's RomM page and the official emulator download.

Finished downloads are hardlinked into RomM's library (`roms/<platform>/`, firmware in `bios/<platform>/`), and RomM's filesystem watcher picks them up.

It's a single Python file using only the standard library, served from a stock `python:3.12-alpine` image. No build step.

## Requirements

- **RomM 5.3+** using the standard library layout (structure A):
  ```yaml
  # RomM config.yml
  filesystem:
    structure:
      default: "roms/{platform}/{game}"
      firmware: "bios/{platform}"
  ```
  and these RomM environment variables, so new files are picked up automatically:
  ```
  ENABLE_RESCAN_ON_FILESYSTEM_CHANGE=true
  RESCAN_ON_FILESYSTEM_CHANGE_DELAY=2
  ```
- **Prowlarr** with at least one indexer that returns Console results.
- **qBittorrent** (WebUI API v2). romseek talks to it without logging in, so add romseek's docker network to qBittorrent's *Bypass authentication for clients in whitelisted IP subnets* (Options → WebUI).
- An **authenticating reverse proxy** in front of it (Authelia/Authentik forward-auth, etc.). romseek has no login of its own yet. **Never expose its port directly.**

## Paths (important)

romseek must see downloads **at the same path qBittorrent reports them**, and downloads and the RomM library must be on the **same filesystem** so hardlinks work. The simplest setup mounts one parent folder at the same place in every container. The example uses `/data`:

| Host | qBittorrent | romseek | RomM |
|---|---|---|---|
| `/srv` | `/data` | `/data` | — |
| `/srv/media/roms` | — | `/data/media/roms` | `/romm/library` |

If your layout differs, set `QBIT_SAVE_PATH` and `ROMM_LIBRARY` accordingly.

## Setup

1. Copy `docker-compose.example.yml` into your compose project (or merge the service) and `.env.example` to `romseek.env`, then fill it in.
2. Make sure romseek's user (default `1000:1000`) can write to `<library>/roms` and `<library>/bios`.
3. `docker compose up -d romseek`, then put it behind your proxy, e.g. Caddy:
   ```
   roms.example.com {
       import authelia      # your forward-auth snippet
       reverse_proxy romseek:8000
   }
   ```
4. Optional, for direct game links in the Library tab: in RomM go to *Client API Tokens* and create a token with **View ROMs** and **View platforms**, then put it in `ROMM_API_TOKEN`. Add **Edit ROMs** as well if admins should be able to fix metadata and remove games (see below).

romseek creates a qBittorrent category `roms` on first start.

## Safety

- Only files that pass these checks are imported:
  - nothing starting with a Windows executable header (`MZ`) is imported;
  - junk (`.nfo`, `.txt`, `.url`, images, …) is skipped.
- **Optional virus-scan gate:** set `SCAN_STATE` to a file whose modification time is the cutoff your scanner has covered (e.g. a scanner that runs `touch -d @<start time> last-run` after each pass). Downloads wait until the scanner has covered them. Leave it empty to import right away.
- BIOS zip packs are unpacked by file name only, so paths inside the zip can't escape the firmware folder. Neo Geo and arcade BIOS stay zipped, because RomM expects the zip.
- It's a good idea to also block executables in qBittorrent itself: Options → Downloads → *Excluded file names* (`*.exe`, `*.scr`, `*.bat`, `*.lnk`, …).

## Configuration

See `.env.example`. Everything except `PROWLARR_KEY` has a default.

## Platform detection

Each search result's system is worked out, in order, from:

1. **The release name**: console names, file extensions and known release groups (e.g. `[Wii]`, `.nsp`, `NGC`, `MoonCube`).
2. **A game-title database** built from the No-Intro / Redump lists in [libretro-database](https://github.com/libretro/libretro-database). It's downloaded on first start into `state/titles.json` (~2.5 MB, ~56k titles) and refreshed every 30 days. A game only released on one system is placed automatically. Otherwise its systems are listed first in the picker. Switch, Neo Geo and arcade aren't in those lists.
3. **Other results in the same search** for the same game: their majority system becomes the suggestion.
4. **Your earlier choices**: picking a system for an unknown result is remembered for that title (`state/learned.json`).

**Check files** asks the swarm for a torrent's file list (`stopCondition=MetadataReceived`, deleted right after, nothing downloaded) and reads the extensions. It needs peers, so it won't work on dead torrents. A **size?** badge flags results too big for one game on the detected system.

## Managing games (admins)

Users in `ADMIN_GROUPS` (default `admins`, read from the forward-auth `Remote-Groups` header) get a ⋮ menu on every game tile:

- **Metadata lookup:** searches every metadata provider enabled in RomM (LaunchBox, IGDB, ScreenScraper, MobyGames, …) and applies the match you pick. RomM then fetches the full details and cover itself. If a name finds nothing, RomSeek retries with the closest real titles from its title database and shows them as suggestions.
- **Disc and product IDs:** files named like `RZDP01.wbfs` (Wii/GameCube) or `BLUS30443` (PS3) are translated with [GameTDB](https://www.gametdb.com) title lists, which also supply a cover.
- **Edit details:** set the title, year, genres, cover URL and description by hand.
- **Clear metadata** and **Remove game**. Remove deletes the library file and the RomM entry, and can also delete the torrent and its download.

These need the **Edit ROMs** scope on the RomM token. LaunchBox search needs RomM's local LaunchBox database, so set `ENABLE_SCHEDULED_UPDATE_LAUNCHBOX_METADATA=true` in RomM and run its *Update LaunchBox metadata* task once. ScreenScraper (`SCREENSCRAPER_USER`/`_PASSWORD`) and IGDB (`IGDB_CLIENT_ID`/`_SECRET`) accounts are free and add many more matches.

## Homepage widget

`GET /api/stats` returns `{"pending": n, "roms": n, "platforms": n}` for dashboard tiles. For [Homepage](https://gethomepage.dev):

```yaml
- RomSeek:
    icon: https://raw.githubusercontent.com/Evident-Dev/romseek/main/logo.svg
    href: https://roms.example.com
    widget:
      type: customapi
      url: http://romseek:8000/api/stats
      refreshInterval: 30000
      mappings:
        - { field: pending, label: Pending, format: number }
        - { field: roms, label: ROMs, format: number }
        - { field: platforms, label: Platforms, format: number }
```

## Notes

- Some indexers miscategorise PC releases as Console. Results that can't be matched to a platform sort last, and `EXCLUDED_INDEXERS` hides indexers entirely.
- Only download games you have the right to. Dumping your own cartridges and discs is the clean route.
