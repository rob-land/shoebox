"""Orchestrates pull-from-server, local scan, and upload."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from gi.repository import GLib

from ..backends import Backend, BackendError, SyncResetRequired
from ..database import Account, Database
from ..worker import run_async
from . import conditions, scanner

if TYPE_CHECKING:
    from ..application import ShoeboxApplication


ProgressFn = Callable[[str], None]


INITIAL_SERVER_PAGE_SIZE = 200
SCROLL_SERVER_PAGE_SIZE = 200

# account_state key: '1' once a change-feed checkpoint is established, so
# later runs pull deltas instead of resetting to a full feed.
_FEED_READY_KEY = 'change-feed-ready'
_FEED_PROGRESS_EVERY = 500
# A feed response can be cut before the completion sentinel (proxy write
# timeouts on big catalogs); progress is checkpointed, so re-pull until
# complete, with a cap against a server that never sends the sentinel.
_FEED_MAX_ROUNDS = 50


class SyncManager:
    """Single-account sync orchestrator.

    .run() does a refresh pass:
      1. pulls server changes from the backend's change feed (upserts,
         metadata edits, deletions since the last checkpoint; the first
         run streams the full catalog) — or, for backends without a
         feed, refreshes the most recent server page
      2. scans local sync dirs into the catalog
      3. uploads pending local-only assets (gated by Wi-Fi / charging)

    .fetch_more_remote(page) is used by the gallery on scroll to pull older
    pages from the server on demand. It does not run the local scan or
    uploads, and is not gated by sync conditions.
    """

    def __init__(
        self,
        app: ShoeboxApplication,
        account: Account,
        backend: Backend,
        *,
        on_progress: ProgressFn | None = None,
        on_complete: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self.app = app
        self.account = account
        self.backend = backend
        self.on_progress = on_progress or (lambda _msg: None)
        self.on_complete = on_complete or (lambda: None)
        self.on_error = on_error or (lambda _msg: None)
        self._running = False
        self._fetching_more = False

    def run(self) -> None:
        if self._running:
            return
        self._running = True

        settings = self.app.settings
        network_pref = settings.get_string('sync-network')
        charging_only = settings.get_boolean('sync-charging-only')

        def _progress(msg: str) -> None:
            GLib.idle_add(self._emit_progress, msg)

        def work() -> None:
            try:
                self._pull_remote(_progress)
                self._scan_local(_progress)
                allowed, reason = conditions.should_sync(
                    network_pref=network_pref,
                    charging_only=charging_only,
                )
                if allowed:
                    self._upload_pending(_progress)
                else:
                    _progress(reason)
            finally:
                pass

        def done(_):
            self._running = False
            self.on_complete()

        def error(exc):
            self._running = False
            msg = exc.args[0] if isinstance(exc, BackendError) and exc.args else str(exc)
            self.on_error(msg)

        run_async(work, on_done=done, on_error=error)

    def _emit_progress(self, msg: str) -> bool:
        self.on_progress(msg)
        return False

    # ----- steps -----

    def _pull_remote(self, progress: ProgressFn) -> None:
        db = Database()
        try:
            try:
                self._pull_change_feed(db, progress)
            except NotImplementedError:
                self._pull_latest_page(db, progress)
        finally:
            db.close()

    def _pull_latest_page(self, db: Database, progress: ProgressFn) -> None:
        """Fallback for backends without a change feed: refresh the newest
        page. Older cached rows can go stale until scrolled back into."""
        progress('Fetching latest photos…')
        items, _has_more = self.backend.fetch_page(
            page=1, size=INITIAL_SERVER_PAGE_SIZE,
        )
        for asset in items:
            self._store_remote(db, asset)
        progress(f'Fetched latest {len(items)} from server')

    def _pull_change_feed(self, db: Database, progress: ProgressFn) -> None:
        ready = db.get_account_state(self.account.id, _FEED_READY_KEY) == '1'
        try:
            self._apply_change_feed(db, progress, reset=not ready)
        except SyncResetRequired:
            progress('Server requested a full resync…')
            self._apply_change_feed(db, progress, reset=True)
        db.set_account_state(self.account.id, _FEED_READY_KEY, '1')

    def _apply_change_feed(
        self, db: Database, progress: ProgressFn, *, reset: bool,
    ) -> None:
        progress('Downloading catalog from server…' if reset
                 else 'Syncing changes from server…')
        # On a full (reset) feed we see every live asset, so anything the
        # catalog has that the feed doesn't mention was deleted meanwhile.
        seen: set[str] | None = set() if reset else None
        count = 0
        complete = False
        for _round in range(_FEED_MAX_ROUNDS):
            got_events = False
            for change in self.backend.sync_changes(reset=reset):
                got_events = True
                if change.kind == 'complete':
                    complete = True
                    continue
                if change.kind == 'delete':
                    db.delete_remote_asset(self.account.id, change.remote_id)
                else:
                    db.patch_remote_asset(
                        self.account.id,
                        change.remote_id,
                        change.fields or {},
                        create=change.kind == 'upsert',
                    )
                    if seen is not None and change.kind == 'upsert':
                        seen.add(change.remote_id)
                count += 1
                if count % _FEED_PROGRESS_EVERY == 0:
                    progress(f'Synced {count} changes…')
            reset = False  # later rounds resume from the checkpoint
            if complete or not got_events:
                break
        # Only reconcile against a full feed that actually reached the
        # head — a cut-off one hasn't mentioned everything that exists.
        if seen is not None and complete:
            swept = db.sweep_remote_assets(self.account.id, keep=seen)
            if swept:
                progress(f'Removed {swept} assets deleted on the server')
        progress(f'Applied {count} changes from server' if count
                 else 'Catalog is up to date')

    def _store_remote(self, db: Database, asset) -> None:
        db.upsert_remote_asset(
            self.account.id,
            asset.remote_id,
            checksum=asset.checksum,
            filename=asset.filename,
            mime_type=asset.mime_type,
            width=asset.width,
            height=asset.height,
            taken_at=asset.taken_at,
            size_bytes=asset.size_bytes,
            is_favorite=asset.is_favorite,
            latitude=asset.latitude,
            longitude=asset.longitude,
            place_city=asset.place_city,
            place_state=asset.place_state,
            place_country=asset.place_country,
            camera_make=asset.camera_make,
            camera_model=asset.camera_model,
            lens=asset.lens,
            iso=asset.iso,
            f_number=asset.f_number,
            exposure_time=asset.exposure_time,
            focal_length=asset.focal_length,
            orientation=asset.orientation,
            description=asset.description,
        )

    # ----- on-demand pagination (called by the gallery on scroll) -----

    def fetch_more_remote(
        self,
        page: int,
        *,
        size: int = SCROLL_SERVER_PAGE_SIZE,
        on_complete: Callable[[bool, int], None],
    ) -> None:
        """Fetch one server page in a worker thread.

        on_complete is called on the main loop with (has_more, count_added).
        Errors are logged to on_progress and on_complete is called with
        (False, 0) so the caller can fall through gracefully.
        """
        if self._fetching_more:
            on_complete(False, 0)
            return
        self._fetching_more = True

        def work() -> tuple[bool, int]:
            db = Database()
            try:
                items, has_more = self.backend.fetch_page(page=page, size=size)
                for asset in items:
                    self._store_remote(db, asset)
                return has_more, len(items)
            finally:
                db.close()

        def done(result):
            self._fetching_more = False
            has_more, n = result
            on_complete(has_more, n)

        def error(exc):
            self._fetching_more = False
            msg = exc.args[0] if isinstance(exc, BackendError) and exc.args else str(exc)
            self.on_error(msg)
            on_complete(False, 0)

        run_async(work, on_done=done, on_error=error)

    def _scan_local(self, progress: ProgressFn) -> None:
        db = Database()
        try:
            dirs = db.list_sync_dirs(self.account.id)
            if not dirs:
                return
            for path, recursive in dirs:
                progress(f'Scanning {path}…')
                root = Path(path)
                count = 0
                for fpath, checksum, mtime, size in scanner.scan(root, recursive):
                    db.upsert_local_asset(
                        self.account.id,
                        local_path=str(fpath),
                        checksum=checksum,
                        filename=fpath.name,
                        taken_at=mtime,
                        size_bytes=size,
                    )
                    count += 1
                    if count % 100 == 0:
                        progress(f'Scanned {count} files in {path}…')
        finally:
            db.close()

    def _upload_pending(self, progress: ProgressFn) -> None:
        db = Database()
        try:
            pending = list(db.pending_uploads(self.account.id))
            total = len(pending)
            if total == 0:
                return
            for i, asset in enumerate(pending, 1):
                if not asset.local_path or not asset.checksum:
                    continue
                progress(f'Uploading {i}/{total}…')
                try:
                    db.mark_asset_state(asset.id, 'uploading')
                    remote_id = self.backend.upload(
                        asset.local_path,
                        checksum=asset.checksum,
                        taken_at=asset.taken_at,
                    )
                    db.upsert_remote_asset(
                        self.account.id,
                        remote_id,
                        checksum=asset.checksum,
                        filename=asset.filename,
                    )
                except BackendError as e:
                    db.mark_asset_state(asset.id, 'failed', str(e))
            progress(f'Uploaded {total}')
        finally:
            db.close()
