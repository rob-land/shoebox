"""Immich v2 sync change feed: event mapping, catalog patching, streaming."""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from shoebox.backends.base import BackendError, SyncResetRequired
from shoebox.backends.immich import ImmichBackend
from shoebox.database import Database

CHECKSUM_HEX = 'ab' * 20
CHECKSUM_B64 = base64.b64encode(bytes.fromhex(CHECKSUM_HEX)).decode()
TAKEN_ISO = '2024-06-01T12:00:00Z'
TAKEN_AT = int(datetime(2024, 6, 1, 12, tzinfo=timezone.utc).timestamp())


def asset_event(asset_id: str = 'a1', **overrides) -> dict:
    data = {
        'id': asset_id,
        'checksum': CHECKSUM_B64,
        'originalFileName': 'IMG_0001.jpg',
        'fileCreatedAt': TAKEN_ISO,
        'localDateTime': '2024-06-01T14:00:00Z',
        'isFavorite': True,
        'width': 4000,
        'height': 3000,
        'deletedAt': None,
        'visibility': 'timeline',
    }
    data.update(overrides)
    return data


# ----- event → RemoteChange mapping -----

class TestChangeFromEvent:
    def test_asset_upsert(self):
        change = ImmichBackend._change_from_event('AssetV2', asset_event())
        assert change.kind == 'upsert'
        assert change.remote_id == 'a1'
        assert change.fields['checksum'] == CHECKSUM_HEX
        assert change.fields['filename'] == 'IMG_0001.jpg'
        assert change.fields['mime_type'] == 'image/jpeg'
        assert change.fields['taken_at'] == TAKEN_AT
        assert change.fields['is_favorite'] is True
        assert change.fields['width'] == 4000

    def test_trashed_asset_becomes_delete(self):
        change = ImmichBackend._change_from_event(
            'AssetV2', asset_event(deletedAt='2024-06-02T00:00:00Z'),
        )
        assert change.kind == 'delete'
        assert change.remote_id == 'a1'

    def test_non_timeline_visibility_becomes_delete(self):
        for visibility in ('archive', 'hidden', 'locked'):
            change = ImmichBackend._change_from_event(
                'AssetV2', asset_event(visibility=visibility),
            )
            assert change.kind == 'delete'

    def test_asset_delete(self):
        change = ImmichBackend._change_from_event('AssetDeleteV1', {'assetId': 'a9'})
        assert change.kind == 'delete'
        assert change.remote_id == 'a9'

    def test_exif_patch(self):
        change = ImmichBackend._change_from_event('AssetExifV1', {
            'assetId': 'a1',
            'exposureTime': '1/200',
            'fNumber': 1.8,
            'iso': 100,
            'orientation': '6',
            'city': 'Lisbon',
            'country': 'Portugal',
            'latitude': 38.7,
            'longitude': -9.1,
            'fileSizeInByte': 123456,
            'description': None,
        })
        assert change.kind == 'patch'
        assert change.remote_id == 'a1'
        assert change.fields['exposure_time'] == pytest.approx(1 / 200)
        assert change.fields['f_number'] == pytest.approx(1.8)
        assert change.fields['orientation'] == 6
        assert change.fields['place_city'] == 'Lisbon'
        assert change.fields['size_bytes'] == 123456
        assert change.fields['description'] is None

    def test_ack_events_map_to_nothing(self):
        assert ImmichBackend._change_from_event('SyncAckV1', {}) is None

    def test_complete_event_maps_to_sentinel(self):
        change = ImmichBackend._change_from_event('SyncCompleteV1', {})
        assert change.kind == 'complete'
        assert change.fields is None


# ----- catalog patching -----

@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'catalog.sqlite')
    yield database
    database.close()


@pytest.fixture
def account(db):
    return db.add_account('immich', 'http://server', 'user@example.com')


class TestPatchRemoteAsset:
    def test_creates_then_patches_only_named_fields(self, db, account):
        db.patch_remote_asset(
            account.id, 'r1', {'filename': 'a.jpg', 'taken_at': 100},
        )
        asset, = db.list_assets(account.id)
        assert asset.remote_id == 'r1'
        assert asset.sync_state == 'server_only'
        db.patch_remote_asset(account.id, 'r1', {'taken_at': 200})
        asset, = db.list_assets(account.id)
        assert asset.taken_at == 200
        assert asset.filename == 'a.jpg'

    def test_create_false_drops_patch_for_missing_row(self, db, account):
        db.patch_remote_asset(account.id, 'r1', {'iso': 100}, create=False)
        assert db.list_assets(account.id) == []

    def test_rejects_unknown_columns(self, db, account):
        with pytest.raises(ValueError):
            db.patch_remote_asset(account.id, 'r1', {'sync_state': 'synced'})

    def test_merges_local_only_row_by_checksum(self, db, account):
        db.upsert_local_asset(
            account.id, '/pics/a.jpg', CHECKSUM_HEX, filename='a.jpg',
        )
        db.patch_remote_asset(
            account.id, 'r1', {'checksum': CHECKSUM_HEX, 'taken_at': 100},
        )
        asset, = db.list_assets(account.id)
        assert asset.remote_id == 'r1'
        assert asset.local_path == '/pics/a.jpg'
        assert asset.sync_state == 'synced'


