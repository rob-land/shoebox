"""Bulk-edit non-date metadata (location, description, favorite) for many assets.

Distinct from BulkDateDialog because dates are inherently *per-asset*
(offsets/anchor) while these fields take a single value applied to every
selected photo. Empty fields mean "leave unchanged" — the dialog never
clobbers a field unless the user typed a value into it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from .. import exif_writer
from ..database import Asset, Database
from ..worker import run_async

if TYPE_CHECKING:
    from ..window import ShoeboxWindow


log = logging.getLogger(__name__)


_FAV_OPTIONS = ['Don’t change', 'Mark as favorite', 'Remove favorite']


class BulkEditDialog(Adw.Dialog):
    __gtype_name__ = 'ShoeboxBulkEditDialog'

    def __init__(
        self,
        window: ShoeboxWindow,
        assets: list[Asset],
        on_done: Callable[[], None] | None = None,
    ):
        super().__init__()
        self.window = window
        self._assets = list(assets)
        self._on_done = on_done

        self.set_title('Edit metadata')
        self.set_content_width(440)
        self.set_content_height(560)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        cancel = Gtk.Button(label='Cancel')
        cancel.connect('clicked', lambda *_: self.close())
        header.pack_start(cancel)

        self._apply_btn = Gtk.Button(label='Apply')
        self._apply_btn.add_css_class('suggested-action')
        self._apply_btn.connect('clicked', self._on_apply)
        header.pack_end(self._apply_btn)

        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._build_form(), 'form')
        self._stack.add_named(self._build_progress(), 'progress')
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

    def _build_form(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=420)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.set_margin_top(18)
        outer.set_margin_bottom(18)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        n = len(self._assets)
        summary = Gtk.Label(
            label=f'Editing {n} photo{"s" if n != 1 else ""}. Leave a field '
                  'empty to keep its current value.',
            xalign=0.0, wrap=True,
        )
        summary.add_css_class('dim-label')
        outer.append(summary)

        # Location
        loc_group = Adw.PreferencesGroup(
            title='Location',
            description='Both fields required to set; leave both empty to skip.',
        )
        self._lat_row = Adw.EntryRow()
        self._lat_row.set_title('Latitude')
        loc_group.add(self._lat_row)
        self._lon_row = Adw.EntryRow()
        self._lon_row.set_title('Longitude')
        loc_group.add(self._lon_row)
        outer.append(loc_group)

        # Description
        desc_group = Adw.PreferencesGroup(title='Description')
        self._desc_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._desc_view.set_top_margin(6)
        self._desc_view.set_bottom_margin(6)
        self._desc_view.set_left_margin(6)
        self._desc_view.set_right_margin(6)
        frame = Gtk.Frame()
        scroller2 = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            min_content_height=120,
        )
        scroller2.set_child(self._desc_view)
        frame.set_child(scroller2)
        desc_row = Adw.PreferencesRow(activatable=False)
        desc_row.set_child(frame)
        desc_group.add(desc_row)
        outer.append(desc_group)

        # Favorite tristate
        fav_group = Adw.PreferencesGroup(title='Favorite')
        self._fav_row = Adw.ComboRow()
        self._fav_row.set_title('Action')
        model = Gtk.StringList.new(_FAV_OPTIONS)
        self._fav_row.set_model(model)
        self._fav_row.set_selected(0)
        fav_group.add(self._fav_row)
        outer.append(fav_group)

        clamp.set_child(outer)
        scroller.set_child(clamp)
        return scroller

    def _build_progress(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        spinner = Adw.Spinner()
        spinner.set_size_request(48, 48)
        box.append(spinner)
        self._progress_label = Gtk.Label(label='Working…')
        box.append(self._progress_label)
        return box

    # ----- apply -----

    def _on_apply(self, _btn) -> None:
        try:
            edits = self._collect_edits()
        except ValueError as e:
            self.window.toast(str(e))
            return
        if not edits:
            self.window.toast('Nothing to apply')
            return

        self._stack.set_visible_child_name('progress')
        self._apply_btn.set_sensitive(False)

        assets = list(self._assets)
        window = self.window

        def work() -> tuple[int, int]:
            ok = 0
            failed = 0
            for asset in assets:
                try:
                    _apply_one(window, asset, edits)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    log.exception('bulk edit failed for asset %s', asset.id)
                    failed += 1
                GLib.idle_add(
                    self._progress_label.set_text,
                    f'Updated {ok + failed} of {len(assets)}…',
                )
            return ok, failed

        def done(result: tuple[int, int]) -> None:
            ok, failed = result
            msg = f'Updated {ok} photo{"s" if ok != 1 else ""}'
            if failed:
                msg += f', {failed} failed'
            window.toast(msg)
            self.close()
            if self._on_done is not None:
                self._on_done()

        def error(exc: BaseException) -> None:
            window.toast(f'Bulk update failed: {exc}')
            self.close()

        run_async(work, on_done=done, on_error=error)

    def _collect_edits(self) -> dict:
        edits: dict = {}

        # Location — both required if either set
        lat_text = self._lat_row.get_text().strip()
        lon_text = self._lon_row.get_text().strip()
        if lat_text or lon_text:
            if not lat_text or not lon_text:
                raise ValueError('Set both latitude and longitude, or neither')
            try:
                lat = float(lat_text)
                lon = float(lon_text)
            except ValueError:
                raise ValueError('Coordinates must be decimal numbers')
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                raise ValueError('Coordinates out of range')
            edits['latitude'] = lat
            edits['longitude'] = lon

        # Description — empty buffer means skip
        buf = self._desc_view.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, False)
        if text:
            edits['description'] = text

        # Favorite tristate
        fav_idx = self._fav_row.get_selected()
        if fav_idx == 1:
            edits['is_favorite'] = True
        elif fav_idx == 2:
            edits['is_favorite'] = False

        return edits


def _apply_one(window: ShoeboxWindow, asset: Asset, edits: dict) -> None:
    if asset.remote_id:
        backend = window.app.primary_backend()
        if backend is None:
            raise RuntimeError('No backend configured')
        backend.update_asset(asset.remote_id, **edits)

    # Local EXIF only takes the fields it understands; favorite isn't a
    # standard EXIF concept so we filter it out for the local write.
    local_edits = {k: v for k, v in edits.items() if k != 'is_favorite'}
    if asset.local_path and local_edits and exif_writer.is_available():
        exif_writer.write_metadata(asset.local_path, **local_edits)

    db = Database()
    try:
        db.update_asset_metadata(asset.id, **edits)
    finally:
        db.close()
