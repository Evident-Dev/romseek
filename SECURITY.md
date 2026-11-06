# Security

## Reporting a problem

Do not open a public issue for a security problem. Use GitHub's private reporting instead:

1. Go to the **Security** tab of this repo.
2. Click **Report a vulnerability**.

Include what you found, how to reproduce it, and the version (commit) you tested.

## How RomSeek is meant to run

- RomSeek has its own sign-in (local accounts, LLDAP, or a trusted proxy). Run it behind a reverse proxy with HTTPS so passwords and session cookies are encrypted.
- Never publish its port. Your proxy reaches it on the Docker network.
- With `PROXY_AUTH=true`, RomSeek trusts the `Remote-User` and `Remote-Groups` headers. Only turn it on when the proxy runs forward auth on every request and is the only way to reach RomSeek; otherwise anyone could set those headers themselves.
- The first-run setup needs a one-time code from the server log, so a stranger who finds a fresh install can't claim the admin account.
- `/api/stats` and `/healthz` answer without signing in. They return counts only.
- qBittorrent trusts RomSeek by network address. Only whitelist your Docker network, never your whole LAN.
