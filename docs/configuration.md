# Configuration

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
| `SCAN_STATE` | none | Virus scan gate file. See [Virus scanning](virus-scanning.md). |
| `EXCLUDED_INDEXERS` | `LimeTorrents` | Prowlarr indexers to hide, comma-separated. |
| `ADMIN_GROUPS` | `admins` | Login groups that get the admin menu, comma-separated. |
| `POLL_SECONDS` | `60` | How often RomSeek checks for finished downloads. |
| `UPLOAD_MAX_MB` | `1024` | Biggest BIOS file you can upload. |
| `STATE_DIR` | `/state` | Where RomSeek keeps its own data. |
| `PORT` | `8000` | Port inside the container. |
| `TITLE_DB_URL` | libretro-database on GitHub | Source of the game title list. |

---

[← Back to README](../README.md)
