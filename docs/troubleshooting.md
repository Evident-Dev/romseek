# Troubleshooting

**Search shows nothing.**
Check `PROWLARR_KEY`. Check that at least one Prowlarr indexer supports the Console category.

**Grab fails, or the log says qBittorrent refused the connection.**
Redo [Step 4](setup.md#step-4-let-romseek-talk-to-qbittorrent). Check that `QBIT_URL` uses the right container name. If qBittorrent is behind gluetun, use the gluetun name.

**The download finishes but never imports.**
RomSeek can't find the files at the path qBittorrent reports. Redo [Step 5](setup.md#step-5-check-your-folders). Run this and compare the paths:

```bash
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' qbittorrent romseek
```

**It says "Waiting for virus scan" forever.**
Your scanner is not updating the `SCAN_STATE` file. Check that the file exists inside the container: `docker exec romseek ls -l /scan/last-run`.

**It says "blocked".**
The download had a Windows program in it, or the virus scanner deleted it. Pick a different release.

**The game imported, but RomM doesn't show it.**
Check the two RomM environment lines in [Step 2](setup.md#step-2-set-up-romm). You can also run a scan in RomM by hand.

**Imports use double disk space.**
Downloads and the library are on different drives, so RomSeek copies instead of hardlinking. Move them onto the same drive.

**Permission denied in the log.**
The `user:` in your compose file can't write to the library or `state` folder. Redo the ownership check in [Step 5](setup.md#step-5-check-your-folders).

**The Edit ROMs box is missing when making the RomM token.**
RomM only offers it to Admin accounts. See the note in [Step 8](setup.md#step-8-link-romm-recommended).

**Metadata lookup finds nothing.**
RomM has no metadata source turned on that has data. LaunchBox needs its local database: set `ENABLE_SCHEDULED_UPDATE_LAUNCHBOX_METADATA=true` in RomM and run the **Update LaunchBox metadata** task once. Then try the full official title, or one of the **Try:** suggestions.

**Admin actions say "RomM refused".**
The RomM token is missing **Edit ROMs**. Make a new token with it, put it in `romseek.env`, and run `docker compose up -d romseek`.

**A save made on one device isn't there on another.**
Saves are stored in RomM, not RomSeek. RomM loads the newest save when the play page opens, so a RomM tab left open from before the save won't know about it. Reload the page (or use RomSeek's ▶ Play, which always opens a fresh one) before you press Play.

**A result has the wrong system.**
Pick the right one before you grab. RomSeek remembers your choice for that title next time.

**PC games show up in results.**
Some indexers mark PC games as Console. Results with no known system sort last. Use `EXCLUDED_INDEXERS` to hide a bad indexer.

**RomSeek shows its own sign-in page even though I signed in through Authelia.**
Either `PROXY_AUTH=true` isn't set in `romseek.env`, or the proxy isn't passing the user header. Check the `copy_headers` / `authResponseHeaders` line in [Step 7](setup.md#option-c-sso-proxy-forward-auth).

**I upgraded and now RomSeek asks for a setup code.**
RomSeek used to rely on the proxy alone. If you run it behind Authelia or Authentik, add `PROXY_AUTH=true` to `romseek.env` and restart. Otherwise, create the admin with the code from `docker logs romseek`.

**Someone can't see a game that's in the library.**
Their groups hide that platform or rating. Check their groups under **Settings → Users & groups**. New downloads count as Unrated until RomM matches them.

**Every request waits for approval, even ones a profile should approve.**
Check the profile order under **Settings → Auto-approval**: the first one that fits wins, so a narrower profile above a broader one is fine, but a disabled or wrong-group profile does nothing. A game for a platform that isn't set up yet also needs a profile that approves platforms. Rating rules treat games as Unrated until the LaunchBox database has finished downloading (see `docker logs romseek`).

**An old game counts as Unrated.**
Most games from before 1994 have no ESRB rating. Tick **Unrated** in the profile (or group) for retro platforms.

**"Groups come from LLDAP" when I try to make a group.**
The LLDAP sync account is set, so LLDAP owns the groups. Make the group in LLDAP, put people in it, and it shows up in RomSeek at the next sync (or press **Sync LLDAP now**).

**An approved request says "couldn't start it".**
The indexer didn't hand over the download (it may be down). The request stays in the queue: try again later, or deny it and ask them to pick another result.

**Settings → Sign-in says "LLDAP refused LLDAP_SYNC_USER's password".**
The sync account doesn't exist in LLDAP yet, or has a different password. Follow the steps shown there, then press **Sync now**.

**Forgot the admin password.**
See [Locked out?](setup.md#locked-out).

**Still stuck?**
Ask in [Discussions → Q&A](https://github.com/Evident-Dev/romseek/discussions/categories/q-a). Include your RomSeek log (`docker logs --tail 100 romseek`) with keys and tokens removed.

---

[← Back to README](../README.md)
