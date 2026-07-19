"""Abstract backend interface.

Implementations are blocking (synchronous I/O) and expected to be called from
a worker thread by callers that need to keep the GTK main loop responsive.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass


class BackendError(Exception):
    """Raised when a backend call fails (network, auth, server)."""


class SyncResetRequired(BackendError):
    """The backend lost the change-feed checkpoint; re-sync from scratch.

    Raised by Backend.sync_changes when the server can no longer resume
    from the last acknowledged position (expired or missing checkpoint).
    The caller should restart with ``sync_changes(reset=True)`` and treat
    the result as the complete server state.
    """


@dataclass
class RemoteAsset:
    remote_id: str
    checksum: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    taken_at: int | None = None
    size_bytes: int | None = None
    is_favorite: bool | None = None
    latitude: float | None = None
    longitude: float | None = None
    place_city: str | None = None
    place_state: str | None = None
    place_country: str | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    lens: str | None = None
    iso: int | None = None
    f_number: float | None = None
    exposure_time: float | None = None  # seconds
    focal_length: float | None = None   # mm
    orientation: int | None = None
    description: str | None = None


@dataclass
class RemoteChange:
    """One entry from a backend change feed (see Backend.sync_changes).

    *kind* is one of:
      'upsert'   — the asset was created or updated; a catalog row may
                   be created if none exists yet.
      'patch'    — secondary metadata (e.g. EXIF) for an asset that
                   should already have a row; dropped if the row is gone.
      'delete'   — the asset was removed (or hidden) server-side.
      'complete' — sentinel: the feed reached the server's current head.
                   A feed may end early *without* this (e.g. a proxy cut
                   the response); callers re-pull until they see it.

    *fields* maps RemoteAsset field names to new values. Only the named
    fields changed; an explicit None clears the stored value. Deletions
    and the completion sentinel carry no fields (and 'complete' has an
    empty remote_id).
    """
    kind: str
    remote_id: str
    fields: dict[str, object] | None = None


@dataclass
class UserInfo:
    user_id: str
    username: str
    display_name: str | None = None


class Backend(ABC):
    name: str = 'abstract'
    display_name: str = 'Abstract Backend'

    def __init__(self, server_url: str, token: str | None = None):
        self.server_url = server_url.rstrip('/')
        self.token = token

    @abstractmethod
    def login(self, username: str, password: str) -> tuple[str, UserInfo]:
        """Authenticate; return (token, user_info). Sets self.token on success."""

    @abstractmethod
    def validate(self) -> UserInfo:
        """Verify the current token; return user info or raise BackendError."""

    @abstractmethod
    def fetch_page(
        self, page: int = 1, size: int = 200,
    ) -> tuple[list[RemoteAsset], bool]:
        """Return one page of remote assets ordered newest-first.

        *page* is 1-based. Returns (assets, has_more).
        """

    def iter_assets(
        self, *, page_size: int = 200, since: int | None = None,
    ) -> Iterator[RemoteAsset]:
        """Yield every remote asset by walking pages. Default impl uses fetch_page."""
        page = 1
        while True:
            items, has_more = self.fetch_page(page=page, size=page_size)
            for item in items:
                yield item
            if not has_more:
                return
            page += 1

    @abstractmethod
    def fetch_thumbnail(self, remote_id: str, size: int) -> bytes:
        """Return raw thumbnail bytes."""

    @abstractmethod
    def fetch_original(self, remote_id: str) -> bytes:
        """Return raw original file bytes."""

    @abstractmethod
    def upload(
        self,
        local_path: str,
        *,
        checksum: str,
        taken_at: int | None = None,
    ) -> str:
        """Upload a local file, return the remote_id."""

    @abstractmethod
    def asset_exists(self, checksum: str) -> str | None:
        """Check whether an asset with this checksum exists; return remote_id or None."""

    def sync_changes(self, *, reset: bool = False) -> Iterator[RemoteChange]:
        """Yield changes since the last acknowledged sync checkpoint.

        Optional capability: backends without a change feed raise
        NotImplementedError, and the caller should fall back to polling
        with fetch_page.

        With ``reset=True`` the feed restarts from scratch and re-yields
        every asset; the caller should reconcile rows it didn't see.
        Raises SyncResetRequired when the server demands such a restart.

        The iterator ends either at a RemoteChange(kind='complete')
        sentinel (feed fully drained) or early when the transport cuts
        the response; in the latter case the caller should just call
        again — progress is checkpointed, so the next call resumes.

        Implementations acknowledge progress to the server as the caller
        consumes the iterator, so each change must be durably applied
        before advancing to the next one.
        """
        raise NotImplementedError

    def search_smart(self, query: str, *, limit: int = 100) -> list[RemoteAsset]:
        """Natural-language search across the backend's library.

        Optional capability: backends without a smart-search index raise
        NotImplementedError, and the caller should hide the search UI or
        fall back to a local-only mode.
        """
        raise NotImplementedError

    def update_asset(
        self,
        remote_id: str,
        *,
        taken_at: int | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        description: str | None = None,
        is_favorite: bool | None = None,
    ) -> None:
        """Push metadata edits to the backend.

        Optional capability: backends that don't implement this raise
        NotImplementedError, and the caller should treat the edit as
        local-only. Fields left as None are not updated.
        """
        raise NotImplementedError
