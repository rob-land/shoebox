"""Adaptive sectioned timeline gallery.

Photos are grouped into per-day sections sorted newest-first. The view
loads the latest batch from the catalog on entry; scrolling to the bottom
pulls more rows from the catalog and, once exhausted, fetches the next
page from the server in the background.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Iterable, Optional

from gi.repository import Adw, Gio, GLib, Gtk

from ..backends import Backend
from ..database import Asset
from .widgets import AssetItem, ThumbnailTile

if TYPE_CHECKING:
    from ..window import ShoeboxWindow


_DB_BATCH = 200          # rows to read from the catalog per scroll fetch
_SERVER_PAGE_SIZE = 200  # rows to fetch from the server per scroll fetch
_UNDATED_KEY = '0000-00-00'


def _day_key(taken_at: Optional[int]) -> tuple[str, str]:
    """Return a (sort-key, display-title) pair for a photo's day section.

    Undated photos sort to the very end via the _UNDATED_KEY sentinel.
    """
    if taken_at is None or taken_at <= 0:
        return _UNDATED_KEY, 'Undated'
    dt = datetime.fromtimestamp(taken_at)
    return dt.strftime('%Y-%m-%d'), _format_day(dt)


def _format_day(dt: datetime) -> str:
    today = date.today()
    d = dt.date()
    delta = (today - d).days
    if delta == 0:
        return 'Today'
    if delta == 1:
        return 'Yesterday'
    if d.year == today.year:
        return dt.strftime('%A, %-d %B')
    return dt.strftime('%A, %-d %B %Y')


def _scrubber_title(section_key: str, fallback: str) -> str:
    """Coarser label for the scrubber pill — month, not day.

    Keeps the pill from flickering through every day as the user
    scrolls; jumping a single month is the right granularity for "where
    am I in time."
    """
    if section_key == _UNDATED_KEY:
        return 'Undated'
    try:
        dt = datetime.strptime(section_key, '%Y-%m-%d')
    except ValueError:
        return fallback
    if dt.year == date.today().year:
        return dt.strftime('%B')
    return dt.strftime('%B %Y')


# ----- per-day section --------------------------------------------------------


class _Section:
    """One day's worth of assets: header label + GridView."""

    def __init__(
        self,
        page: 'GalleryPage',
        key: str,
        title: str,
    ):
        self.key = key
        self.title = title
        self._page = page
        self.store: Gio.ListStore = Gio.ListStore.new(AssetItem)

        self.container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.container.set_margin_start(12)
        self.container.set_margin_end(12)
        self.container.set_margin_top(12)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_row.set_margin_bottom(2)

        self.header = Gtk.Label(label=title)
        self.header.add_css_class('heading')
        self.header.set_halign(Gtk.Align.START)
        self.header.set_hexpand(True)
        header_row.append(self.header)

        self.select_all_button = Gtk.Button(
            icon_name='checkbox-symbolic',
            tooltip_text='Select all in this day',
        )
        self.select_all_button.add_css_class('flat')
        self.select_all_button.set_visible(False)
        self.select_all_button.connect(
            'clicked', lambda *_: self._page._select_all_in_section(self),
        )
        header_row.append(self.select_all_button)

        self.container.append(header_row)

        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', page._factory_setup)
        factory.connect('bind', page._factory_bind)
        factory.connect('unbind', page._factory_unbind)

        selection = Gtk.NoSelection.new(self.store)
        self.grid = Gtk.GridView.new(selection, factory)
        self.grid.set_hexpand(True)
        self.grid.set_vexpand(False)
        page._apply_columns(self.grid)
        self.grid.connect('activate', self._on_activate)
        self.container.append(self.grid)

    def append_assets(self, assets: Iterable[Asset]) -> None:
        for a in assets:
            self.store.append(AssetItem(a))

    def set_selection_mode(self, on: bool) -> None:
        self.select_all_button.set_visible(on)

    def _on_activate(self, _grid, position: int) -> None:
        item: AssetItem = self.store.get_item(position)
        if item is None:
            return
        if self._page._selection_mode:
            self._page._toggle_selection(item.asset)
        else:
            self._page._open_detail(item.asset)


# ----- the page itself --------------------------------------------------------


