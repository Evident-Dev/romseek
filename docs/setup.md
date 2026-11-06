# Setup guide

## Before you start

> ⚖️ Only use RomSeek for games, BIOS and firmware you have the legal right to download. See [Legal](legal.md).

You need these already running in Docker:

- [ ] **RomM** 5.3 or newer
- [ ] **Prowlarr** with at least one indexer that returns Console results
- [ ] **qBittorrent** (any version with the WebUI)
- [ ] **A reverse proxy with HTTPS**, like Caddy or Traefik. Optional: Authelia, Authentik or LLDAP for single sign-on.

> ⚠️ Never open RomSeek's port straight to the internet. Put it behind your reverse proxy.

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

### Step 7. Choose how people sign in

RomSeek has its own sign-in, users and groups. Pick one way in (you can combine them):

| Option | Best for | Set in `romseek.env` |
|---|---|---|
| **A. RomSeek accounts** | No SSO. You make an account for each person. | nothing |
| **B. LLDAP** | You run [LLDAP](https://github.com/lldap/lldap) but no SSO proxy. People use their LLDAP password. | `LLDAP_URL` |
| **C. SSO proxy** | Authelia, Authentik or similar forward auth in front of RomSeek. People are added on their first visit. | `PROXY_AUTH=true` |

All settings live in `romseek.env` (see [Configuration](configuration.md)). Users and groups are managed in the **Users** tab.

#### First admin (options A and B)

On a fresh install RomSeek asks you to create the admin account. To prove you
own the server it needs a one-time **setup code** from the log:

```bash
docker logs romseek 2>&1 | grep -A1 "setup code"
```

It's also in `state/setup-code.txt`. Open RomSeek, enter the code, pick a
username and password, and you're signed in as admin. The code changes on every
restart until setup is done. With option C there's no setup step: members of
`ADMIN_GROUPS` are admins.

> ⚠️ Even with its own sign-in, RomSeek is safest behind a reverse proxy with
> HTTPS. Don't publish its port straight to the internet.

#### Option A. RomSeek accounts

Nothing to set. After the first admin, open **Users → + New user** to add
people with a username and password. Anyone can change their own password from
the menu under their name.

#### Option B. LLDAP

```ini
LLDAP_URL=http://lldap:17170
```

The sign-in form now also accepts LLDAP usernames and passwords. The person's
LLDAP groups come with them, so someone in `admins` (or any group in
`ADMIN_GROUPS`) is an admin. If no LLDAP admin exists yet, make a RomSeek admin
with the setup code first.

People appear in **Users** once they first sign in. To list everyone ahead of
time, make an LLDAP user in the `lldap_strict_readonly` group and add:

```ini
LLDAP_SYNC_USER=romseek
LLDAP_SYNC_PASSWORD=...
```

RomSeek then syncs every 15 minutes (`LLDAP_SYNC_MINUTES`), and removes people
you delete from LLDAP.

#### Option C. SSO proxy (forward auth)

```ini
PROXY_AUTH=true
```

How it works:

1. Someone opens `https://roms.example.com`.
2. Your proxy asks your login service (Authelia here) whether they are signed in.
   This is called **forward auth**. If they aren't signed in, they get the login page.
3. Once they are signed in, Authelia answers with headers that describe the user:

   | Header | Example |
   |---|---|
   | `Remote-User` | `alex` |
   | `Remote-Name` | `Alex Smith` |
   | `Remote-Email` | `alex@example.com` |
   | `Remote-Groups` | `admins,family` |

4. The proxy copies those headers onto the request it passes to RomSeek.
5. RomSeek adds the user on their first visit and keeps their groups in step
   with the proxy. Members of a group in `ADMIN_GROUPS` (default `admins`) are admins.

> ⚠️ With `PROXY_AUTH=true`, RomSeek believes whatever `Remote-User` header it
> gets. Only turn it on when the proxy is the **only** way to reach RomSeek
> (no published port) and the proxy runs forward auth on every request. Anyone
> who can reach the port directly could sign in as anyone.

##### Caddy + Authelia

```
(authelia) {
    forward_auth authelia:9091 {
        uri /api/authz/forward-auth
        copy_headers Remote-User Remote-Groups Remote-Name Remote-Email
    }
}

roms.example.com {
    import authelia
    reverse_proxy romseek:8000
}
```

> ⚠️ The `copy_headers` line matters. Without it you still get Authelia's login
> page, but RomSeek never learns who you are and shows its own sign-in instead.

##### Traefik + Authelia

Labels on the `romseek` service:

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.romseek.rule=Host(`roms.example.com`)
  - traefik.http.routers.romseek.middlewares=authelia@docker
  - traefik.http.services.romseek.loadbalancer.server.port=8000
  # Define the middleware once (often on the authelia container):
  - traefik.http.middlewares.authelia.forwardauth.address=http://authelia:9091/api/authz/forward-auth
  - traefik.http.middlewares.authelia.forwardauth.trustForwardHeader=true
  - traefik.http.middlewares.authelia.forwardauth.authResponseHeaders=Remote-User,Remote-Groups,Remote-Name,Remote-Email
```

##### Authentik

Use Authentik's proxy outpost, and tell RomSeek Authentik's header names
(groups separated by `|` work too):

```ini
PROXY_AUTH=true
PROXY_USER_HEADER=X-authentik-username
PROXY_GROUPS_HEADER=X-authentik-groups
PROXY_NAME_HEADER=X-authentik-name
PROXY_EMAIL_HEADER=X-authentik-email
```

#### Check it

Open `https://roms.example.com`. You should land in RomSeek signed in (option
C) or see its sign-in page (A and B). The menu under your name, top right,
shows your username, groups and whether you're an admin. Admins also get a
**Users** tab.

