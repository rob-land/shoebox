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