@Gtk.Template(resource_path='/land/rob/shoebox/ui/gallery.ui')
class GalleryPage(Adw.NavigationPage):
    __gtype_name__ = 'ShoeboxGalleryPage'

    status_label:         Gtk.Label         = Gtk.Template.Child()
    stack:                Gtk.Stack         = Gtk.Template.Child()
    scroller:             Gtk.ScrolledWindow = Gtk.Template.Child()
    sections_box:         Gtk.Box           = Gtk.Template.Child()
    bottom_indicator:     Gtk.Box           = Gtk.Template.Child()
    date_pill:            Gtk.Label         = Gtk.Template.Child()
    browse_header:        Adw.HeaderBar     = Gtk.Template.Child()
    select_header:        Adw.HeaderBar     = Gtk.Template.Child()
    count_label:          Gtk.Label         = Gtk.Template.Child()
    adjust_dates_button:  Gtk.Button        = Gtk.Template.Child()
    edit_metadata_button: Gtk.Button        = Gtk.Template.Child()

    def __init__(self, window: 'ShoeboxWindow'):
        super().__init__()
        self.window = window

        self._sections: dict[str, _Section] = {}
        self._db_offset: int = 0
        self._next_server_page: int = 2  # page 1 is fetched by SyncManager.run()
        self._has_more_in_db: bool = True
        self._has_more_on_server: bool = True
        self._loading_more: bool = False
        self._sync_manager = None
        self._pill_hide_id: Optional[int] = None
        self._last_pill_text: str = ''
        self._selection_mode: bool = False
        self._selected_ids: set[int] = set()

        self.stack.set_visible_child_name('empty')

        self.scroller.connect('edge-reached', self._on_edge_reached)
        # Prefetch slightly before the very bottom for smoother scrolling.
        self.scroller.get_vadjustment().connect(
            'value-changed', self._on_vadjustment_changed,
        )

        self.window.connect('notify::compact', lambda *_: self._sync_columns())

        self.connect('shown', lambda *_: self._first_load())

    # ----- title / status -----

    def _set_status(self, text: str) -> None:
        self.status_label.set_text(text)

    @Gtk.Template.Callback()
    def _on_sync_clicked(self, *_args) -> None:
        self.request_sync()

    # ----- thumbnail factory (shared across sections) -----

    def _factory_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        tile = ThumbnailTile()
        tile.set_select_callback(self._on_tile_select_gesture)
        list_item.set_child(tile)

    def _on_tile_select_gesture(self, asset: Asset) -> None:
        """Long-press or Ctrl-click on a tile: enter selection mode + toggle."""
        if not self._selection_mode:
            self._set_selection_mode(True)
        self._toggle_selection(asset)

    def _factory_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        item: AssetItem = list_item.get_item()
        tile: ThumbnailTile = list_item.get_child()
        size = self.window.app.settings.get_int('thumbnail-size')
        tile.bind(
            item.asset, size, self._backend(),
            selected=item.asset.id in self._selected_ids,
            show_check=self._selection_mode,
        )

    def _factory_unbind(self, _factory, list_item: Gtk.ListItem) -> None:
        pass

    # ----- selection mode -----

    @Gtk.Template.Callback()
    def _on_select_mode_clicked(self, *_args) -> None:
        self._set_selection_mode(True)

    @Gtk.Template.Callback()
    def _on_select_cancel(self, *_args) -> None:
        self._set_selection_mode(False)

    @Gtk.Template.Callback()
    def _on_adjust_dates_clicked(self, *_args) -> None:
        if not self._selected_ids:
            return
        from .bulk_date_dialog import BulkDateDialog
        dialog = BulkDateDialog(
            self.window,
            self._selected_assets(),
            on_done=self._on_bulk_done,
        )
        dialog.present(self.window)

    @Gtk.Template.Callback()
    def _on_bulk_edit_clicked(self, *_args) -> None:
        if not self._selected_ids:
            return
        from .bulk_edit_dialog import BulkEditDialog
        dialog = BulkEditDialog(
            self.window,
            self._selected_assets(),
            on_done=self._on_bulk_done,
        )
        dialog.present(self.window)

    def _set_selection_mode(self, on: bool) -> None:
        self._selection_mode = on
        self.browse_header.set_visible(not on)
        self.select_header.set_visible(on)
        if not on:
            self._selected_ids.clear()
        self._refresh_section_selection_buttons()
        self._refresh_visible_tiles()
        self._update_selection_count()

    def _toggle_selection(self, asset: Asset) -> None:
        if asset.id in self._selected_ids:
            self._selected_ids.remove(asset.id)
        else:
            self._selected_ids.add(asset.id)
        self._refresh_visible_tiles()
        self._update_selection_count()

    def _select_all_in_section(self, section: '_Section') -> None:
        ids = {section.store.get_item(i).asset.id
               for i in range(section.store.get_n_items())}
        # Toggle: if every item is already selected, deselect them; else select.
        if ids.issubset(self._selected_ids):
            self._selected_ids -= ids
        else:
            self._selected_ids |= ids
        self._refresh_visible_tiles()
        self._update_selection_count()

    def _selected_assets(self) -> list[Asset]:
        out: list[Asset] = []
        for section in self._sections.values():
            for i in range(section.store.get_n_items()):
                item = section.store.get_item(i)
                if item.asset.id in self._selected_ids:
                    out.append(item.asset)
        return out

    def _update_selection_count(self) -> None:
        n = len(self._selected_ids)
        if n == 0:
            self.count_label.set_text('Select photos')
        elif n == 1:
            self.count_label.set_text('1 selected')
        else:
            self.count_label.set_text(f'{n} selected')
        self.adjust_dates_button.set_sensitive(n > 0)
        self.edit_metadata_button.set_sensitive(n > 0)

    def _refresh_section_selection_buttons(self) -> None:
        for section in self._sections.values():
            section.set_selection_mode(self._selection_mode)

    def _refresh_visible_tiles(self) -> None:
        # Rebinding via items-changed forces the GridView factory to re-run
        # bind on every visible row. Cheap: ListView only re-binds widgets
        # actually attached to the viewport.
        for section in self._sections.values():
            n = section.store.get_n_items()
            if n > 0:
                section.store.items_changed(0, n, n)

    def _on_bulk_done(self) -> None:
        self._set_selection_mode(False)
        self._refresh_from_db()

    # ----- column adaptation -----

    def _apply_columns(self, grid: Gtk.GridView) -> None:
        if self.window.compact:
            grid.set_min_columns(2)
            grid.set_max_columns(3)
        else:
            grid.set_min_columns(3)
            grid.set_max_columns(8)

    def _sync_columns(self) -> None:
        for section in self._sections.values():
            self._apply_columns(section.grid)

    # ----- backend / sync glue -----

    def _backend(self) -> Optional[Backend]:
        return self.window.app.primary_backend()

    def _sync(self):
        if self._sync_manager is None:
            from ..sync.manager import SyncManager
            account = self.window.app.primary_account()
            backend = self._backend()
            if not account or not backend:
                return None
            self._sync_manager = SyncManager(
                self.window.app, account, backend,
                on_progress=self._set_status,
                on_complete=self._refresh_from_db,
                on_error=lambda e: self.window.toast(f'Sync failed: {e}'),
            )
        return self._sync_manager

    def request_sync(self) -> None:
        sm = self._sync()
        if sm is None:
            self.window.toast('No account configured')
            return
        sm.run()

    # ----- initial load -----

    def _first_load(self) -> None:
        self._refresh_from_db()
        account = self.window.app.primary_account()
        if account is None:
            return
        # If the catalog is empty for this account, kick off a sync immediately.
        total, _, _ = self.window.app.db.asset_count(account.id)
        if total == 0:
            self.request_sync()

    def _refresh_from_db(self) -> None:
        """Reset the timeline and load the latest batch from the catalog."""
        account = self.window.app.primary_account()
        if account is None:
            return

        # Tear down existing sections.
        self._sections.clear()
        child = self.sections_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.sections_box.remove(child)
            child = nxt

        # Reset cursors.
        self._db_offset = 0
        self._has_more_in_db = True
        self._has_more_on_server = True
        self._next_server_page = 2

        assets = self.window.app.db.list_assets(
            account.id, limit=_DB_BATCH, offset=0,
        )
        if assets:
            self._merge_assets(assets)
            self._db_offset = len(assets)
            if len(assets) < _DB_BATCH:
                self._has_more_in_db = False
            self.stack.set_visible_child_name('timeline')
        else:
            self.stack.set_visible_child_name('empty')

        self._update_status()

    def _update_status(self) -> None:
        account = self.window.app.primary_account()
        if account is None:
            return
        total, local_only, pending = self.window.app.db.asset_count(account.id)
        bits = [f'{total} photos']
        if local_only:
            bits.append(f'{local_only} local-only')
        if pending:
            bits.append(f'{pending} pending')
        self._set_status(' · '.join(bits))

    # ----- merge assets into sections -----

    def _merge_assets(self, assets: Iterable[Asset]) -> None:
        # Group by (key, title). Inputs are already sorted DESC by taken_at.
        grouped: dict[str, tuple[str, list[Asset]]] = {}
        for asset in assets:
            key, title = _day_key(asset.taken_at)
            grouped.setdefault(key, (title, []))[1].append(asset)

        # Iterate in DESC order so insertions land in the right places.
        for key in sorted(grouped.keys(), reverse=True):
            title, items = grouped[key]
            section = self._sections.get(key)
            if section is None:
                section = self._create_section(key, title)
            section.append_assets(items)

    def _create_section(self, key: str, title: str) -> _Section:
        section = _Section(self, key, title)
        self._sections[key] = section

        # Insert into sections_box in date-descending order.
        # 'Undated' (key '0000-00') sorts to the end naturally.
        keys_sorted = sorted(self._sections.keys(), reverse=True)
        idx = keys_sorted.index(key)
        if idx == 0:
            self.sections_box.prepend(section.container)
        else:
            prev_key = keys_sorted[idx - 1]
            prev_section = self._sections[prev_key]
            self.sections_box.insert_child_after(
                section.container, prev_section.container,
            )
        return section

    # ----- scroll / load-more -----

    def _on_edge_reached(self, _sw, position: Gtk.PositionType) -> None:
        if position == Gtk.PositionType.BOTTOM:
            self._maybe_load_more()

    def _on_vadjustment_changed(self, adj) -> None:
        # Prefetch when within one page of the bottom.
        upper = adj.get_upper()
        page = adj.get_page_size()
        value = adj.get_value()
        if upper > 0 and (value + page * 2) >= upper:
            self._maybe_load_more()
        self._update_date_pill(value)

    # ----- date scrubber pill -----

    def _update_date_pill(self, scroll_y: float) -> None:
        title = self._title_at_scroll_y(scroll_y)
        if not title:
            return
        if title != self._last_pill_text:
            self.date_pill.set_label(title)
            self._last_pill_text = title
        self.date_pill.add_css_class('visible')
        if self._pill_hide_id is not None:
            GLib.source_remove(self._pill_hide_id)
        self._pill_hide_id = GLib.timeout_add(
            800, self._hide_date_pill,
        )

    def _hide_date_pill(self) -> bool:
        self.date_pill.remove_css_class('visible')
        self._pill_hide_id = None
        return False

    def _title_at_scroll_y(self, scroll_y: float) -> str:
        """Return the month-and-year of the topmost visible section."""
        # Sections are appended into sections_box in newest-first order, so
        # walking that order and picking the first whose container bottom is
        # past the scroll cursor gives us the section currently at the top of
        # the viewport.
        for key in sorted(self._sections.keys(), reverse=True):
            section = self._sections[key]
            alloc = section.container.get_allocation()
            if alloc.y + alloc.height > scroll_y:
                return _scrubber_title(key, section.title)
        return ''

    def _maybe_load_more(self) -> None:
        if self._loading_more:
            return
        if not self._has_more_in_db and not self._has_more_on_server:
            self.bottom_indicator.set_visible(False)
            return
        self._loading_more = True
        self.bottom_indicator.set_visible(True)
        # Defer to next idle so the scrolled-window finishes its pass first.
        GLib.idle_add(self._load_more_step)

    def _load_more_step(self) -> bool:
        account = self.window.app.primary_account()
        if account is None:
            self._loading_more = False
            self.bottom_indicator.set_visible(False)
            return False

        if self._has_more_in_db:
            new_assets = self.window.app.db.list_assets(
                account.id, limit=_DB_BATCH, offset=self._db_offset,
            )
            if new_assets:
                self._merge_assets(new_assets)
                self._db_offset += len(new_assets)
                if len(new_assets) < _DB_BATCH:
                    self._has_more_in_db = False
                self._loading_more = False
                self.bottom_indicator.set_visible(False)
                self._update_status()
                return False
            self._has_more_in_db = False

        if not self._has_more_on_server:
            self._loading_more = False
            self.bottom_indicator.set_visible(False)
            return False

        sm = self._sync()
        if sm is None:
            self._loading_more = False
            self.bottom_indicator.set_visible(False)
            return False

        sm.fetch_more_remote(
            page=self._next_server_page,
            size=_SERVER_PAGE_SIZE,
            on_complete=self._on_server_fetched,
        )
        return False

    def _on_server_fetched(self, has_more: bool, count: int) -> None:
        self._next_server_page += 1
        if not has_more:
            self._has_more_on_server = False
        if count > 0:
            self._has_more_in_db = True
        self._loading_more = False
        if count > 0:
            # Pull the just-stored rows into the timeline.
            self._maybe_load_more()
        else:
            self.bottom_indicator.set_visible(False)
            self._update_status()

    # ----- detail navigation -----

    def _open_detail(self, asset: Asset) -> None:
        from .detail import DetailPage
        self.window.push(DetailPage(self.window, asset))
