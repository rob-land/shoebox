"""Immich backend.

Uses libsoup3 for HTTP. All methods are blocking — call from a worker thread.

API endpoints used (Immich >= 1.106; rename here if you run an older version):
  POST /api/auth/login                       email + password → {accessToken, ...}
  POST /api/auth/validateToken               header auth → {authStatus: true}
  GET  /api/users/me                         current user info
  POST /api/search/metadata                  paged asset listing
  POST /api/search/smart                     CLIP-backed natural-language search
  POST /api/assets/bulk-upload-check         dedupe by checksum
  GET  /api/assets/{id}/thumbnail            thumbnail bytes
  GET  /api/assets/{id}/original             original bytes
  POST /api/assets                           multipart upload
  POST /api/sync/stream                      JSONL change feed (v2 sync API)
  POST /api/sync/ack                         change-feed checkpoint acks

The sync endpoints need the v2 sync API (stable in Immich 2.x; tested
against 3.0) and a session token from password login — Immich rejects
API keys there. On servers without them sync_changes raises
NotImplementedError and the sync manager falls back to page pulls.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import socket
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

from gi.repository import Gio, GLib, Soup

from .base import (
    Backend, BackendError, RemoteAsset, RemoteChange, SyncResetRequired,
    UserInfo,
)

log = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 250
USER_AGENT = 'Shoebox/0.1 (libsoup3)'

# Change-feed subscriptions: core asset rows plus their EXIF side records.
SYNC_REQUEST_TYPES = ('AssetsV2', 'AssetExifsV1')
# Checkpoint with the server every N feed lines.
SYNC_ACK_INTERVAL = 500


def _device_id() -> str:
    return f'shoebox-{socket.gethostname()}'


def _as_int(v) -> int | None:
    if v is None or v == '':
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v) -> float | None:
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _checksum_hex(v) -> str | None:
    """Immich transmits checksums base64-encoded; the catalog stores hex
    (matching the local scanner's sha1 hexdigest, so checksum merges work)."""
    if not v:
        return None
    try:
        return base64.b64decode(v, validate=True).hex()
    except (ValueError, TypeError):
        return v


def _parse_timestamp(v) -> int | None:
    """ISO-8601 string → unix seconds."""
    if not v:
        return None
    try:
        return int(datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp())
    except (ValueError, AttributeError):
        return None


def _parse_exposure(v) -> float | None:
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

    def __init__(self, server_url: str, token: str | None = None):
        super().__init__(server_url, token)
        self._session = self._new_session()

    @staticmethod
    def _new_session() -> Soup.Session:
        session = Soup.Session.new()
        session.set_user_agent(USER_AGENT)
        session.set_timeout(30)
        return session

    # ---------- low-level HTTP ----------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        raw_body: bytes | None = None,
        body_content_type: str | None = None,
        params: dict | None = None,
        accept: str = 'application/json',
        session: Soup.Session | None = None,
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
            data = (session or self._session).send_and_read(msg, None)
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

    def search_smart(self, query: str, *, limit: int = 100) -> list[RemoteAsset]:
        # Immich's /search/smart returns the same {assets: {items: [...]}} shape
        # as /search/metadata, ordered by CLIP relevance. `size` caps the page;
        # we only ever ask for the first page since relevance falls off fast.
        q = query.strip()
        if not q:
            return []
        resp = self._post_json('/api/search/smart', {
            'query': q,
            'size': max(1, min(limit, 250)),
            'page': 1,
            'withExif': True,
        })
        assets = resp.get('assets', {}) if isinstance(resp, dict) else {}
        return [self._asset_from_json(r) for r in assets.get('items', [])]

    @staticmethod
    def _asset_from_json(raw: dict) -> RemoteAsset:
        exif = raw.get('exifInfo') or {}
        taken_at = _parse_timestamp(
            raw.get('fileCreatedAt') or raw.get('localDateTime')
        )

        return RemoteAsset(
            remote_id=raw['id'],
            checksum=_checksum_hex(raw.get('checksum')),
            filename=raw.get('originalFileName'),
            mime_type=raw.get('originalMimeType') or exif.get('mimeType'),
            width=exif.get('exifImageWidth'),
            height=exif.get('exifImageHeight'),
            taken_at=taken_at,
            size_bytes=exif.get('fileSizeInByte'),
            is_favorite=raw.get('isFavorite'),
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

    # ---------- change feed (v2 sync) ----------

    def sync_changes(self, *, reset: bool = False) -> Iterator[RemoteChange]:
        """Stream asset changes from /api/sync/stream.

        The server checkpoints per session token, resuming from the last
        acknowledged line, so each yielded change must be applied before
        the caller advances the iterator.
        """
        body: dict = {'types': list(SYNC_REQUEST_TYPES)}
        if reset:
            body['reset'] = True
        # The feed gets its own session: issuing the ack POSTs on the
        # session whose response stream is still being read makes libsoup
        # recurse until the stack overflows.
        stream_session = self._new_session()
        stream = self._open_stream(
            stream_session, 'POST', '/api/sync/stream', body=body,
        )
        reader = Gio.DataInputStream.new(stream)
        pending_acks: dict[str, str] = {}
        unacked = 0
        try:
            while True:
                try:
                    line, _length = reader.read_line_utf8(None)
                except GLib.Error as e:
                    raise BackendError(f'sync stream read: {e.message}') from e
                if line is None:
                    break
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    log.warning('skipping malformed sync line: %.100s', line)
                    continue
                etype = event.get('type')
                if etype == 'SyncResetV1':
                    raise SyncResetRequired('server requested a full resync')
                change = self._change_from_event(etype, event.get('data') or {})
                if change is not None:
                    yield change
                # Once the yield returns, the consumer has applied the
                # change, so its ack token is safe to checkpoint. Tokens
                # embed their entity type as the first |-field; the server
                # keeps one checkpoint per type.
                ack = event.get('ack')
                if ack:
                    pending_acks[ack.split('|', 1)[0]] = ack
                    unacked += 1
                if unacked >= SYNC_ACK_INTERVAL:
                    self._send_acks(pending_acks)
                    unacked = 0
        finally:
            try:
                reader.close(None)
            except GLib.Error:
                pass
        self._send_acks(pending_acks)

    def _open_stream(
        self, session: Soup.Session, method: str, path: str, *, body: dict,
    ) -> Gio.InputStream:
        msg = Soup.Message.new(method, self.server_url + path)
        headers = msg.get_request_headers()
        headers.append('Accept', 'application/jsonlines+json')
        if self.token:
            headers.append('Authorization', f'Bearer {self.token}')
        payload = json.dumps(body).encode('utf-8')
        msg.set_request_body_from_bytes(
            'application/json', GLib.Bytes.new(payload)
        )
        try:
            stream = session.send(msg, None)
        except GLib.Error as e:
            raise BackendError(f'{method} {path}: {e.message}') from e
        status = msg.get_status()
        if status in (403, 404):
            # Pre-v2-sync server, or a token the sync API rejects
            # (API keys). The sync manager falls back to page pulls.
            raise NotImplementedError(f'{path} unsupported (HTTP {status})')
        if status >= 400:
            raise BackendError(f'{method} {path} → HTTP {status}')
        return stream

    def _send_acks(self, acks: dict[str, str]) -> None:
        """Best-effort checkpoint of consumed feed lines.

        Runs on a private session inside its own GLib main context —
        pumping the shared thread-default context while the feed's
        response stream is open re-enters libsoup and can recurse until
        the stack overflows. Failure is not fatal: the server just
        re-serves already-applied (idempotent) changes next time.
        """
        if not acks:
            return
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        try:
            self._request(
                'POST', '/api/sync/ack',
                body={'acks': list(acks.values())},
                session=self._new_session(),
            )
        except BackendError as e:
            log.warning('sync ack failed (changes re-serve next sync): %s', e)
        else:
            acks.clear()
        finally:
            ctx.pop_thread_default()

    @staticmethod
    def _change_from_event(etype: str | None, data: dict) -> RemoteChange | None:
        if etype in ('AssetV1', 'AssetV2'):
            remote_id = data.get('id')
            if not remote_id:
                return None
            # Trashed / archived / hidden assets leave the timeline.
            visible = data.get('visibility') in (None, 'timeline')
            if data.get('deletedAt') or not visible:
                return RemoteChange('delete', remote_id)
            return RemoteChange(
                'upsert', remote_id,
                ImmichBackend._fields_from_asset_event(data),
            )
        if etype == 'AssetDeleteV1':
            remote_id = data.get('assetId')
            return RemoteChange('delete', remote_id) if remote_id else None
        if etype == 'AssetExifV1':
            remote_id = data.get('assetId')
            if not remote_id:
                return None
            return RemoteChange(
                'patch', remote_id,
                ImmichBackend._fields_from_exif_event(data),
            )
        if etype == 'SyncCompleteV1':
            return RemoteChange('complete', '')
        if etype != 'SyncAckV1':
            log.debug('ignoring sync event type %s', etype)
        return None

    @staticmethod
    def _fields_from_asset_event(data: dict) -> dict:
        filename = data.get('originalFileName')
        return {
            'checksum': _checksum_hex(data.get('checksum')),
            'filename': filename,
            'mime_type': mimetypes.guess_type(filename or '')[0],
            'width': data.get('width'),
            'height': data.get('height'),
            'taken_at': _parse_timestamp(
                data.get('fileCreatedAt') or data.get('localDateTime')
            ),
            'is_favorite': data.get('isFavorite'),
        }

    @staticmethod
    def _fields_from_exif_event(data: dict) -> dict:
        return {
            'size_bytes': data.get('fileSizeInByte'),
            'latitude': _as_float(data.get('latitude')),
            'longitude': _as_float(data.get('longitude')),
            'place_city': data.get('city'),
            'place_state': data.get('state'),
            'place_country': data.get('country'),
            'camera_make': data.get('make'),
            'camera_model': data.get('model'),
            'lens': data.get('lensModel'),
            'iso': _as_int(data.get('iso')),
            'f_number': _as_float(data.get('fNumber')),
            'exposure_time': _parse_exposure(data.get('exposureTime')),
            'focal_length': _as_float(data.get('focalLength')),
            'orientation': _as_int(data.get('orientation')),
            'description': data.get('description'),
        }

    # ---------- bytes ----------

    def fetch_thumbnail(self, remote_id: str, size: int) -> bytes:
        # Immich serves two pre-rendered sizes: 'thumbnail' (~256 px) and
        # 'preview' (~1440 px). The latter is roughly 6x the bytes for
        # marginal visual gain on tiles, so only escalate when the user
        # has cranked the thumbnail-size pref above ~2x the small render.
        kind = 'preview' if size > 320 else 'thumbnail'
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
        taken_at: int | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        description: str | None = None,
        is_favorite: bool | None = None,
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

    def asset_exists(self, checksum: str) -> str | None:
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
        taken_at: int | None = None,
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
