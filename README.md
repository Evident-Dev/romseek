<img src="logo.svg" width="96" alt="RomSeek logo">

# RomSeek

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg)](https://www.python.org)
[![Docker](https://img.shields.io/badge/docker-python%3A3.12--alpine-2496ED.svg)](docker-compose.example.yml)

RomSeek is a game download manager for [RomM](https://github.com/rommapp/romm). It does for games what Sonarr and Radarr do for TV and movies. Search your [Prowlarr](https://prowlarr.com) indexers, pick a game, and RomSeek sends it to qBittorrent. When the download finishes, RomSeek puts it in your RomM library.

## Major Features

- Search every Prowlarr indexer for console games in one place
- Detects the system for each result from the release name, a 56,000-title game list and your past choices
- Sends downloads to qBittorrent and imports finished games into RomM on its own
- Sets up BIOS files for the systems that need them (upload your own dumps, or use the maker's official download where there is one)
- Blocks Windows programs and junk files from your library
- Optional virus scan gate that works with ClamAV
- Library view by system, genre and auto-updating collections
- Game pages with three steps: get the game, get the emulator, get the BIOS
- Admin tools to fix metadata, edit details and remove games
- Dashboard stats for [Homepage](https://gethomepage.dev)
- One Python file with no dependencies. Runs on the stock `python:3.12-alpine` image.

## Getting Started

You need RomM, Prowlarr, qBittorrent and a reverse proxy with a login, all in Docker.

1. Clone this repo into your compose project folder.
2. Copy `.env.example` to `romseek.env` and add your Prowlarr API key.
3. Add the service from `docker-compose.example.yml` to your compose file.
4. Start it and put it behind your proxy's login.

> ⚠️ RomSeek has no login of its own. Never open its port to the internet.

The full guide takes about 20 minutes: **[Setup guide](docs/setup.md)**

## Documentation

- [Setup guide](docs/setup.md): install and connect everything, step by step
- [Virus scanning](docs/virus-scanning.md): built-in checks, ClamAV, or your own scanner
- [Configuration](docs/configuration.md): every setting in `romseek.env`
- [Features](docs/features.md): admin tools, collections, the Homepage widget, and how imports work
- [Troubleshooting](docs/troubleshooting.md): fixes for common problems
- [Legal](docs/legal.md): your rights to download, and what not to post here

## Support

- [Ask a question or get setup help](../../discussions/categories/q-a) (GitHub Discussions)
- [Share an idea](../../discussions/categories/ideas) or [show off your setup](../../discussions/categories/show-and-tell)
- [Report a bug](../../issues/new?template=bug_report.yml)
- [Request a feature](../../issues/new?template=feature_request.yml)
- [Report a security problem](SECURITY.md)

## Contributing

Pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## Your rights to download

RomSeek is a tool, like Sonarr or Radarr. It doesn't include, host or link to any games, BIOS files or keys, and it only searches indexers you add yourself.

**Only download games, BIOS and firmware you have the legal right to**, such as your own dumps of cartridges, discs and consoles you own, homebrew, or content the rights holder gives away. Laws differ by country, and getting around copy protection (for example decryption keys) is illegal in many places even for games you own.

Don't use this project's Issues or Discussions to share or ask for ROMs, BIOS files, keys or download sites. Those posts will be removed.

Read more: **[Legal: your rights to download](docs/legal.md)** (general information, not legal advice).

## License

- [MIT](LICENSE)
- Copyright 2026 Evident-Dev
