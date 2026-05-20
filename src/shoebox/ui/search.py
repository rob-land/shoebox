"""Smart-search page: natural-language query → backend-ranked thumbnails.

Currently backed by Immich's /search/smart (CLIP). Backends that don't
implement search_smart simply never expose the entry point — the gallery
header hides its search button when the active backend doesn't advertise
the capability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gi.repository import Adw, Gio, Gtk

from ..backends import Backend
from ..backends.base import RemoteAsset
from ..database import Asset
from ..worker import run_async
from .widgets import AssetItem, ThumbnailTile

if TYPE_CHECKING:
    from ..window import ShoeboxWindow


_RESULT_LIMIT = 100


@Gtk.Template(resource_path='/land/rob/shoebox/ui/search.ui')
class SearchPage(Adw.NavigationPage):
    __gtype_name__ = 'ShoeboxSearchPage'

    search_entry:    Gtk.SearchEntry = Gtk.Template.Child()
    stack:           Gtk.Stack       = Gtk.Template.Child()
    results_grid:    Gtk.GridView    = Gtk.Template.Child()
    results_caption: Gtk.Label       = Gtk.Template.Child()
    error_status:    Adw.StatusPage  = Gtk.Template.Child()

    def __init__(self, window: ShoeboxWindow):
        super().__init__()
        self.window = window

        self._store: Gio.ListStore = Gio.ListStore.new(AssetItem)
        # Monotonic counter so a slow in-flight search whose result lands
        # after the user has typed a newer query gets discarded.
        self._query_seq: int = 0

        factory = Gtk.SignalListItemFactory()
        factory.connect('setup', self._factory_setup)
        factory.connect('bind', self._factory_bind)
        self.results_grid.set_factory(factory)
        self.results_grid.set_model(Gtk.NoSelection.new(self._store))
        self.results_grid.connect('activate', self._on_activate)

        self._apply_columns()
        self.window.connect('notify::compact', lambda *_: self._apply_columns())

        self.stack.set_visible_child_name('prompt')
        self.connect('shown', lambda *_: self.search_entry.grab_focus())

    # ----- factory -----

    def _factory_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        list_item.set_child(ThumbnailTile())

    def _factory_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        item: AssetItem = list_item.get_item()
        tile: ThumbnailTile = list_item.get_child()
        size = self.window.app.settings.get_int('thumbnail-size')
        tile.bind(item.asset, size, self._backend())

    def _apply_columns(self) -> None:
        if self.window.compact:
            self.results_grid.set_min_columns(2)
            self.results_grid.set_max_columns(3)
        else:
            self.results_grid.set_min_columns(3)
            self.results_grid.set_max_columns(8)

    # ----- search entry -----

    @Gtk.Template.Callback()
    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        # SearchEntry already debounces via its `search-delay` property, so
        # by the time we land here the user has paused typing.
        self._run_query(entry.get_text())

    @Gtk.Template.Callback()
    def _on_search_activate(self, entry: Gtk.SearchEntry) -> None:
        # Enter bypasses the debounce — fire immediately.
        self._run_query(entry.get_text())

    def _run_query(self, raw: str) -> None:
        query = raw.strip()
        if not query:
            self._store.remove_all()
            self.stack.set_visible_child_name('prompt')
            return

        backend = self._backend()
        account = self.window.app.primary_account()
        if backend is None or account is None:
            self._show_error('No account configured')
            return

        self._query_seq += 1
        seq = self._query_seq
        self.stack.set_visible_child_name('loading')

        def work() -> list[RemoteAsset]:
            return backend.search_smart(query, limit=_RESULT_LIMIT)

        run_async(
            work,
            on_done=lambda hits: self._on_results(seq, hits),
            on_error=lambda exc: self._on_error(seq, exc),
        )

    def _on_results(self, seq: int, hits: list[RemoteAsset]) -> None:
        if seq != self._query_seq:
            return
        account = self.window.app.primary_account()
        if account is None:
            return

        remote_ids = [h.remote_id for h in hits if h.remote_id]
        assets: list[Asset] = self.window.app.db.list_assets_by_remote_ids(
            account.id, remote_ids,
        )

        self._store.remove_all()
        for asset in assets:
            self._store.append(AssetItem(asset))

        if not assets:
            self.stack.set_visible_child_name('empty')
            return

        # When the server returns hits we couldn't map to local rows it
        # means the catalog hasn't fully caught up — surface that so the
        # result count makes sense to the user.
        missing = len(remote_ids) - len(assets)
        if missing > 0:
            self.results_caption.set_text(
                f'{len(assets)} results · {missing} not yet synced'
            )
        else:
            self.results_caption.set_text(f'{len(assets)} results')
        self.stack.set_visible_child_name('results')

    def _on_error(self, seq: int, exc: BaseException) -> None:
        if seq != self._query_seq:
            return
        if isinstance(exc, NotImplementedError):
            self._show_error('This backend does not support search')
        else:
            self._show_error(str(exc))

    def _show_error(self, message: str) -> None:
        self.error_status.set_description(message)
        self.stack.set_visible_child_name('error')

    # ----- navigation -----

    def _on_activate(self, _grid, position: int) -> None:
        item: AssetItem = self._store.get_item(position)
        if item is None:
            return
        from .detail import DetailPage
        self.window.push(DetailPage(self.window, item.asset))

    # ----- backend -----

    def _backend(self) -> Backend | None:
        return self.window.app.primary_backend()