If an SSO admin shows up without admin rights, the groups header isn't reaching
RomSeek: check the `copy_headers` / `authResponseHeaders` line and the user's
groups in your login service.

#### Groups and permissions

Open **Users**. Each group card sets:

- **Download new games:** search and grab games. Off hides the **Download games** tab.
- **Add platforms and download/upload BIOS:** set up new systems and manage BIOS files.
- **Platforms they see:** which systems show in the Library and Platforms tabs.
- **Game ratings they see:** E, E10+, T, M, AO, Unrated. Ratings come from
  RomM's metadata (IGDB); other boards like PEGI are matched to the nearest
  tier. Games RomM hasn't matched yet count as **Unrated**.
- **Admin:** everything, plus managing users, metadata, collections and downloads.

How they combine:

- Someone in several groups gets everything any of them allows.
- People in no set-up group get **Default**. On a fresh install, Default sees
  everything and can download games but can't add platforms or BIOS.
- Groups from LLDAP or your SSO appear automatically. Until you save settings
  for one, its members get Default. You can also make RomSeek-only groups and
  tick them on any user.

Hiding a platform or rating only hides those games in RomSeek. It doesn't stop
anyone downloading: if a Kids account grabs an M-rated game, it downloads, and
it disappears from their library once RomM matches it. RomSeek doesn't control
what RomM itself shows; set that up in RomM.

#### Locked out?

```bash
docker exec -it romseek python3 /app/romseek.py passwd alex
docker restart romseek
```

This sets a new password for the RomSeek account `alex` (creating it if needed)
and makes it an admin.

### Step 8. Link RomM (recommended)

This turns on the Library page, game pages and the admin menu.

1. In RomM, go to **Client API Tokens**.
2. Create a token with these permissions:
   - **View ROMs** and **View platforms** (needed for the Library)
   - **Edit ROMs** (only if admins should fix metadata or remove games)

   > 💡 RomM only offers **Edit ROMs** to accounts with the **Admin** role. If the box isn't there, have a RomM admin set your account to Admin (**Settings → Users**), sign out and back in, then make the token again. You can't add permissions to an existing token.

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
