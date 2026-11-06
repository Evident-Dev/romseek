# Security

## Reporting a problem

Do not open a public issue for a security problem. Use GitHub's private reporting instead:

1. Go to the **Security** tab of this repo.
2. Click **Report a vulnerability**.

Include what you found, how to reproduce it, and the version (commit) you tested.

## How RomSeek is meant to run

- RomSeek has no login of its own. It must run behind a reverse proxy that requires a login, like Authelia or Authentik.
- Never publish its port. Your proxy reaches it on the Docker network.
- Admin actions trust the `Remote-Groups` header from your proxy. Make sure your proxy removes that header from incoming requests, so users can't set it themselves.
- qBittorrent trusts RomSeek by network address. Only whitelist your Docker network, never your whole LAN.
