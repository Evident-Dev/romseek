# Features

## Admin tools and extras

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

[← Back to README](../README.md)
