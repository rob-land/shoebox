# Shoebox — CLAUDE.md

## What this project is

An adaptive GTK4/libadwaita gallery client for self-hosted photo
services (Immich today; pluggable backend layer for PhotoPrism etc.
later). Phone-first, desktop-friendly. Local SQLite catalog plus
libsecret-stored tokens, GSettings prefs. NetworkManager + UPower
gating for Wi-Fi-only / unmetered / charging-only sync.

App ID: `land.rob.Shoebox`. License: GPL-3.0-or-later.

Written entirely by Claude. PRs welcome.

## Before making changes

Read [`STYLE_GUIDE.md`](STYLE_GUIDE.md) first when touching any of:

- Meson build files, the Flatpak manifest, or `requirements.txt`
- Anything under `data/ui/` or `data/icons/`
- New top-level Python files, or new modules under `src/<pkg>/`
- Imports — especially `import gi` / `gi.require_version`
- New launcher / `.in` substitution targets

A `Stop` hook in `.claude/settings.json` runs
`~/projects/style-check.py` and surfaces violations back at the end of
each turn. The recurring slip across the sibling projects has been
reintroducing per-file `gi.require_version` blocks — `src/shoebox/main.py`
is the single declaration site.

## Tech stack

- **Language**: Python 3.10+
- **UI toolkit**: GTK4 + libadwaita (PyGObject), `.ui` XML templates
  bundled via GResource (the `.blp` migration that the rest of the
  collection has done is still pending here)
- **Build system**: Meson + Ninja
- **Packaging**: Flatpak (manifest:
  `build-aux/flatpak/land.rob.Shoebox.json`), GNOME 50 SDK
- **Storage**: SQLite catalog, libsecret tokens, GSettings prefs

## Source layout

```
meson.build, meson_options.txt
build-aux/flatpak/
  land.rob.Shoebox.json        Flatpak manifest
  build-flatpak.sh             multi-arch driver (in-tree)
data/
  land.rob.Shoebox.{desktop,metainfo.xml,gschema.xml}.in*
  shoebox.gresource.xml
  ui/                          Gtk.Builder XML templates
  icons/
src/
  meson.build
  shoebox.in                   Launcher (Meson-substituted)
  shoebox/
    __init__.py, main.py, application.py, window.py
    database.py, secrets.py, settings.py, worker.py
    backends/                  base + Immich
    sync/                      manager, scanner, conditions
    ui/                        Python-side view modules
```

## Key conventions

- See [`STYLE_GUIDE.md`](STYLE_GUIDE.md) for the cross-project
  conventions (this is a sibling of banter, clicker, finlit, jamjar,
  tonic, coffer).
- `gi.require_version` is declared once in `src/shoebox/main.py`;
  every other module just `from gi.repository import …`.
- Backend layer: every concrete client subclasses `backends/base.py`
  so swapping Immich → PhotoPrism is a config flip, not a refactor.
- Sync conditions live in `sync/conditions.py` and read
  NetworkManager / UPower over D-Bus — don't sprinkle the checks
  inline.

## Things to watch out for

- The Flatpak manifest grants `--filesystem=xdg-pictures`,
  `xdg-videos`, and `xdg-download` plus the system-bus
  NetworkManager/UPower talk-names. New permissions need a real
  reason — flag them in commit messages so Flathub review is easier.
- Tokens go through `secrets.py` (libsecret). Never write tokens to
  GSettings, the catalog, or stdout.
