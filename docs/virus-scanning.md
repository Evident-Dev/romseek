# Virus scanning

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

[← Back to README](../README.md)
