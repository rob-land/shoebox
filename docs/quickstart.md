# Shoebox — Quick start

A GTK4 / libadwaita gallery client for self-hosted photo services.
Phone-first, desktop-friendly. Today's backend is Immich;
PhotoPrism and similar can be added as new modules under
`shoebox.backends`. SQLite catalog locally; tokens in libsecret;
NetworkManager + UPower gating so background sync respects your
Wi-Fi / battery rules.

## Install

```bash
flatpak install --user rob-land land.rob.shoebox
```

## First-time setup

Launch Shoebox. The **Setup** page walks you through:

1. **Pick a backend** — only Immich is in the dropdown today.
2. **Server URL** — your Immich base URL
   (`https://photos.example.com`).
3. **Sign in** — username + password. Shoebox exchanges them for
   an API token via `/auth/login` and stores it in libsecret.
4. **Pick folders to sync** — pick local directories that should
   upload to Immich (typically `~/Pictures` and `~/DCIM`-like
   paths from your phone).

After setup the **Gallery** view loads showing your Immich assets.
Sync starts in the background if you turned **Sync automatically**
on.

## Daily use

### Gallery view

`Adw.NavigationSplitView` (when wide enough):

- **Sidebar (left)** — album list. Albums come from Immich.
- **Content** — paginated thumbnail grid for the selected album
  (or "All photos" when none selected).
- **Detail** — full-size preview when you tap a thumbnail, with
  metadata (date, location, camera).

On phone widths the sidebar collapses; you navigate via
back-button between albums → grid → detail.

### Search

Top-bar search box filters by filename / date range / album.
Server-side, so it works across your entire library, not just
the loaded page.

### Adding photos

Two paths:

- **Upload from disk** — pick files / folders, Shoebox queues
  them for upload. Sync conditions (see below) gate when they
  actually push.
- **Watch a folder** — directories you configured during setup
  get scanned and new files queued automatically.

The local SQLite catalog tracks every asset by its perceptual
hash so re-importing the same photo (or syncing the same source
folder twice) doesn't create duplicates server-side.

## Sync conditions

This is the feature that makes Shoebox actually usable on a
phone. **Preferences → When to sync**:

- **Sync automatically** — master toggle. When off, only manual
  sync runs.
- **Network** — `Any connection`, `Wi-Fi only`, or
  `Unmetered connections`. Wi-Fi-only is the typical phone
  default. "Unmetered" uses NetworkManager's
  `connection-metered` property.
- **Only while charging** — pause uploads when the phone's on
  battery. Combined with Wi-Fi-only this gives the "upload
  overnight while charging" pattern most people want.

NetworkManager and UPower drive the checks over D-Bus. If
your phone reports metered status correctly (most carriers'
NM profiles do), the "unmetered" mode handles roaming and
hotspot-tethering correctly.

## Background sync

**Preferences → Background**:

- **Run in background** — closing the window keeps the sync
  manager running. Periodic sync timer (every 30 minutes by
  default) fires while you're not actively using Shoebox.
- **Run at startup** — registers an autostart entry that
  launches `shoebox --background` at login. Sync runs from boot,
  respecting the conditions above.

The periodic sync is a no-op when conditions aren't met (no
Wi-Fi, on battery while charging-only is set, etc.) — it just
re-checks and waits for the next tick.

## Where things are kept

| What | Path |
| --- | --- |
| Local catalog (SQLite) | `~/.var/app/land.rob.shoebox/data/shoebox/shoebox.db` |
| Watched folders + prefs | GSettings (`land.rob.shoebox`) + DB |
| Immich API token | libsecret |
| Logs | `~/.var/app/land.rob.shoebox/data/shoebox/shoebox.log` |
| Autostart entry (if enabled) | `~/.config/autostart/land.rob.shoebox.desktop` |

The Flatpak manifest grants `--filesystem=xdg-pictures`,
`xdg-videos`, and `xdg-download` so the upload paths work
without further config. To watch a folder outside those, edit
the manifest's finish-args.

## Notable limits

- **Immich only**, today. The backend layer (`shoebox.backends`)
  is abstract — adding PhotoPrism or another provider is a
  matter of subclassing `backends.base.Backend` and registering
  it in `backends/__init__.py`. Open an issue if you want one
  prioritized.
- **No video transcoding** — videos upload as-is. Whatever your
  Immich server transcodes them to is what plays back.
- **No metadata editing** — read-only client. Edit timestamps /
  tags / album membership in Immich's own web UI.
- **No facial recognition or AI features** — those are server-
  side in Immich; Shoebox just renders the results.
- **Sharing** — Immich's share-by-link works from the Immich web
  UI; Shoebox doesn't surface a share button yet.
