"""Immich backend.

Uses libsoup3 for HTTP. All methods are blocking — call from a worker thread.

API endpoints used (Immich >= 1.106; rename here if you run an older version):
  POST /api/auth/login                       email + password → {accessToken, ...}
  POST /api/auth/validateToken               header auth → {authStatus: true}
  GET  /api/users/me                         current user info
  POST /api/search/metadata                  paged asset listing
  POST /api/assets/bulk-upload-check         dedupe by checksum
  GET  /api/assets/{id}/thumbnail            thumbnail bytes
  GET  /api/assets/{id}/original             original bytes
  POST /api/assets                           multipart upload
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import uuid
from datetime import datetime, timezone
from typing import Optional


from gi.repository import GLib, Soup

from .base import Backend, BackendError, RemoteAsset, UserInfo

DEFAULT_PAGE_SIZE = 250
USER_AGENT = 'Shoebox/0.1 (libsoup3)'


def _device_id() -> str:
    return f'shoebox-{socket.gethostname()}'


def _as_int(v) -> Optional[int]:
    if v is None or v == '':
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v) -> Optional[float]:
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_exposure(v) -> Optional[float]:
    """Immich returns exposureTime as a string like '1/200' or '0.005'."""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if '/' in s:
        try:
            num, den = s.split('/', 1)
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


class ImmichBackend(Backend):
    name = 'immich'
    display_name = 'Immich'

    def __init__(self, server_url: str, token: Optional[str] = None):
        super().__init__(server_url, token)
        self._session = Soup.Session.new()
        self._session.set_user_agent(USER_AGENT)
        self._session.set_timeout(30)

    # ---------- low-level HTTP ----------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict] = None,
        raw_body: Optional[bytes] = None,
        body_content_type: Optional[str] = None,
        params: Optional[dict] = None,
        accept: str = 'application/json',
    ) -> bytes:
        url = self.server_url + path
        if params:
            qs = GLib.Uri.build(
                GLib.UriFlags.NONE, 'http', None, 'x', -1, '', None, None
            )
            # Simple manual querystring; libsoup's URI builder is awkward for this.
            from urllib.parse import urlencode
            url = f'{url}?{urlencode(params)}'

        msg = Soup.Message.new(method, url)
        headers = msg.get_request_headers()
        headers.append('Accept', accept)
        if self.token:
            headers.append('Authorization', f'Bearer {self.token}')

        if body is not None:
            payload = json.dumps(body).encode('utf-8')
            msg.set_request_body_from_bytes(
                'application/json', GLib.Bytes.new(payload)
            )
        elif raw_body is not None:
            msg.set_request_body_from_bytes(
                body_content_type or 'application/octet-stream',
                GLib.Bytes.new(raw_body),
            )

        try:
            data = self._session.send_and_read(msg, None)
        except GLib.Error as e:
            raise BackendError(f'{method} {path}: {e.message}') from e

        status = msg.get_status()
        body_bytes = data.get_data() or b''
        if status >= 400:
            raise BackendError(
                f'{method} {path} → HTTP {status}: {body_bytes[:200].decode("utf-8", "replace")}'
            )
        return body_bytes

    def _get_json(self, path: str, **kwargs) -> dict | list:
        raw = self._request('GET', path, **kwargs)
        return json.loads(raw or b'null')

    def _post_json(self, path: str, body: dict) -> dict | list:
        raw = self._request('POST', path, body=body)
        return json.loads(raw or b'null')

    def _put_json(self, path: str, body: dict) -> dict | list:
        raw = self._request('PUT', path, body=body)
        return json.loads(raw or b'null')

    # ---------- auth ----------

    def login(self, username: str, password: str) -> tuple[str, UserInfo]:
        # Email is the canonical identifier in Immich.
        resp = self._post_json('/api/auth/login', {
            'email': username,
            'password': password,
        })
        token = resp.get('accessToken')
        if not token:
            raise BackendError('login response missing accessToken')
        self.token = token
        user = UserInfo(
            user_id=resp.get('userId') or '',
            username=resp.get('userEmail') or username,
            display_name=resp.get('name'),
        )
        return token, user

    def validate(self) -> UserInfo:
        if not self.token:
            raise BackendError('no token set')
        resp = self._post_json('/api/auth/validateToken', {})
        if not resp.get('authStatus'):
            raise BackendError('token rejected')
        me = self._get_json('/api/users/me')
        return UserInfo(
            user_id=me.get('id', ''),
            username=me.get('email', ''),
            display_name=me.get('name'),
        )

    # ---------- listing ----------

    def fetch_page(
        self, page: int = 1, size: int = DEFAULT_PAGE_SIZE,
    ) -> tuple[list[RemoteAsset], bool]:
        body: dict = {
            'page': page,
            'size': size,
            'withExif': True,
            'order': 'desc',
        }
        resp = self._post_json('/api/search/metadata', body)
        assets = resp.get('assets', {})
        items = [self._asset_from_json(r) for r in assets.get('items', [])]
        has_more = bool(assets.get('nextPage'))
        return items, has_more

    @staticmethod
    def _asset_from_json(raw: dict) -> RemoteAsset:
        exif = raw.get('exifInfo') or {}
        taken = raw.get('fileCreatedAt') or raw.get('localDateTime')
        taken_at: Optional[int] = None
        if taken:
            try:
                taken_at = int(
                    datetime.fromisoformat(taken.replace('Z', '+00:00')).timestamp()
                )
            except (ValueError, AttributeError):
                taken_at = None

        return RemoteAsset(
            remote_id=raw['id'],
            checksum=raw.get('checksum'),
            filename=raw.get('originalFileName'),
            mime_type=raw.get('originalMimeType') or exif.get('mimeType'),
            width=exif.get('exifImageWidth'),
            height=exif.get('exifImageHeight'),
            taken_at=taken_at,
            size_bytes=exif.get('fileSizeInByte'),
            latitude=_as_float(exif.get('latitude')),
            longitude=_as_float(exif.get('longitude')),
            place_city=exif.get('city'),
            place_state=exif.get('state'),
            place_country=exif.get('country'),
            camera_make=exif.get('make'),
            camera_model=exif.get('model'),
            lens=exif.get('lensModel'),
            iso=_as_int(exif.get('iso')),
            f_number=_as_float(exif.get('fNumber')),
            exposure_time=_parse_exposure(exif.get('exposureTime')),
            focal_length=_as_float(exif.get('focalLength')),
            orientation=_as_int(exif.get('orientation')),
            description=exif.get('description'),
        )

    # ---------- bytes ----------

    def fetch_thumbnail(self, remote_id: str, size: int) -> bytes:
        kind = 'preview' if size > 250 else 'thumbnail'
        return self._request(
            'GET',
            f'/api/assets/{remote_id}/thumbnail',
            params={'size': kind},
            accept='image/*',
        )

    def fetch_original(self, remote_id: str) -> bytes:
        return self._request(
            'GET',
            f'/api/assets/{remote_id}/original',
            accept='application/octet-stream',
        )

    # ---------- upload ----------

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
        body: dict = {}
        if taken_at is not None:
            body['dateTimeOriginal'] = datetime.fromtimestamp(
                taken_at, tz=timezone.utc,
            ).isoformat()
        if latitude is not None:
            body['latitude'] = latitude
        if longitude is not None:
            body['longitude'] = longitude
        if description is not None:
            body['description'] = description
        if is_favorite is not None:
            body['isFavorite'] = is_favorite
        if not body:
            return
        self._put_json(f'/api/assets/{remote_id}', body)

    def asset_exists(self, checksum: str) -> Optional[str]:
        resp = self._post_json('/api/assets/bulk-upload-check', {
            'assets': [{'id': checksum, 'checksum': checksum}],
        })
        results = resp.get('results') or []
        for r in results:
            if r.get('action') == 'reject' and r.get('reason') == 'duplicate':
                return r.get('assetId')
        return None

    def upload(
        self,
        local_path: str,
        *,
        checksum: str,
        taken_at: Optional[int] = None,
    ) -> str:
        existing = self.asset_exists(checksum)
        if existing:
            return existing

        boundary = f'shoebox-{uuid.uuid4().hex}'
        mime = mimetypes.guess_type(local_path)[0] or 'application/octet-stream'
        filename = os.path.basename(local_path)
        device_asset_id = f'{filename}-{int(os.path.getmtime(local_path))}'
        created_iso = datetime.fromtimestamp(
            taken_at or os.path.getmtime(local_path), tz=timezone.utc
        ).isoformat()
        modified_iso = datetime.fromtimestamp(
            os.path.getmtime(local_path), tz=timezone.utc
        ).isoformat()

        with open(local_path, 'rb') as f:
            file_bytes = f.read()

        parts = [
            ('deviceAssetId', device_asset_id),
            ('deviceId', _device_id()),
            ('fileCreatedAt', created_iso),
            ('fileModifiedAt', modified_iso),
            ('isFavorite', 'false'),
            ('checksum', checksum),
        ]

        body = bytearray()
        for name, value in parts:
            body += f'--{boundary}\r\n'.encode()
            body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            body += value.encode()
            body += b'\r\n'
        body += f'--{boundary}\r\n'.encode()
        body += (
            f'Content-Disposition: form-data; name="assetData"; '
            f'filename="{filename}"\r\n'
        ).encode()
        body += f'Content-Type: {mime}\r\n\r\n'.encode()
        body += file_bytes
        body += f'\r\n--{boundary}--\r\n'.encode()

        raw = self._request(
            'POST',
            '/api/assets',
            raw_body=bytes(body),
            body_content_type=f'multipart/form-data; boundary={boundary}',
        )
        resp = json.loads(raw or b'null')
        remote_id = resp.get('id')
        if not remote_id:
            raise BackendError(f'upload response missing id: {resp}')
        return remote_id
