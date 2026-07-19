# Shoebox — CLAUDE.md

## What this project is

An adaptive GTK4/libadwaita gallery client for self-hosted photo
services (Immich today; pluggable backend layer for PhotoPrism etc.
later). Phone-first, desktop-friendly. Local SQLite catalog plus
libsecret-stored tokens, GSettings prefs. NetworkManager + UPower
gating for Wi-Fi-only / unmetered / charging-only sync.

App ID: `land.rob.shoebox`. License: GPL-3.0-or-later.

## Code quality

A core goal is well-structured, readable code that follows idiomatic Python (PEP 8) and GNOME / libadwaita conventions; the cohort-shared [`STYLE_GUIDE.md`](STYLE_GUIDE.md) layers on top. When existing code doesn't meet that bar, refactor rather than perpetuate the pattern.

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
- **UI toolkit**: GTK4 + libadwaita (PyGObject); UI authored in
  Blueprint (`.blp`) under `data/ui/`, compiled to `.ui` at build
  time and bundled via GResource. Python widgets are thin
  `@Gtk.Template`-decorated wrappers around the templates; dynamic
  per-item content (gallery tiles, folder rows) stays programmatic.
- **Build system**: Meson + Ninja
- **Packaging**: Flatpak (manifest:
  `build-aux/flatpak/land.rob.shoebox.json`), GNOME 50 SDK
- **Storage**: SQLite catalog, libsecret tokens, GSettings prefs

## Source layout

```
meson.build, meson_options.txt
build-all.sh                   multi-arch flatpak driver (root)
fix-flatpak-deps.py             tarball -> wheel patcher
requirements.txt                Python runtime deps (currently empty)
run.sh                          dev launcher: meson install + run from _install
build-aux/flatpak/
  land.rob.shoebox.json        Flatpak manifest
data/
  land.rob.shoebox.{desktop,metainfo.xml,gschema.xml}.in*
  ui/
    meson.build                blueprint-compiler + gnome.compile_resources
    land.rob.shoebox.gresource.xml
    style.css
    *.blp                      Blueprint UI templates
  icons/
po/
  LINGUAS, POTFILES.in, meson.build (no translations yet)
src/
  meson.build
  shoebox.in                   Launcher (Meson-substituted)
  shoebox/
    __init__.py, main.py, application.py, window.py
    database.py, secrets.py, settings.py, worker.py
    backends/                  base + Immich
    sync/                      manager, scanner, conditions
    ui/                        @Gtk.Template wrappers for the .blp templates
tests/
  meson.build, test_smoke.py   pytest target wired into `meson test`
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
- Remote sync rides Immich's v2 change feed (`/api/sync/stream` +
  `/api/sync/ack`), which requires a *session* token from password
  login — Immich rejects API keys there. Checkpoints live server-side
  per session; the `change-feed-ready` account_state flag decides when
  to request a reset (full) feed. Never issue a blocking libsoup call
  on the thread-default main context while the feed's response stream
  is open — that recurses inside libsoup; see `_send_acks`.
