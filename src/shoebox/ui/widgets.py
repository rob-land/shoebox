"""Shared widgets and helpers for Shoebox UI."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, GObject, Gio, Gtk

from ..database import Asset
from ..worker import run_async

if TYPE_CHECKING:
    from ..backends import Backend


# ---- model item --------------------------------------------------------------


class AssetItem(GObject.Object):
    __gtype_name__ = 'ShoeboxAssetItem'

    def __init__(self, asset: Asset):
        super().__init__()
        self.asset = asset


# ---- thumbnail cache ---------------------------------------------------------


def _cache_dir() -> Path:
    base = Path(GLib.get_user_cache_dir()) / 'shoebox' / 'thumbnails'
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cache_path(asset: Asset, size: int) -> Path:
    if asset.checksum:
        key = asset.checksum
    elif asset.remote_id:
        key = 'r-' + asset.remote_id
    else:
        key = 'l-' + hashlib.sha1(
            (asset.local_path or '').encode('utf-8')
        ).hexdigest()
    return _cache_dir() / f'{key}-{size}.jpg'


def _load_thumbnail_bytes(
    asset: Asset, size: int, backend: Optional['Backend']
) -> Optional[bytes]:
    cached = _cache_path(asset, size)
    if cached.exists():
        return cached.read_bytes()

    data: Optional[bytes] = None
    if asset.local_path and Path(asset.local_path).is_file():
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                asset.local_path, size, size, True
            )
            ok, raw = pixbuf.save_to_bufferv('jpeg', ['quality'], ['82'])
            if ok:
                data = bytes(raw)
        except GLib.Error:
            data = None

    if data is None and asset.remote_id and backend is not None:
        try:
            data = backend.fetch_thumbnail(asset.remote_id, size)
        except Exception:  # noqa: BLE001
            data = None

    if data:
        try:
            cached.write_bytes(data)
        except OSError:
            pass
    return data


# ---- thumbnail widget --------------------------------------------------------


@Gtk.Template(resource_path='/land/rob/shoebox/ui/thumbnail-tile.ui')
class ThumbnailTile(Gtk.Overlay):
    """Square tile with lazy-loaded image and a "local only" indicator."""

    __gtype_name__ = 'ShoeboxThumbnailTile'

    picture: Gtk.Picture = Gtk.Template.Child()
    badge:   Gtk.Image   = Gtk.Template.Child()
    spinner: Adw.Spinner = Gtk.Template.Child()

    def __init__(self):
        super().__init__()
        self._size = 256
        self._asset: Optional[Asset] = None

    def bind(self, asset: Asset, size: int, backend: Optional['Backend']) -> None:
        self._asset = asset
        self._size = size
        self.picture.set_paintable(None)
        self.badge.set_visible(asset.is_local_only)
        self.spinner.set_visible(True)

        def load() -> Optional[bytes]:
            return _load_thumbnail_bytes(asset, size, backend)

        def done(data: Optional[bytes]) -> None:
            if self._asset is not asset:
                return  # row was rebound to a different asset
            self.spinner.set_visible(False)
            if not data:
                return
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                self.picture.set_paintable(texture)
            except GLib.Error:
                pass

        run_async(load, on_done=done, on_error=lambda _e: done(None))


def Adw_spinner_or_fallback() -> Gtk.Widget:
    """Adw.Spinner shim — kept for gallery.py until it migrates to a
    @Gtk.Template that can declare the widget directly in Blueprint."""
    return Adw.Spinner()
