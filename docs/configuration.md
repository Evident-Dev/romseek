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
| `ADMIN_GROUPS` | `admins` | LLDAP or SSO groups whose members are always admins, comma-separated. |
| `POLL_SECONDS` | `60` | How often RomSeek checks for finished downloads. |
| `UPLOAD_MAX_MB` | `1024` | Biggest BIOS file you can upload. |
| `STATE_DIR` | `/state` | Where RomSeek keeps its own data. |
| `PORT` | `8000` | Port inside the container. |
| `TITLE_DB_URL` | libretro-database on GitHub | Source of the game title list. |

## Sign-in

See [Step 7 of the setup guide](setup.md#step-7-choose-how-people-sign-in) for how these fit together.

| Setting | Default | What it does |
|---|---|---|
| `PROXY_AUTH` | `false` | Trust the user and groups headers from a forward-auth proxy (Authelia, Authentik). Only turn on when the proxy is the only way to reach RomSeek. |
| `PROXY_USER_HEADER` | `Remote-User` | Header with the username. |
| `PROXY_GROUPS_HEADER` | `Remote-Groups` | Header with the groups, separated by `,` or `\|`. |
| `PROXY_NAME_HEADER` | `Remote-Name` | Header with the display name. |
| `PROXY_EMAIL_HEADER` | `Remote-Email` | Header with the email address. |
| `LLDAP_URL` | none | LLDAP's web address, like `http://lldap:17170`. Lets people sign in with their LLDAP password. |
| `LLDAP_LOGIN` | `true` | Set `false` to use `LLDAP_URL` only for syncing. |
| `LLDAP_SYNC_USER` | none | An LLDAP user in `lldap_strict_readonly`. Lists every LLDAP user ahead of their first sign-in, and makes LLDAP the owner of groups. |
| `LLDAP_SYNC_PASSWORD` | none | That user's password. |
| `LLDAP_SYNC_MINUTES` | `15` | How often to sync. |
| `LLDAP_SERVICE_GROUPS` | `lldap_strict_readonly,lldap_password_manager` | Members of these LLDAP groups are service accounts and aren't added as users. |
| `SESSION_DAYS` | `30` | How long a sign-in lasts. |

Users, groups, profiles and sessions are stored in `STATE_DIR` (`users.json`, `sessions.json`). Passwords are salted PBKDF2 hashes.

## Requests and audit log

| Setting | Default | What it does |
|---|---|---|
| `AUDIT_DAYS` | `90` | How long audit log entries are kept (`STATE_DIR/audit.log`). |
| `LAUNCHBOX_METADATA_URL` | LaunchBox's `Metadata.zip` | Games database used to check ratings before a download. |
| `LAUNCHBOX_REFRESH_DAYS` | `7` | How often to download it again. |

Requests are stored in `STATE_DIR/requests.json`; firmware uploads waiting for approval in `STATE_DIR/uploads/`.

---

[← Back to README](../README.md)
