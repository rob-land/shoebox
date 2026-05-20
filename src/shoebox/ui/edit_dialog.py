"""Edit metadata for a single photo: date, location, description."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from gi.repository import Adw, GLib, Gtk

from .. import exif_writer
from ..backends import BackendError
from ..database import Asset, Database
from ..worker import run_async

if TYPE_CHECKING:
    from ..window import ShoeboxWindow


log = logging.getLogger(__name__)


class EditMetadataDialog(Adw.Dialog):
    """Modal dialog with editable date, GPS, and description fields.

    Applies changes server-side via Backend.update_asset, then (if a
    local file exists and GExiv2 is available) writes EXIF/XMP tags
    into the file, then updates the catalog. Failures at any step toast
    out rather than silently leaving the user wondering.
    """

    __gtype_name__ = 'ShoeboxEditMetadataDialog'

    def __init__(
        self,
        window: ShoeboxWindow,
        asset: Asset,
        on_saved: Callable[[Asset], None] | None = None,
    ):
        super().__init__()
        self.set_title('Edit metadata')
        self.set_content_width(420)
        self.set_content_height(560)
        self.window = window
        self.asset = asset
        self._on_saved = on_saved

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        cancel = Gtk.Button(label='Cancel')
        cancel.connect('clicked', lambda *_: self.close())
        header.pack_start(cancel)

        self._save_btn = Gtk.Button(label='Save')
        self._save_btn.add_css_class('suggested-action')
        self._save_btn.connect('clicked', self._on_save)
        header.pack_end(self._save_btn)

        toolbar.add_top_bar(header)
        toolbar.set_content(self._build_body())
        self.set_child(toolbar)

    # ----- form -----

    def _build_body(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=400)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.set_margin_top(18)
        outer.set_margin_bottom(18)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        outer.append(self._build_date_group())
        outer.append(self._build_location_group())
        outer.append(self._build_description_group())
        outer.append(self._build_footer_hint())

        clamp.set_child(outer)
        scroller.set_child(clamp)
        return scroller

    def _build_date_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title='Date taken')

        dt = (datetime.fromtimestamp(self.asset.taken_at)
              if self.asset.taken_at else datetime.now())

        # Adw.SpinRow for hour/minute and Gtk.Calendar for the day.
        self._calendar = Gtk.Calendar()
        self._calendar.select_day(GLib.DateTime.new_local(
            dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
        ))
        cal_row = Adw.PreferencesRow(title='Date')
        cal_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        cal_box.set_margin_top(8)
        cal_box.set_margin_bottom(8)
        cal_box.set_margin_start(12)
        cal_box.set_margin_end(12)
        cal_label = Gtk.Label(label='Date', xalign=0.0)
        cal_label.add_css_class('caption-heading')
        cal_label.add_css_class('dim-label')
        cal_box.append(cal_label)
        cal_box.append(self._calendar)
        cal_row.set_child(cal_box)
        group.add(cal_row)

        self._hour_row = Adw.SpinRow.new_with_range(0, 23, 1)
        self._hour_row.set_title('Hour')
        self._hour_row.set_value(dt.hour)
        group.add(self._hour_row)

        self._minute_row = Adw.SpinRow.new_with_range(0, 59, 1)
        self._minute_row.set_title('Minute')
        self._minute_row.set_value(dt.minute)
        group.add(self._minute_row)

        return group

    def _build_location_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title='Location',
            description='Decimal degrees, e.g. 40.7128, -74.0060',
        )

        self._lat_row = Adw.EntryRow()
        self._lat_row.set_title('Latitude')
        if self.asset.latitude is not None:
            self._lat_row.set_text(f'{self.asset.latitude:.6f}')
        group.add(self._lat_row)

        self._lon_row = Adw.EntryRow()
        self._lon_row.set_title('Longitude')
        if self.asset.longitude is not None:
            self._lon_row.set_text(f'{self.asset.longitude:.6f}')
        group.add(self._lon_row)

        return group

    def _build_description_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title='Description')

        self._desc_view = Gtk.TextView()
        self._desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._desc_view.set_top_margin(6)
        self._desc_view.set_bottom_margin(6)
        self._desc_view.set_left_margin(6)
        self._desc_view.set_right_margin(6)
        if self.asset.description:
            self._desc_view.get_buffer().set_text(self.asset.description, -1)

        frame = Gtk.Frame()
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            min_content_height=120,
        )
        scroller.set_child(self._desc_view)
        frame.set_child(scroller)

        wrapper = Adw.PreferencesRow()
        wrapper.set_child(frame)
        wrapper.set_activatable(False)
        group.add(wrapper)
        return group

    def _build_footer_hint(self) -> Gtk.Widget:
        local = self.asset.local_path is not None
        gx = exif_writer.is_available()
        if local and gx:
            text = 'Changes are written to the file’s EXIF/XMP and pushed to the server.'
        elif local and not gx:
            text = 'GExiv2 is unavailable; the local file won’t be touched. Server is updated.'
        elif self.asset.remote_id and not local:
            text = 'Server only — no local copy to update.'
        else:
            text = 'No remote or local target; edits will only land in the catalog.'
        label = Gtk.Label(label=text, wrap=True, xalign=0.0)
        label.add_css_class('caption')
        label.add_css_class('dim-label')
        return label

    # ----- save -----

    def _on_save(self, _btn) -> None:
        edits = self._collect_edits()
        if edits is None:
            return  # validation toast already shown

        self._save_btn.set_sensitive(False)
        asset = self.asset
        window = self.window

        def work() -> str:
            return _apply_edits(window, asset, edits)

        def done(result: str) -> None:
            self._save_btn.set_sensitive(True)
            if result:
                window.toast(result)
            self.close()
            if self._on_saved is not None:
                # Reload the catalog row so callers see updated fields.
                db = window.app.db
                refreshed = next(
                    (a for a in db.list_assets(asset.account_id, limit=1, offset=0)
                     if a.id == asset.id),
                    None,
                )
                self._on_saved(refreshed or asset)

        def error(exc: BaseException) -> None:
            self._save_btn.set_sensitive(True)
            log.exception('edit save failed')
            window.toast(f'Save failed: {exc}')

        run_async(work, on_done=done, on_error=error)

    def _collect_edits(self) -> dict | None:
        """Return a dict of fields to apply, or None on validation failure."""
        edits: dict = {}

        # Date
        gdate = self._calendar.get_date()
        try:
            new_dt = datetime(
                gdate.get_year(), gdate.get_month(), gdate.get_day_of_month(),
                int(self._hour_row.get_value()),
                int(self._minute_row.get_value()),
            )
        except ValueError as e:
            self.window.toast(f'Invalid date: {e}')
            return None
        new_taken_at = int(new_dt.timestamp())
        if new_taken_at != (self.asset.taken_at or 0):
            edits['taken_at'] = new_taken_at

        # GPS — both must be set together or both cleared together.
        lat_text = self._lat_row.get_text().strip()
        lon_text = self._lon_row.get_text().strip()
        if lat_text or lon_text:
            try:
                lat = float(lat_text)
                lon = float(lon_text)
            except ValueError:
                self.window.toast('Latitude and longitude must both be decimal numbers')
                return None
            if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                self.window.toast('Coordinates out of range')
                return None
            if lat != (self.asset.latitude or 0) or lon != (self.asset.longitude or 0):
                edits['latitude'] = lat
                edits['longitude'] = lon
        # If both fields blank but the asset had coords: caller may want to
        # clear. Immich's API doesn't accept null here, so we leave clearing
        # for a future explicit "remove location" affordance.

        # Description
        buf = self._desc_view.get_buffer()
        start, end = buf.get_bounds()
        new_desc = buf.get_text(start, end, False)
        if new_desc != (self.asset.description or ''):
            edits['description'] = new_desc

        return edits


def _apply_edits(window: ShoeboxWindow, asset: Asset, edits: dict) -> str:
    """Push edits to server → local EXIF → catalog. Returns a status message."""
    if not edits:
        return 'No changes'

    pieces: list[str] = []

    # 1) Remote
    if asset.remote_id:
        backend = window.app.primary_backend()
        if backend is None:
            return 'No backend configured'
        try:
            backend.update_asset(asset.remote_id, **edits)
            pieces.append('server')
        except (BackendError, NotImplementedError) as e:
            log.warning('remote update failed for %s: %s', asset.remote_id, e)
            return f'Server update failed: {e}'

    # 2) Local EXIF (best-effort)
    if asset.local_path and exif_writer.is_available():
        wrote = exif_writer.write_metadata(asset.local_path, **edits)
        if wrote:
            pieces.append('EXIF')

    # 3) Catalog — always last so a crash mid-update leaves the source of
    # truth (server/file) authoritative.
    db = Database()
    try:
        db.update_asset_metadata(asset.id, **edits)
    finally:
        db.close()
    pieces.append('catalog')

    return 'Updated ' + ', '.join(pieces)
