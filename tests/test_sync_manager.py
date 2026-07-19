"""SyncManager change-feed orchestration: reset, retry, sweep, fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shoebox.backends.base import RemoteChange, SyncResetRequired
from shoebox.database import Database
from shoebox.sync.manager import SyncManager, _FEED_READY_KEY


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'catalog.sqlite')
    yield database
    database.close()


@pytest.fixture
def account(db):
    return db.add_account('immich', 'http://server', 'user@example.com')


class FakeBackend:
    """Duck-typed stand-in: scripted change feed plus a page fallback."""

    def __init__(self, feeds=None):
        # feeds: list of lists-of-changes (or exceptions), consumed per call.
        self.feeds = list(feeds or [])
        self.sync_calls: list[bool] = []
        self.page_calls = 0

    def sync_changes(self, *, reset=False):
        self.sync_calls.append(reset)
        feed = self.feeds.pop(0)
        if isinstance(feed, Exception):
            raise feed
        yield from feed

    def fetch_page(self, page=1, size=200):
        self.page_calls += 1
        return [], False


def make_manager(db, account, backend):
    return SyncManager(SimpleNamespace(), account, backend)


def upsert(remote_id, **fields):
    return RemoteChange('upsert', remote_id, {'filename': 'x.jpg', **fields})


COMPLETE = RemoteChange('complete', '')


def test_first_run_resets_and_sweeps(db, account):
    # r-stale predates the feed and isn't mentioned by the full stream.
    db.patch_remote_asset(account.id, 'r-stale', {'filename': 'old.jpg'})
    backend = FakeBackend(feeds=[[upsert('r1'), upsert('r2'), COMPLETE]])
    manager = make_manager(db, account, backend)

    manager._pull_change_feed(db, lambda _msg: None)

    assert backend.sync_calls == [True]
    assert {a.remote_id for a in db.list_assets(account.id)} == {'r1', 'r2'}
    assert db.get_account_state(account.id, _FEED_READY_KEY) == '1'


def test_later_runs_pull_deltas_without_sweeping(db, account):
    db.set_account_state(account.id, _FEED_READY_KEY, '1')
    db.patch_remote_asset(account.id, 'r-existing', {'filename': 'keep.jpg'})
    backend = FakeBackend(feeds=[[
        upsert('r1'),
        RemoteChange('patch', 'r-existing', {'iso': 400}),
        RemoteChange('delete', 'r-gone'),
        COMPLETE,
    ]])
    manager = make_manager(db, account, backend)

    manager._pull_change_feed(db, lambda _msg: None)

    assert backend.sync_calls == [False]
    assets = {a.remote_id: a for a in db.list_assets(account.id)}
    assert set(assets) == {'r1', 'r-existing'}
    assert assets['r-existing'].iso == 400


def test_server_reset_retries_with_full_feed(db, account):
    db.set_account_state(account.id, _FEED_READY_KEY, '1')
    db.patch_remote_asset(account.id, 'r-stale', {'filename': 'old.jpg'})
    backend = FakeBackend(feeds=[
        SyncResetRequired('checkpoint expired'),
        [upsert('r1'), COMPLETE],
    ])
    manager = make_manager(db, account, backend)

    manager._pull_change_feed(db, lambda _msg: None)

    assert backend.sync_calls == [False, True]
    assert {a.remote_id for a in db.list_assets(account.id)} == {'r1'}


def test_cut_off_feed_resumes_until_complete(db, account):
    # A proxy cut the first response before the sentinel: the second
    # round resumes from the checkpoint, and the sweep still sees the
    # union of both rounds.
    db.patch_remote_asset(account.id, 'r-stale', {'filename': 'old.jpg'})
    backend = FakeBackend(feeds=[
        [upsert('r1')],
        [upsert('r2'), COMPLETE],
    ])
    manager = make_manager(db, account, backend)

    manager._pull_change_feed(db, lambda _msg: None)

    assert backend.sync_calls == [True, False]
    assert {a.remote_id for a in db.list_assets(account.id)} == {'r1', 'r2'}


def test_no_sentinel_skips_sweep(db, account):
    # Feed dries up without ever reaching the head: keep possibly-stale
    # rows rather than dropping assets the feed never got to mention.
    db.patch_remote_asset(account.id, 'r-stale', {'filename': 'old.jpg'})
    backend = FakeBackend(feeds=[[upsert('r1')], []])
    manager = make_manager(db, account, backend)

    manager._pull_change_feed(db, lambda _msg: None)

    assert backend.sync_calls == [True, False]
    assert {a.remote_id for a in db.list_assets(account.id)} == {'r1', 'r-stale'}


def test_backends_without_feed_fall_back_to_page_pull(
    db, account, tmp_path, monkeypatch,
):
    backend = FakeBackend(feeds=[NotImplementedError()])
    manager = make_manager(db, account, backend)
    # _pull_remote opens its own catalog connection; point it at the test one.
    monkeypatch.setattr(
        'shoebox.sync.manager.Database',
        lambda: Database(tmp_path / 'catalog.sqlite'),
    )

    manager._pull_remote(lambda _msg: None)

    assert backend.sync_calls == [True]  # fresh account → full-feed attempt
    assert backend.page_calls == 1
