"""Shared widgets and helpers for Shoebox UI."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, GObject, Gio, Gtk

from ..database import Asset

if TYPE_CHECKING:
    from ..backends import Backend


log = logging.getLogger(__name__)

# Recently-used decoded textures, keyed by (asset_id, size). When a tile
# rebinds and the bytes are already in memory we skip disk + decode and
# paint instantly — fixes the "spinner flash on every scroll" problem.
# 200 entries × ~256 KB decoded each ≈ 50 MB upper bound.
_TEXTURE_CACHE_LIMIT = 200
_texture_cache: 'OrderedDict[tuple[int, int], Gdk.Texture]' = OrderedDict()
_texture_cache_lock = threading.Lock()


def _texture_cache_get(key: tuple[int, int]) -> Optional[Gdk.Texture]:
    with _texture_cache_lock:
        tex = _texture_cache.get(key)
        if tex is not None:
            _texture_cache.move_to_end(key)
        return tex


def _texture_cache_put(key: tuple[int, int], texture: Gdk.Texture) -> None:
    with _texture_cache_lock:
        _texture_cache[key] = texture
        _texture_cache.move_to_end(key)
        while len(_texture_cache) > _TEXTURE_CACHE_LIMIT:
            _texture_cache.popitem(last=False)


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
        except GLib.Error as e:
            log.debug('local thumbnail decode failed for %s: %s',
                      asset.local_path, e)

    if data is None and asset.remote_id and backend is not None:
        # libsoup3 sessions are nominally thread-safe but get jittery
        # under bursty concurrent loads — one retry mops up the
        # transient failures that produced the empty-tile bug.
        for attempt in range(2):
            try:
                data = backend.fetch_thumbnail(asset.remote_id, size)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    time.sleep(0.05)
                    continue
                log.debug('remote thumbnail %s failed: %s',
                          asset.remote_id, e)

    if data:
        try:
            cached.write_bytes(data)
        except OSError:
            pass
    return data


# A small dedicated pool keeps libsoup happy: each visible scroll page
# can spawn dozens of binds at once, and an unbounded thread-per-bind
# strategy raced the shared Soup.Session and dropped 1/3 to 1/2 of
# requests. Six is enough to saturate any reasonable Immich and small
# enough that nothing trips over itself.
_thumb_pool: Optional[ThreadPoolExecutor] = None


def _get_thumb_pool() -> ThreadPoolExecutor:
    global _thumb_pool
    if _thumb_pool is None:
        _thumb_pool = ThreadPoolExecutor(
            max_workers=6, thread_name_prefix='shoebox-thumb',
        )
    return _thumb_pool


def _submit_thumbnail(
    asset: Asset, size: int, backend: Optional['Backend'],
    on_done,
) -> Future:
    def worker() -> None:
        try:
            data = _load_thumbnail_bytes(asset, size, backend)
        except Exception:  # noqa: BLE001 — must always reach on_done
            log.exception('thumbnail worker crashed')
            data = None
        GLib.idle_add(_safely_call, on_done, data)

    return _get_thumb_pool().submit(worker)


def _safely_call(cb, arg) -> bool:
    try:
        cb(arg)
    except Exception:  # noqa: BLE001
        log.exception('thumbnail callback failed')
    return False


# ---- thumbnail widget --------------------------------------------------------


@Gtk.Template(resource_path='/land/rob/shoebox/ui/thumbnail-tile.ui')
class ThumbnailTile(Gtk.Overlay):
    """Square tile with lazy-loaded image and a "local only" indicator."""

    __gtype_name__ = 'ShoeboxThumbnailTile'

    picture:        Gtk.Picture = Gtk.Template.Child()
    badge:          Gtk.Image   = Gtk.Template.Child()
    spinner:        Adw.Spinner = Gtk.Template.Child()
    select_check:   Gtk.Image   = Gtk.Template.Child()
    favorite_badge: Gtk.Image   = Gtk.Template.Child()

    def __init__(self):
        super().__init__()
        self._size = 256
        self._asset: Optional[Asset] = None
        self._select_cb = None  # set by the GridView factory; receives Asset
        self._pending: Optional[Future] = None

        long_press = Gtk.GestureLongPress()
        long_press.set_touch_only(False)
        long_press.connect('pressed', self._on_long_press)
        self.add_controller(long_press)

        ctrl_click = Gtk.GestureClick()
        ctrl_click.set_button(1)
        ctrl_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctrl_click.connect('pressed', self._on_click_pressed)
        self.add_controller(ctrl_click)

    def set_select_callback(self, cb) -> None:
        self._select_cb = cb

    def _on_long_press(self, _gesture, _x, _y) -> None:
        if self._asset is not None and self._select_cb is not None:
            self._select_cb(self._asset)

    def _on_click_pressed(self, gesture, _n_press, _x, _y) -> None:
        # Only intercept on Ctrl-click; let bare clicks propagate to the
        # GridView's activate signal so normal navigation still works.
        state = gesture.get_current_event_state()
        if not (state & Gdk.ModifierType.CONTROL_MASK):
            return
        if self._asset is not None and self._select_cb is not None:
            self._select_cb(self._asset)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def bind(
        self,
        asset: Asset,
        size: int,
        backend: Optional['Backend'],
        *,
        selected: bool = False,
        show_check: bool = False,
    ) -> None:
        # Drop any prior load — Future.cancel only works if the worker
        # hasn't started yet, but during fast scrolling that's the common
        # case (queued tasks pile up faster than 6 workers can drain).
        # Releases queue slots so newly-visible tiles get serviced first.
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

        self._asset = asset
        self._size = size
        self.badge.set_visible(asset.is_local_only)
        self.favorite_badge.set_visible(asset.is_favorite)
        self.set_selected(selected, show_check=show_check)

        # Fast path: decoded texture in memory cache → paint immediately,
        # no spinner, no work.
        cache_key = (asset.id, size)
        cached = _texture_cache_get(cache_key)
        if cached is not None:
            self.picture.set_paintable(cached)
            self.spinner.set_visible(False)
            return

        self.picture.set_paintable(None)
        self.spinner.set_visible(True)

        def done(data: Optional[bytes]) -> None:
            if self._asset is not asset:
                return  # row was rebound to a different asset
            self._pending = None
            self.spinner.set_visible(False)
            if not data:
                return
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except GLib.Error:
                return
            _texture_cache_put(cache_key, texture)
            self.picture.set_paintable(texture)

        self._pending = _submit_thumbnail(asset, size, backend, done)

    def set_selected(self, selected: bool, *, show_check: bool = True) -> None:
        if selected:
            self.picture.add_css_class('selected')
            self.select_check.set_visible(show_check)
        else:
            self.picture.remove_css_class('selected')
            self.select_check.set_visible(False)


def Adw_spinner_or_fallback() -> Gtk.Widget:
    """Adw.Spinner shim — kept for gallery.py until it migrates to a
    @Gtk.Template that can declare the widget directly in Blueprint."""
    return Adw.Spinner()
