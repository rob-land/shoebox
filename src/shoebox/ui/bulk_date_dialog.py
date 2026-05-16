"""Bulk-edit photo dates for a selected group of assets.

Two modes:

* **Offset** — shift every selected photo's timestamp by the same
  signed delta (days/hours/minutes). For "all my photos from the road
  trip are off by 6 hours because the camera was in the wrong timezone."
* **Anchor first** — set the first (oldest-id) photo to a chosen
  date/time and slide everything else by the same delta. For scanned
  prints where the relative ordering inside the batch is right but the
  absolute date isn't.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from gi.repository import Adw, GLib, GObject, Gtk

from .. import exif_writer
from ..backends import BackendError
from ..database import Asset, Database
from ..worker import run_async

if TYPE_CHECKING:
    from ..window import ShoeboxWindow


log = logging.getLogger(__name__)


class BulkDateDialog(Adw.Dialog):
    __gtype_name__ = 'ShoeboxBulkDateDialog'

    def __init__(
        self,
        window: 'ShoeboxWindow',
        assets: list[Asset],
        on_done: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        # Sort by current taken_at so "anchor first" picks the earliest.
        self._assets = sorted(
            [a for a in assets if a.taken_at],
            key=lambda a: a.taken_at,
        )
        # Assets with no date land at the end; anchor mode shifts them
        # by the same delta as the rest.
        self._undated = [a for a in assets if not a.taken_at]
        self.window = window
        self._on_done = on_done

        self.set_title('Adjust dates')
        self.set_content_width(440)
        self.set_content_height(520)

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

    # ----- form -----

    def _build_form(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=420)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.set_margin_top(18)
        outer.set_margin_bottom(18)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        # Summary
        n = len(self._assets) + len(self._undated)
        summary = Gtk.Label(
            label=f'Adjusting {n} photo{"s" if n != 1 else ""}.',
            xalign=0.0, wrap=True,
        )
        summary.add_css_class('dim-label')
        outer.append(summary)

        # Mode selector
        mode_group = Adw.PreferencesGroup(title='Mode')
        self._mode_offset = Gtk.CheckButton(label='Shift by an offset')
        self._mode_offset.set_active(True)
        self._mode_offset.connect('toggled', self._on_mode_toggled)

        self._mode_anchor = Gtk.CheckButton(label='Set first photo to a date, shift the rest')
        self._mode_anchor.set_group(self._mode_offset)
        self._mode_anchor.connect('toggled', self._on_mode_toggled)

        if not self._assets:
            self._mode_anchor.set_sensitive(False)
            self._mode_anchor.set_tooltip_text('No dated photos to anchor on')

        mode_row1 = Adw.PreferencesRow(activatable=False)
        mode_row1.set_child(self._padded(self._mode_offset))
        mode_group.add(mode_row1)

        mode_row2 = Adw.PreferencesRow(activatable=False)
        mode_row2.set_child(self._padded(self._mode_anchor))
        mode_group.add(mode_row2)
        outer.append(mode_group)

        # Offset controls
        self._offset_group = Adw.PreferencesGroup(
            title='Offset',
            description='Negative values shift earlier in time.',
        )
        self._days_row = Adw.SpinRow.new_with_range(-36525, 36525, 1)
        self._days_row.set_title('Days')
        self._offset_group.add(self._days_row)
        self._hours_row = Adw.SpinRow.new_with_range(-23, 23, 1)
        self._hours_row.set_title('Hours')
        self._offset_group.add(self._hours_row)
        self._minutes_row = Adw.SpinRow.new_with_range(-59, 59, 1)
        self._minutes_row.set_title('Minutes')
        self._offset_group.add(self._minutes_row)
        outer.append(self._offset_group)

        # Anchor controls
        self._anchor_group = Adw.PreferencesGroup(
            title='First photo',
            description='The first photo (by current date) becomes this.',
        )
        anchor_row = Adw.PreferencesRow(title='Anchor')
        cal_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        cal_box.set_margin_top(8)
        cal_box.set_margin_bottom(8)
        cal_box.set_margin_start(12)
        cal_box.set_margin_end(12)
        self._anchor_calendar = Gtk.Calendar()
        if self._assets:
            first = datetime.fromtimestamp(self._assets[0].taken_at)
            self._anchor_calendar.select_day(GLib.DateTime.new_local(
                first.year, first.month, first.day, first.hour, first.minute, 0,
            ))
        cal_box.append(self._anchor_calendar)
        anchor_row.set_child(cal_box)
        self._anchor_group.add(anchor_row)

        self._anchor_hour = Adw.SpinRow.new_with_range(0, 23, 1)
        self._anchor_hour.set_title('Hour')
        self._anchor_group.add(self._anchor_hour)
        self._anchor_minute = Adw.SpinRow.new_with_range(0, 59, 1)
        self._anchor_minute.set_title('Minute')
        self._anchor_group.add(self._anchor_minute)
        if self._assets:
            first = datetime.fromtimestamp(self._assets[0].taken_at)
            self._anchor_hour.set_value(first.hour)
            self._anchor_minute.set_value(first.minute)

        self._anchor_group.set_visible(False)
        outer.append(self._anchor_group)

        # Apply scope
        scope_group = Adw.PreferencesGroup(
            description=(
                'Changes are pushed to the server and written to local EXIF '
                'when available. There is no undo — make a backup if this is a '
                'large batch.'
            ),
        )
        outer.append(scope_group)

        clamp.set_child(outer)
        scroller.set_child(clamp)
        return scroller

    def _padded(self, w: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.append(w)
        return box

    def _on_mode_toggled(self, _btn) -> None:
        offset_active = self._mode_offset.get_active()
        self._offset_group.set_visible(offset_active)
        self._anchor_group.set_visible(not offset_active)

    # ----- progress -----

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
            delta_seconds = self._compute_delta()
        except ValueError as e:
            self.window.toast(str(e))
            return
        if delta_seconds == 0:
            self.window.toast('No change to apply')
            return

        self._stack.set_visible_child_name('progress')
        self._apply_btn.set_sensitive(False)

        assets = list(self._assets) + list(self._undated)
        window = self.window

        def work() -> tuple[int, int]:
            ok = 0
            failed = 0
            for asset in assets:
                if asset.taken_at is None:
                    # Skip undated assets in offset/anchor — there's no
                    # base time to add the delta to. They remain undated.
                    continue
                new_taken = asset.taken_at + delta_seconds
                edits = {'taken_at': new_taken}
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

    def _compute_delta(self) -> int:
        if self._mode_offset.get_active():
            days = int(self._days_row.get_value())
            hours = int(self._hours_row.get_value())
            minutes = int(self._minutes_row.get_value())
            return (days * 86400) + (hours * 3600) + (minutes * 60)

        if not self._assets:
            raise ValueError('No dated photos to anchor on')
        first = self._assets[0]
        gdate = self._anchor_calendar.get_date()
        new_first = datetime(
            gdate.get_year(), gdate.get_month(), gdate.get_day_of_month(),
            int(self._anchor_hour.get_value()),
            int(self._anchor_minute.get_value()),
        )
        return int(new_first.timestamp()) - first.taken_at


def _apply_one(window: 'ShoeboxWindow', asset: Asset, edits: dict) -> None:
    """Push edits for a single asset. Raises on irrecoverable failure."""
    if asset.remote_id:
        backend = window.app.primary_backend()
        if backend is None:
            raise RuntimeError('No backend configured')
        backend.update_asset(asset.remote_id, **edits)

    if asset.local_path and exif_writer.is_available():
        exif_writer.write_metadata(asset.local_path, **edits)

    db = Database()
    try:
        db.update_asset_metadata(asset.id, **edits)
    finally:
        db.close()