class TestDeleteAndSweep:
    def test_delete_drops_server_only_row(self, db, account):
        db.patch_remote_asset(account.id, 'r1', {'filename': 'a.jpg'})
        db.delete_remote_asset(account.id, 'r1')
        assert db.list_assets(account.id) == []

    def test_delete_keeps_row_backed_by_local_file(self, db, account):
        db.upsert_local_asset(account.id, '/pics/a.jpg', CHECKSUM_HEX)
        db.patch_remote_asset(account.id, 'r1', {'checksum': CHECKSUM_HEX})
        db.delete_remote_asset(account.id, 'r1')
        asset, = db.list_assets(account.id)
        assert asset.remote_id is None
        assert asset.local_path == '/pics/a.jpg'
        assert asset.sync_state == 'remote_deleted'
        assert list(db.pending_uploads(account.id)) == []

    def test_sweep_removes_rows_missing_from_feed(self, db, account):
        db.patch_remote_asset(account.id, 'r1', {'filename': 'a.jpg'})
        db.patch_remote_asset(account.id, 'r2', {'filename': 'b.jpg'})
        assert db.sweep_remote_assets(account.id, keep={'r1'}) == 1
        asset, = db.list_assets(account.id)
        assert asset.remote_id == 'r1'


def test_account_state_roundtrip(db, account):
    assert db.get_account_state(account.id, 'change-feed-ready') is None
    db.set_account_state(account.id, 'change-feed-ready', '1')
    assert db.get_account_state(account.id, 'change-feed-ready') == '1'
    db.set_account_state(account.id, 'change-feed-ready', '0')
    assert db.get_account_state(account.id, 'change-feed-ready') == '0'


# ----- streaming against a fake Immich server -----

class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        if self.path == '/api/sync/stream':
            self.server.stream_requests.append(body)
            if self.server.stream_status != 200:
                self._respond(self.server.stream_status)
                return
            payload = ''.join(
                json.dumps(line) + '\n' for line in self.server.stream_lines
            ).encode()
            self._respond(200, payload, 'application/jsonlines+json')
        elif self.path == '/api/sync/ack':
            self.server.acks.append(body['acks'])
            self._respond(204)
        else:
            self._respond(404)

    def _respond(self, status, payload=b'', content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.fixture
def fake_server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    server.stream_lines = []
    server.stream_status = 200
    server.stream_requests = []
    server.acks = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _backend(server) -> ImmichBackend:
    return ImmichBackend(
        f'http://127.0.0.1:{server.server_address[1]}', token='token',
    )


def test_sync_changes_end_to_end(fake_server):
    fake_server.stream_lines = [
        {'type': 'AssetV2', 'data': asset_event(), 'ack': 'AssetV2|1'},
        {'type': 'AssetExifV1', 'data': {'assetId': 'a1', 'iso': 200},
         'ack': 'AssetExifV1|2'},
        {'type': 'AssetDeleteV1', 'data': {'assetId': 'gone'},
         'ack': 'AssetDeleteV1|3'},
        {'type': 'SyncCompleteV1', 'data': {}, 'ack': 'SyncCompleteV1|4'},
    ]
    changes = list(_backend(fake_server).sync_changes())
    assert [c.kind for c in changes] == ['upsert', 'patch', 'delete', 'complete']
    assert changes[0].fields['checksum'] == CHECKSUM_HEX
    assert fake_server.stream_requests == [
        {'types': ['AssetsV2', 'AssetExifsV1']},
    ]
    # One checkpoint batch at end of stream, newest token per entity type.
    assert fake_server.acks == [
        ['AssetV2|1', 'AssetExifV1|2', 'AssetDeleteV1|3', 'SyncCompleteV1|4'],
    ]


def test_sync_changes_acks_in_batches(fake_server, monkeypatch):
    from shoebox.backends import immich
    monkeypatch.setattr(immich, 'SYNC_ACK_INTERVAL', 2)
    fake_server.stream_lines = [
        {'type': 'AssetV2', 'data': asset_event(f'a{i}'), 'ack': f'AssetV2|{i}'}
        for i in range(5)
    ]
    list(_backend(fake_server).sync_changes())
    # Two full batches mid-stream, the remainder at end of stream; each
    # batch carries only the newest token per type since the last one.
    assert fake_server.acks == [['AssetV2|1'], ['AssetV2|3'], ['AssetV2|4']]


def test_sync_changes_forwards_reset_flag(fake_server):
    list(_backend(fake_server).sync_changes(reset=True))
    assert fake_server.stream_requests == [
        {'types': ['AssetsV2', 'AssetExifsV1'], 'reset': True},
    ]


def test_reset_event_raises_without_acking(fake_server):
    fake_server.stream_lines = [
        {'type': 'SyncResetV1', 'data': {}, 'ack': 'SyncResetV1|1'},
    ]
    with pytest.raises(SyncResetRequired):
        list(_backend(fake_server).sync_changes())
    assert fake_server.acks == []


@pytest.mark.parametrize('status', [403, 404])
def test_unsupported_server_signals_capability(fake_server, status):
    fake_server.stream_status = status
    with pytest.raises(NotImplementedError):
        list(_backend(fake_server).sync_changes())


def test_server_error_is_a_backend_error(fake_server):
    fake_server.stream_status = 500
    with pytest.raises(BackendError):
        list(_backend(fake_server).sync_changes())
