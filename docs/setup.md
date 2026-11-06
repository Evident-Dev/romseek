# Setup guide

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

### Step 9. Test it

1. Open RomSeek and go to **Download games**.
2. Search for a game you own.
3. Check the system it picked. Change it if it's wrong.
4. Click **Grab**.
5. Wait for the download to finish. RomSeek checks every 60 seconds.
6. Open RomM. The game shows up under its system.

Done. Next, pick a [virus scan option](virus-scanning.md).

---

[← Back to README](../README.md)
