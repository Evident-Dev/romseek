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

**A result has the wrong system.**
Pick the right one before you grab. RomSeek remembers your choice for that title next time.

**PC games show up in results.**
Some indexers mark PC games as Console. Results with no known system sort last. Use `EXCLUDED_INDEXERS` to hide a bad indexer.

---

[← Back to README](../README.md)
