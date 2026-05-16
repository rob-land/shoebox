"""Single-photo detail view with collapsible info sidebar."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ..database import Asset
from ..worker import run_async

if TYPE_CHECKING:
    from ..window import ShoeboxWindow


@Gtk.Template(resource_path='/land/rob/shoebox/ui/detail.ui')
class DetailPage(Adw.NavigationPage):
    __gtype_name__ = 'ShoeboxDetailPage'

    local_badge:    Gtk.Image          = Gtk.Template.Child()
    info_toggle:    Gtk.ToggleButton   = Gtk.Template.Child()
    picture:        Gtk.Picture        = Gtk.Template.Child()
    spinner:        Adw.Spinner        = Gtk.Template.Child()
    split:          Adw.OverlaySplitView = Gtk.Template.Child()
    sidebar_groups: Gtk.Box            = Gtk.Template.Child()

    def __init__(self, window: 'ShoeboxWindow', asset: Asset):
        super().__init__(title=asset.filename or 'Photo')
        self.window = window
        self.asset = asset

        if asset.local_path:
            self.local_badge.set_tooltip_text(f'Local copy: {asset.local_path}')
            self.local_badge.set_visible(True)

        # Mirror the window's compact state onto the split: on phone widths
        # the sidebar overlays the picture instead of docking beside it.
        self.split.set_collapsed(self.window.compact)
        self.window.connect(
            'notify::compact',
            lambda *_: self.split.set_collapsed(self.window.compact),
        )

        self._populate_sidebar()
        self.connect('shown', lambda *_: self._load())

    # ----- image load -----

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

    # ----- info sidebar -----

    def _populate_sidebar(self) -> None:
        a = self.asset

        # Clear any prior content (sidebar is rebuilt after edits).
        child = self.sidebar_groups.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.sidebar_groups.remove(child)
            child = nxt

        file_rows = [
            ('Filename', a.filename or '—'),
            ('Type', a.mime_type or '—'),
            ('Dimensions', _format_dimensions(a.width, a.height)),
            ('Size', _format_size(a.size_bytes)),
        ]
        self.sidebar_groups.append(_build_group('File', file_rows))

        capture_rows = [
            ('Date', _format_date(a.taken_at)),
            ('Camera', _format_camera(a.camera_make, a.camera_model)),
            ('Lens', a.lens or '—'),
            ('Settings', _format_settings(
                a.iso, a.f_number, a.exposure_time, a.focal_length,
            )),
        ]
        self.sidebar_groups.append(_build_group('Capture', capture_rows))

        if a.has_location or a.place_label:
            self.sidebar_groups.append(self._build_location_group())

        if a.description:
            self.sidebar_groups.append(_build_group(
                'Description', [(None, a.description)],
            ))

        # Edit row at the bottom — single entry to the metadata dialog.
        edit_group = Adw.PreferencesGroup()
        edit_row = Adw.ButtonRow(
            title='Edit metadata',
            start_icon_name='document-edit-symbolic',
        )
        edit_row.connect('activated', self._on_edit_clicked)
        edit_group.add(edit_row)
        self.sidebar_groups.append(edit_group)

    def _on_edit_clicked(self, _row) -> None:
        from .edit_dialog import EditMetadataDialog
        dialog = EditMetadataDialog(self.window, self.asset, on_saved=self._on_saved)
        dialog.present(self.window)

    def _on_saved(self, refreshed: Asset) -> None:
        self.asset = refreshed
        self._populate_sidebar()

    def _build_location_group(self) -> Gtk.Widget:
        a = self.asset
        group = Adw.PreferencesGroup(title='Location')

        place_row = Adw.ActionRow(
            title=a.place_label or 'Coordinates',
            subtitle=_format_coords(a.latitude, a.longitude),
        )
        place_row.add_css_class('property')
        group.add(place_row)

        if a.has_location:
            open_btn = Gtk.Button(
                icon_name='mark-location-symbolic',
                valign=Gtk.Align.CENTER,
                tooltip_text='Open in maps',
            )
            open_btn.add_css_class('flat')
            open_btn.connect('clicked', self._on_open_in_maps)
            place_row.add_suffix(open_btn)

            copy_btn = Gtk.Button(
                icon_name='edit-copy-symbolic',
                valign=Gtk.Align.CENTER,
                tooltip_text='Copy coordinates',
            )
            copy_btn.add_css_class('flat')
            copy_btn.connect('clicked', self._on_copy_coords)
            place_row.add_suffix(copy_btn)

        return group

    def _on_open_in_maps(self, _btn) -> None:
        a = self.asset
        if not a.has_location:
            return
        uri = f'geo:{a.latitude},{a.longitude}?q={a.latitude},{a.longitude}'
        launcher = Gtk.UriLauncher.new(uri)
        launcher.launch(self.window, None, None)

    def _on_copy_coords(self, _btn) -> None:
        a = self.asset
        if not a.has_location:
            return
        text = f'{a.latitude:.6f}, {a.longitude:.6f}'
        clipboard = self.get_display().get_clipboard() if hasattr(self, 'get_display') else None
        if clipboard is None:
            display = Gdk.Display.get_default()
            clipboard = display.get_clipboard() if display else None
        if clipboard is not None:
            clipboard.set(text)
            self.window.toast('Copied')


# ----- formatting helpers -----------------------------------------------------


def _build_group(title: str, rows: list[tuple[Optional[str], str]]) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title=title)
    for label, value in rows:
        if label is None:
            # Long-form text — render as a wrapping label, not a row.
            text = Gtk.Label(label=value, wrap=True, xalign=0.0)
            text.set_selectable(True)
            text.add_css_class('body')
            group.add(text)
            continue
        row = Adw.ActionRow(title=label, subtitle=value)
        row.add_css_class('property')
        group.add(row)
    return group


def _format_dimensions(w: Optional[int], h: Optional[int]) -> str:
    if not w or not h:
        return '—'
    mp = (w * h) / 1_000_000
    return f'{w} × {h}  ·  {mp:.1f} MP' if mp >= 0.05 else f'{w} × {h}'


def _format_size(b: Optional[int]) -> str:
    if not b:
        return '—'
    if b < 1024:
        return f'{b} B'
    units = ['KB', 'MB', 'GB', 'TB']
    val = float(b) / 1024
    for u in units:
        if val < 1024:
            return f'{val:.1f} {u}'
        val /= 1024
    return f'{val:.1f} PB'


def _format_date(taken_at: Optional[int]) -> str:
    if not taken_at:
        return '—'
    dt = datetime.fromtimestamp(taken_at)
    return dt.strftime('%A, %-d %B %Y · %H:%M')


def _format_camera(make: Optional[str], model: Optional[str]) -> str:
    if make and model:
        # Most camera models already include the brand; avoid "Canon Canon EOS R5"
        if model.lower().startswith(make.lower()):
            return model
        return f'{make} {model}'
    return make or model or '—'


def _format_settings(
    iso: Optional[int], f_number: Optional[float],
    exposure_time: Optional[float], focal_length: Optional[float],
) -> str:
    bits: list[str] = []
    if focal_length:
        bits.append(f'{focal_length:g} mm')
    if f_number:
        bits.append(f'ƒ/{f_number:g}')
    if exposure_time:
        bits.append(_format_exposure(exposure_time))
    if iso:
        bits.append(f'ISO {iso}')
    return '  ·  '.join(bits) if bits else '—'


def _format_exposure(seconds: float) -> str:
    if seconds >= 1:
        return f'{seconds:g}s'
    if seconds <= 0:
        return '—'
    # Round to a 1/N denominator so 0.005 → "1/200".
    denom = round(1 / seconds)
    return f'1/{denom}s'


def _format_coords(lat: Optional[float], lon: Optional[float]) -> str:
    if lat is None or lon is None:
        return '—'
    return f'{lat:.5f}, {lon:.5f}'
