"""Shared widgets and helpers for Shoebox UI."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Gio, Gtk

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


class ThumbnailTile(Gtk.Overlay):
    """Square tile with lazy-loaded image and a "local only" indicator."""

    __gtype_name__ = 'ShoeboxThumbnailTile'

    def __init__(self):
        super().__init__()
        self._size = 256
        self._asset: Optional[Asset] = None
        self.set_size_request(140, 140)

        self._picture = Gtk.Picture()
        self._picture.set_can_shrink(True)
        self._picture.set_content_fit(Gtk.ContentFit.COVER)
        self._picture.add_css_class('gallery-thumb')
        self.set_child(self._picture)

        self._badge = Gtk.Image.new_from_icon_name('folder-symbolic')
        self._badge.set_halign(Gtk.Align.END)
        self._badge.set_valign(Gtk.Align.START)
        self._badge.set_margin_top(4)
        self._badge.set_margin_end(4)
        self._badge.add_css_class('osd')
        self._badge.set_visible(False)
        self.add_overlay(self._badge)

        self._spinner = Adw_spinner_or_fallback()
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._spinner.set_valign(Gtk.Align.CENTER)
        self.add_overlay(self._spinner)

    def bind(self, asset: Asset, size: int, backend: Optional['Backend']) -> None:
        self._asset = asset
        self._size = size
        self._picture.set_paintable(None)
        self._badge.set_visible(asset.is_local_only)
        self._spinner.set_visible(True)
        if hasattr(self._spinner, 'start'):
            self._spinner.start()

        def load() -> Optional[bytes]:
            return _load_thumbnail_bytes(asset, size, backend)

        def done(data: Optional[bytes]) -> None:
            if self._asset is not asset:
                return  # row was rebound to a different asset
            self._spinner.set_visible(False)
            if hasattr(self._spinner, 'stop'):
                self._spinner.stop()
            if not data:
                return
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                self._picture.set_paintable(texture)
            except GLib.Error:
                pass

        run_async(load, on_done=done, on_error=lambda _e: done(None))


def Adw_spinner_or_fallback() -> Gtk.Widget:
    """Adw.Spinner is in libadwaita 1.6+, fall back to GtkSpinner otherwise."""
    try:
        from gi.repository import Adw
        return Adw.Spinner()
    except (AttributeError, TypeError):
        s = Gtk.Spinner()
        s.start()
        return s
