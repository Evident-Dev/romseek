# Contributing to RomSeek

Thanks for helping. Keep changes small and focused.

## Rules

- RomSeek uses the Python standard library only. Do not add dependencies.
- The backend stays in `romseek.py`. The frontend stays in `index.html`. There is no build step.
- Never add a feature that imports a file before the safety checks run.
- Don't add links to, or search presets for, ROM/BIOS sites, trackers or decryption keys. Official sources (a console maker's own firmware page, an emulator's own website) are fine. See [docs/legal.md](docs/legal.md).
- RomSeek has no login. Any new admin-only endpoint must check `is_admin(self.headers)` first, like the existing ones.

## Run it locally

1. Copy `.env.example` to `romseek.env` and fill it in.
2. Run it with the example compose file, or directly:

   ```bash
   set -a; . ./romseek.env; set +a
   STATE_DIR=./state python3 romseek.py
   ```

3. Open `http://localhost:8000`.

## Pull requests

1. For anything bigger than a bug fix, start in [Discussions → Ideas](https://github.com/Evident-Dev/romseek/discussions/categories/ideas) or open an issue first.
2. Make one change per pull request.
3. Say what you tested and on which RomM, Prowlarr and qBittorrent versions.
4. Update the docs in `docs/` if you change a setting or a screen.
