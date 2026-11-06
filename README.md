<img src="logo.svg" width="96" alt="RomSeek logo">

# RomSeek

RomSeek is "Sonarr/Radarr for games" on top of [RomM](https://github.com/rommapp/romm).

- **Find games.** Search your [Prowlarr](https://prowlarr.com) indexers, pick the system, and click Grab. qBittorrent downloads it.
- **Auto-import.** When the download finishes, RomSeek puts the game in your RomM library. RomM shows it a few seconds later.
- **BIOS help.** RomSeek shows which systems need BIOS files and finds them for you.
- **A nicer library.** Browse by system, genre or collection. Each game has a page with three steps: get the game, get the emulator, get the BIOS.

<!-- SCREENSHOT: Library page (docs/screenshots/library.png) -->

RomSeek is one Python file with no dependencies. It runs on the stock `python:3.12-alpine` image. There is nothing to build.

---

## Contents

1. [Before you start](#before-you-start)
2. [Setup (about 20 minutes)](#setup-about-20-minutes)
3. [Virus scanning (pick one)](#virus-scanning-pick-one)
4. [Optional extras](#optional-extras)
5. [All settings](#all-settings)
6. [Troubleshooting](#troubleshooting)
7. [How it works](#how-it-works)

---

## Before you start

You need these already running in Docker:

- [ ] **RomM** 5.3 or newer
- [ ] **Prowlarr** with at least one indexer that returns Console results
- [ ] **qBittorrent** (any version with the WebUI)
- [ ] **A reverse proxy with a login**, like Caddy or Traefik with Authelia or Authentik

> ⚠️ RomSeek has no login of its own. Never open its port to the internet. Always put it behind your proxy's login.

All containers must be on the same Docker network, so they can reach each other by name.

---

## Setup (about 20 minutes)

The examples use these names. Change them if yours are different.

| Thing | Example |
|---|---|
| Host folder that holds downloads and the RomM library | `/srv` |
| Downloads folder | `/srv/downloads` |
| RomM library folder | `/srv/media/roms` (contains `roms/` and `bios/`) |
| Your compose project folder | `/opt/stack` |

### Step 1. Get the files

In your compose project folder, run:

```bash
cd /opt/stack
git clone https://github.com/Evident-Dev/romseek.git romseek
mkdir -p romseek/state
cp romseek/.env.example romseek/romseek.env
```

To update later, run `git -C /opt/stack/romseek pull`, then restart the container.

### Step 2. Set up RomM

RomSeek puts games in `roms/<system>/` and BIOS files in `bios/<system>/`. RomM must use the same layout.

1. Open RomM's `config.yml`. Make sure it has this:

   ```yaml
   filesystem:
     structure:
       default: "roms/{platform}/{game}"
       firmware: "bios/{platform}"
   ```

2. Add these two lines to RomM's environment. They make RomM find new games on its own.

   ```
   ENABLE_RESCAN_ON_FILESYSTEM_CHANGE=true
   RESCAN_ON_FILESYSTEM_CHANGE_DELAY=2
   ```

3. Restart RomM.

### Step 3. Copy your Prowlarr API key

1. In Prowlarr, go to **Settings → General**.
2. Copy the **API Key**.
3. Paste it into `romseek/romseek.env` after `PROWLARR_KEY=`.

### Step 4. Let RomSeek talk to qBittorrent

RomSeek does not log in to qBittorrent. You tell qBittorrent to trust your Docker network instead.

1. Find your Docker network's address range. Replace `stack_default` with your network name:

   ```bash
   docker network inspect stack_default -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
   ```

   You get something like `172.18.0.0/16`.

2. In qBittorrent, go to **Options → WebUI**.
3. Turn on **Bypass authentication for clients in whitelisted IP subnets**.
4. Add the address range from step 1. Click **Save**.

<!-- SCREENSHOT: qBittorrent WebUI whitelist setting (docs/screenshots/qbit-whitelist.png) -->

> 💡 If qBittorrent runs inside a VPN container (like gluetun), RomSeek connects through that container. Set `QBIT_URL=http://gluetun:8080` in `romseek.env`.

### Step 5. Check your folders

This is the step people get wrong. Read it slowly.

**Rule 1:** RomSeek and qBittorrent must see the downloads folder at the **same path**.
**Rule 2:** Downloads and the RomM library should be on the **same drive**. Then RomSeek uses hardlinks (no extra space used). On different drives it copies the files instead.

The easy way: mount the same host folder at `/data` in both containers.

| Host folder | In qBittorrent | In RomSeek | In RomM |
|---|---|---|---|
| `/srv` | `/data` | `/data` | not mounted |
| `/srv/media/roms` | not mounted | `/data/media/roms` | `/romm/library` |

If you use these paths, the defaults in `romseek.env` already work. If your paths are different, change `QBIT_SAVE_PATH` and `ROMM_LIBRARY` in `romseek.env`.

**Check who owns the library.** RomSeek must be able to write to it.

```bash
stat -c '%u:%g' /srv/media/roms/roms
```

You get something like `1000:1000`. Use that value for `user:` in the next step. Then give RomSeek its state folder:

```bash
sudo chown 1000:1000 /opt/stack/romseek/state
```

### Step 6. Add RomSeek to your compose file

Copy the `romseek` service from `docker-compose.example.yml` into your `docker-compose.yml`:

```yaml
services:
  romseek:
    image: python:3.12-alpine
    container_name: romseek
    restart: unless-stopped
    user: "1000:1000"            # the value from Step 5
    command: ["python3", "-u", "/app/romseek.py"]
    env_file: ./romseek/romseek.env
    volumes:
      - ./romseek/romseek.py:/app/romseek.py:ro
      - ./romseek/index.html:/app/index.html:ro
      - ./romseek/state:/state
      - /srv:/data                # same folder and path as qBittorrent
    # No "ports:" on purpose. Your proxy reaches it on the Docker network.
```

Start it:

```bash
docker compose up -d romseek
docker logs -f romseek
```

The log should show no errors. On first start RomSeek creates a `roms` category in qBittorrent and downloads its game title list (about 2.5 MB).

### Step 7. Put it behind your proxy

Example for Caddy with Authelia:

```
roms.example.com {
    import authelia          # your login snippet
    reverse_proxy romseek:8000
}
```

Reload your proxy. Open `https://roms.example.com`. You should see the login page first, then RomSeek.

### Step 8. Link RomM (recommended)

This turns on the Library page, game pages and the admin menu.

1. In RomM, go to **Client API Tokens**.
2. Create a token with these permissions:
   - **View ROMs** and **View platforms** (needed for the Library)
   - **Edit ROMs** (only if admins should fix metadata or remove games)
3. Paste the token into `romseek.env` after `ROMM_API_TOKEN=`.
4. Set `ROMM_PUBLIC_URL` to the address your users open for RomM, like `https://romm.example.com`.
5. Restart: `docker compose up -d romseek`.

<!-- SCREENSHOT: RomM Client API Token permissions (docs/screenshots/romm-token.png) -->

### Step 9. Test it

1. Open RomSeek and go to **Download games**.
2. Search for a game you own.
3. Check the system it picked. Change it if it's wrong.
4. Click **Grab**.
5. Wait for the download to finish. RomSeek checks every 60 seconds.
6. Open RomM. The game shows up under its system.

<!-- SCREENSHOT: Search results with platform picker (docs/screenshots/search.png) -->

Done. Pick a virus scan option next.

---

## Virus scanning (pick one)

RomSeek always does these checks, with no setup:

- It never imports a file that starts like a Windows program (`MZ` header).
- It skips junk files (`.nfo`, `.txt`, `.url`, images and so on).
- It unpacks BIOS zips by file name only, so a bad zip can't write outside the BIOS folder.

You can add more on top. Pick one:

### Option A. Built-in checks only (easiest)

Do nothing. Leave `SCAN_STATE` empty. Games import as soon as they finish.

Also block programs in qBittorrent. It takes 1 minute:

1. Go to **Options → Downloads → Excluded file names**.
2. Paste the list from [qbittorrent-excluded-file-names.txt](https://github.com/Evident-Dev/arr-download-malware-guard/blob/main/qbittorrent-excluded-file-names.txt). Click **Save**.

### Option B. ClamAV with arr-download-malware-guard (recommended)

[arr-download-malware-guard](https://github.com/Evident-Dev/arr-download-malware-guard) scans your downloads folder with ClamAV on a timer, deletes anything bad and alerts you. RomSeek waits for each scan before it imports.

1. Set up arr-download-malware-guard with its own guide. If you only run RomSeek, leave `ARR_APPS` empty.
2. Make sure its downloads folder includes RomSeek's downloads (`/srv/downloads/roms`).
3. In your compose file, add this line under RomSeek's `volumes:`:

   ```yaml
      - /var/lib/clamav-scan:/scan:ro
   ```

4. In `romseek.env`, set:

   ```
   SCAN_STATE=/scan/last-run
   ```

5. Restart: `docker compose up -d romseek`.

Now finished downloads show **Waiting for virus scan** until the next scan covers them. If ClamAV deletes a file, RomSeek marks the download as blocked.

### Option C. Your own scanner

Any scanner works if it updates a file's timestamp after each scan. Set the timestamp to the time the scan **started**:

```bash
touch -d "@$SCAN_START" /var/lib/my-scanner/last-run
```

Mount that folder into RomSeek and point `SCAN_STATE` at the file. RomSeek imports only files older than that timestamp.

---

## Optional extras

### Admin menu

Users in the `admins` group get a ⋮ menu on each game. The group comes from your login proxy's `Remote-Groups` header. Change it with `ADMIN_GROUPS`.

- **Metadata lookup:** search every metadata source RomM has turned on, and pick the right match.
- **Edit details:** set the title, year, genres, cover and description by hand.
- **Clear metadata** and **Remove game.** Remove deletes the file and the RomM entry. It can also delete the torrent.

This needs the **Edit ROMs** permission on the RomM token.

For more metadata matches:

- **LaunchBox:** set `ENABLE_SCHEDULED_UPDATE_LAUNCHBOX_METADATA=true` in RomM. Run its **Update LaunchBox metadata** task once.
- **ScreenScraper** and **IGDB:** make free accounts. Add them to RomM (`SCREENSCRAPER_USER`, `SCREENSCRAPER_PASSWORD`, `IGDB_CLIENT_ID`, `IGDB_CLIENT_SECRET`).

### Collections

Open **Library → Collections**.

- **Smart collections** fill themselves: recently added, playable in the browser, retro, 2000s, modern, big downloads, and *Needs metadata* (admins only).
- **Genres:** one for each genre RomM knows.
- **Your collections:** admins make these from rules, like "every game with *zelda* in the title". They update as new games arrive.

### Homepage dashboard widget

`/api/stats` returns pending downloads, game count and system count. For [Homepage](https://gethomepage.dev):

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

---

## All settings

Put these in `romseek.env`. Only `PROWLARR_KEY` is required.

| Setting | Default | What it does |
|---|---|---|
| `PROWLARR_KEY` | none | Prowlarr API key. **Required.** |
| `PROWLARR_URL` | `http://prowlarr:9696` | Prowlarr address. |
| `QBIT_URL` | `http://gluetun:8080` | qBittorrent WebUI address. `.env.example` sets it to `http://qbittorrent:8080`. |
| `QBIT_CATEGORY` | `roms` | qBittorrent category RomSeek uses. |
| `QBIT_SAVE_PATH` | `/data/downloads/roms` | Where qBittorrent saves RomSeek downloads. Same path in both containers. |
| `ROMM_LIBRARY` | `/data/media/roms/roms` | RomM's `roms` folder, as RomSeek sees it. |
| `ROMM_FIRMWARE` | `bios` folder next to `ROMM_LIBRARY` | RomM's BIOS folder. |
| `ROMM_URL` | `http://romm:8080` | RomM address inside Docker. |
| `ROMM_PUBLIC_URL` | none | RomM address your users open. Used for game links. |
| `ROMM_API_TOKEN` | none | RomM client token. Turns on the Library and admin menu. |
| `SCAN_STATE` | none | Virus scan gate file. See [Virus scanning](#virus-scanning-pick-one). |
| `EXCLUDED_INDEXERS` | `LimeTorrents` | Prowlarr indexers to hide, comma-separated. |
| `ADMIN_GROUPS` | `admins` | Login groups that get the admin menu, comma-separated. |
| `POLL_SECONDS` | `60` | How often RomSeek checks for finished downloads. |
| `UPLOAD_MAX_MB` | `1024` | Biggest BIOS file you can upload. |
| `STATE_DIR` | `/state` | Where RomSeek keeps its own data. |
| `PORT` | `8000` | Port inside the container. |
| `TITLE_DB_URL` | libretro-database on GitHub | Source of the game title list. |

---

## Troubleshooting

**Search shows nothing.**
Check `PROWLARR_KEY`. Check that at least one Prowlarr indexer supports the Console category.

**Grab fails, or the log says qBittorrent refused the connection.**
Redo [Step 4](#step-4-let-romseek-talk-to-qbittorrent). Check that `QBIT_URL` uses the right container name. If qBittorrent is behind gluetun, use the gluetun name.

**The download finishes but never imports.**
RomSeek can't find the files at the path qBittorrent reports. Redo [Step 5](#step-5-check-your-folders). Run this and compare the paths:

```bash
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' qbittorrent romseek
```

**It says "Waiting for virus scan" forever.**
Your scanner is not updating the `SCAN_STATE` file. Check that the file exists inside the container: `docker exec romseek ls -l /scan/last-run`.

**It says "blocked".**
The download had a Windows program in it, or the virus scanner deleted it. Pick a different release.

**The game imported, but RomM doesn't show it.**
Check the two RomM environment lines in [Step 2](#step-2-set-up-romm). You can also run a scan in RomM by hand.

**Imports use double disk space.**
Downloads and the library are on different drives, so RomSeek copies instead of hardlinking. Move them onto the same drive.

**Permission denied in the log.**
The `user:` in your compose file can't write to the library or `state` folder. Redo the ownership check in [Step 5](#step-5-check-your-folders).

**A result has the wrong system.**
Pick the right one before you grab. RomSeek remembers your choice for that title next time.

**PC games show up in results.**
Some indexers mark PC games as Console. Results with no known system sort last. Use `EXCLUDED_INDEXERS` to hide a bad indexer.

---

## How it works

1. **Search.** RomSeek asks Prowlarr for Console results.
2. **Pick the system.** It guesses the system for each result from:
   1. the release name (console names, file types, known release groups);
   2. a list of about 56,000 game titles from the No-Intro and Redump sets in [libretro-database](https://github.com/libretro/libretro-database), refreshed every 30 days (Switch, Neo Geo and arcade are not in it);
   3. other results for the same game in the same search;
   4. your earlier choices for that title.
3. **Grab.** It sends the torrent to qBittorrent with the `roms` category and a tag for the system.
4. **Check files (optional).** The **Check files** button reads a torrent's file list from peers without downloading it. It needs live peers. A **size?** badge means the result is too big for one game on that system.
5. **Import.** Every 60 seconds it looks at finished downloads. It runs the safety checks, waits for the virus scan if you set one, then links the files into `roms/<system>/` or `bios/<system>/`. RomM picks them up from there.

RomSeek keeps its own data in the `state` folder: download status, learned systems, collections and the title list.

---

Only download games you have the right to. Dumping your own cartridges and discs is the clean way to do it.
