"""Single-photo detail view."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from gi.repository import Adw, Gdk, GLib, Gtk

from ..database import Asset
from ..worker import run_async

if TYPE_CHECKING:
    from ..window import ShoeboxWindow


@Gtk.Template(resource_path='/land/rob/shoebox/ui/detail.ui')
class DetailPage(Adw.NavigationPage):
    __gtype_name__ = 'ShoeboxDetailPage'

    local_badge: Gtk.Image   = Gtk.Template.Child()
    picture:     Gtk.Picture = Gtk.Template.Child()
    spinner:     Adw.Spinner = Gtk.Template.Child()

    def __init__(self, window: 'ShoeboxWindow', asset: Asset):
        super().__init__(title=asset.filename or 'Photo')
        self.window = window
        self.asset = asset

        if asset.local_path:
            self.local_badge.set_tooltip_text(f'Local copy: {asset.local_path}')
            self.local_badge.set_visible(True)

        self.connect('shown', lambda *_: self._load())

    def _load(self) -> None:
        if self.asset.local_path and Path(self.asset.local_path).is_file():
            self._load_local()
        elif self.asset.remote_id:
            self._load_remote()
        else:
            self.spinner.set_visible(False)

    def _load_local(self) -> None:
        path = self.asset.local_path

        def work() -> Optional[bytes]:
            try:
                return Path(path).read_bytes()
            except OSError:
                return None

        run_async(work, on_done=self._set_bytes,
                  on_error=lambda _e: self._set_bytes(None))

    def _load_remote(self) -> None:
        backend = self.window.app.primary_backend()
        if backend is None:
            self.spinner.set_visible(False)
            return
        remote_id = self.asset.remote_id

        def work() -> Optional[bytes]:
            try:
                return backend.fetch_original(remote_id)
            except Exception:  # noqa: BLE001
                return None

        run_async(work, on_done=self._set_bytes,
                  on_error=lambda _e: self._set_bytes(None))

    def _set_bytes(self, data: Optional[bytes]) -> None:
        self.spinner.set_visible(False)
        if not data:
            self.window.toast('Failed to load image')
            return
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            self.picture.set_paintable(texture)
        except GLib.Error:
            self.window.toast('Unsupported image format')
