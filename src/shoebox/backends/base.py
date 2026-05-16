"""Abstract backend interface.

Implementations are blocking (synchronous I/O) and expected to be called from
a worker thread by callers that need to keep the GTK main loop responsive.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, Optional


class BackendError(Exception):
    """Raised when a backend call fails (network, auth, server)."""


@dataclass
class RemoteAsset:
    remote_id: str
    checksum: Optional[str] = None
    filename: Optional[str] = None
    mime_type: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    taken_at: Optional[int] = None
    size_bytes: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    place_city: Optional[str] = None
    place_state: Optional[str] = None
    place_country: Optional[str] = None
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    lens: Optional[str] = None
    iso: Optional[int] = None
    f_number: Optional[float] = None
    exposure_time: Optional[float] = None  # seconds
    focal_length: Optional[float] = None   # mm
    orientation: Optional[int] = None
    description: Optional[str] = None


@dataclass
class UserInfo:
    user_id: str
    username: str
    display_name: Optional[str] = None


class Backend(ABC):
    name: str = 'abstract'
    display_name: str = 'Abstract Backend'

    def __init__(self, server_url: str, token: Optional[str] = None):
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
        self, *, page_size: int = 200, since: Optional[int] = None,
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
        taken_at: Optional[int] = None,
    ) -> str:
        """Upload a local file, return the remote_id."""

    @abstractmethod
    def asset_exists(self, checksum: str) -> Optional[str]:
        """Check whether an asset with this checksum exists; return remote_id or None."""

    def update_asset(
        self,
        remote_id: str,
        *,
        taken_at: Optional[int] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        description: Optional[str] = None,
        is_favorite: Optional[bool] = None,
    ) -> None:
        """Push metadata edits to the backend.

        Optional capability: backends that don't implement this raise
        NotImplementedError, and the caller should treat the edit as
        local-only. Fields left as None are not updated.
        """
        raise NotImplementedError
