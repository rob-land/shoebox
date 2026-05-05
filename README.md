# Shoebox

An adaptive GTK4 / libadwaita gallery client for self-hosted photo services.
Phone-first, desktop-friendly. Initial backend: Immich. Architecture allows
additional backends (e.g. PhotoPrism).

## Status

Early scaffold. Working pieces:

- Backend abstraction with an Immich implementation (auth, list assets, fetch
  thumbnails, upload)
- SQLite catalog, libsecret token storage, GSettings preferences
- Adaptive AdwApplicationWindow with NavigationView and AdwBreakpoint
- First-run setup wizard (server URL → login → pick sync directories)
- Gallery (server thumbnails + unsynced local photos) with detail page
- Local directory scanner
- Sync manager with Wi-Fi / metered / charging condition checks
  (NetworkManager + UPower over D-Bus)
- Preferences

Known gaps to fill in: real upload pipeline, background sync via systemd user
service, video support, album browsing, search.

## Build

```sh
meson setup build
meson compile -C build
meson install -C build  # installs to /usr/local by default
shoebox
```

To run from the build tree without installing, use:

```sh
meson devenv -C build
./build/src/shoebox
```

## Flatpak

```sh
./build-aux/flatpak/build-flatpak.sh           # both x86_64 and aarch64
./build-aux/flatpak/build-flatpak.sh x86_64    # one arch only
```

Bundles land in `dist/`. Cross-architecture builds need `qemu-user-static`
with `binfmt_misc` registered (Fedora: `sudo dnf install qemu-user-static`;
Debian/Ubuntu: `sudo apt install qemu-user-static binfmt-support`). If the
binfmt entry is missing for a requested arch, the script skips it with a
warning instead of failing.

The manifest pulls `org.gnome.Platform//50` (and SDK) from Flathub.

## Runtime dependencies

- GTK 4 ≥ 4.20 (GNOME 50 target)
- libadwaita ≥ 1.8
- libsoup 3
- libsecret
- python3-gobject
- gir1.2-{gtk-4.0,adw-1,soup-3.0,secret-1,nm-1.0,upowerglib-1.0}

## Project layout

```
data/                 desktop file, metainfo, gschema, gresource, icons
src/shoebox/          Python package
  application.py      AdwApplication + lifecycle
  window.py           Main AdwApplicationWindow, navigation, breakpoints
  database.py         SQLite catalog
  secrets.py          libsecret wrapper
  settings.py         GSettings wrapper
  backends/
    base.py           Abstract Backend interface
    immich.py         Immich implementation
  sync/
    manager.py        Orchestrates uploads given local + condition state
    scanner.py        Walks sync directories, hashes new files
    conditions.py     NetworkManager + UPower observers
  ui/
    setup.py          First-run wizard
    gallery.py        Adaptive grid view
    detail.py         Single-photo viewer
    preferences.py    AdwPreferencesWindow
    widgets.py        Shared widgets (thumbnail, status pill)
```

## Adding a backend

Implement `shoebox.backends.base.Backend` and register it in
`shoebox.backends.__init__.BACKENDS`. The setup wizard auto-discovers
registered backends.
