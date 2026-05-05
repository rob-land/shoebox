"""SQLite catalog of accounts, assets, and sync directories."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from gi.repository import GLib

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  backend TEXT NOT NULL,
  server_url TEXT NOT NULL,
  username TEXT NOT NULL,
  user_id TEXT,
  display_name TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  remote_id TEXT,
  checksum TEXT,
  local_path TEXT,
  filename TEXT,
  mime_type TEXT,
  width INTEGER,
  height INTEGER,
  taken_at INTEGER,
  uploaded_at INTEGER,
  size_bytes INTEGER,
  is_favorite INTEGER NOT NULL DEFAULT 0,
  sync_state TEXT NOT NULL DEFAULT 'unknown',
  last_error TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS assets_remote ON assets(account_id, remote_id)
  WHERE remote_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS assets_local ON assets(account_id, local_path)
  WHERE local_path IS NOT NULL;
CREATE INDEX IF NOT EXISTS assets_taken_at ON assets(taken_at DESC);
CREATE INDEX IF NOT EXISTS assets_state ON assets(sync_state);

CREATE TABLE IF NOT EXISTS sync_dirs (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  recursive INTEGER NOT NULL DEFAULT 1,
  UNIQUE(account_id, path)
);
"""


@dataclass
class Account:
    id: int
    backend: str
    server_url: str
    username: str
    user_id: Optional[str]
    display_name: Optional[str]


@dataclass
class Asset:
    id: int
    account_id: int
    remote_id: Optional[str]
    checksum: Optional[str]
    local_path: Optional[str]
    filename: Optional[str]
    mime_type: Optional[str]
    width: Optional[int]
    height: Optional[int]
    taken_at: Optional[int]
    sync_state: str

    @property
    def is_local_only(self) -> bool:
        return self.remote_id is None and self.local_path is not None

    @property
    def is_server_only(self) -> bool:
        return self.local_path is None and self.remote_id is not None


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        id=row['id'],
        backend=row['backend'],
        server_url=row['server_url'],
        username=row['username'],
        user_id=row['user_id'],
        display_name=row['display_name'],
    )


def _row_to_asset(row: sqlite3.Row) -> Asset:
    return Asset(
        id=row['id'],
        account_id=row['account_id'],
        remote_id=row['remote_id'],
        checksum=row['checksum'],
        local_path=row['local_path'],
        filename=row['filename'],
        mime_type=row['mime_type'],
        width=row['width'],
        height=row['height'],
        taken_at=row['taken_at'],
        sync_state=row['sync_state'],
    )


class Database:
    def __init__(self, path: Optional[Path] = None):
        if path is None:
            data_dir = Path(GLib.get_user_data_dir()) / 'shoebox'
            data_dir.mkdir(parents=True, exist_ok=True)
            path = data_dir / 'catalog.sqlite'
        self.path = path
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA foreign_keys = ON')
        self._conn.execute('PRAGMA journal_mode = WAL')
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        cur = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        )
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # ----- accounts -----

    def add_account(
        self,
        backend: str,
        server_url: str,
        username: str,
        user_id: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> Account:
        cur = self._conn.execute(
            """INSERT INTO accounts (backend, server_url, username,
                                     user_id, display_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (backend, server_url, username, user_id, display_name, int(time.time())),
        )
        return Account(cur.lastrowid, backend, server_url, username, user_id, display_name)

    def list_accounts(self) -> list[Account]:
        cur = self._conn.execute('SELECT * FROM accounts ORDER BY id')
        return [_row_to_account(r) for r in cur.fetchall()]

    def get_account(self, account_id: int) -> Optional[Account]:
        cur = self._conn.execute('SELECT * FROM accounts WHERE id = ?', (account_id,))
        row = cur.fetchone()
        return _row_to_account(row) if row else None

    def delete_account(self, account_id: int) -> None:
        self._conn.execute('DELETE FROM accounts WHERE id = ?', (account_id,))

    # ----- sync directories -----

    def add_sync_dir(self, account_id: int, path: str, recursive: bool = True) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO sync_dirs (account_id, path, recursive)
               VALUES (?, ?, ?)""",
            (account_id, path, 1 if recursive else 0),
        )

    def remove_sync_dir(self, account_id: int, path: str) -> None:
        self._conn.execute(
            'DELETE FROM sync_dirs WHERE account_id = ? AND path = ?',
            (account_id, path),
        )

    def list_sync_dirs(self, account_id: int) -> list[tuple[str, bool]]:
        cur = self._conn.execute(
            'SELECT path, recursive FROM sync_dirs WHERE account_id = ? ORDER BY path',
            (account_id,),
        )
        return [(r['path'], bool(r['recursive'])) for r in cur.fetchall()]

    # ----- assets -----

    def upsert_remote_asset(
        self,
        account_id: int,
        remote_id: str,
        *,
        checksum: Optional[str] = None,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        taken_at: Optional[int] = None,
        size_bytes: Optional[int] = None,
    ) -> None:
        # If a local-only row already has this checksum, merge them.
        if checksum:
            cur = self._conn.execute(
                """SELECT id FROM assets
                   WHERE account_id = ? AND checksum = ? AND remote_id IS NULL""",
                (account_id, checksum),
            )
            row = cur.fetchone()
            if row is not None:
                self._conn.execute(
                    """UPDATE assets
                       SET remote_id = ?, filename = COALESCE(?, filename),
                           mime_type = COALESCE(?, mime_type),
                           width = COALESCE(?, width), height = COALESCE(?, height),
                           taken_at = COALESCE(?, taken_at),
                           size_bytes = COALESCE(?, size_bytes),
                           sync_state = 'synced'
                       WHERE id = ?""",
                    (remote_id, filename, mime_type, width, height,
                     taken_at, size_bytes, row['id']),
                )
                return

        self._conn.execute(
            """INSERT INTO assets (account_id, remote_id, checksum, filename,
                                   mime_type, width, height, taken_at, size_bytes,
                                   sync_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'server_only')
               ON CONFLICT(account_id, remote_id) WHERE remote_id IS NOT NULL
               DO UPDATE SET checksum = excluded.checksum,
                             filename = excluded.filename,
                             mime_type = excluded.mime_type,
                             width = excluded.width,
                             height = excluded.height,
                             taken_at = excluded.taken_at,
                             size_bytes = excluded.size_bytes""",
            (account_id, remote_id, checksum, filename, mime_type,
             width, height, taken_at, size_bytes),
        )

    def upsert_local_asset(
        self,
        account_id: int,
        local_path: str,
        checksum: str,
        *,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        taken_at: Optional[int] = None,
        size_bytes: Optional[int] = None,
    ) -> None:
        # If a server-only row already has this checksum, merge them.
        cur = self._conn.execute(
            """SELECT id FROM assets
               WHERE account_id = ? AND checksum = ? AND local_path IS NULL""",
            (account_id, checksum),
        )
        row = cur.fetchone()
        if row is not None:
            self._conn.execute(
                """UPDATE assets SET local_path = ?, filename = COALESCE(filename, ?),
                                     sync_state = 'synced'
                   WHERE id = ?""",
                (local_path, filename, row['id']),
            )
            return

        self._conn.execute(
            """INSERT INTO assets (account_id, local_path, checksum, filename,
                                   mime_type, width, height, taken_at, size_bytes,
                                   sync_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
               ON CONFLICT(account_id, local_path) WHERE local_path IS NOT NULL
               DO UPDATE SET checksum = excluded.checksum,
                             mime_type = excluded.mime_type,
                             width = excluded.width,
                             height = excluded.height,
                             taken_at = excluded.taken_at,
                             size_bytes = excluded.size_bytes""",
            (account_id, local_path, checksum, filename, mime_type,
             width, height, taken_at, size_bytes),
        )

    def mark_asset_state(self, asset_id: int, state: str, error: Optional[str] = None) -> None:
        self._conn.execute(
            'UPDATE assets SET sync_state = ?, last_error = ? WHERE id = ?',
            (state, error, asset_id),
        )

    def list_assets(self, account_id: int, limit: int = 500, offset: int = 0) -> list[Asset]:
        cur = self._conn.execute(
            """SELECT * FROM assets WHERE account_id = ?
               ORDER BY COALESCE(taken_at, 0) DESC, id DESC
               LIMIT ? OFFSET ?""",
            (account_id, limit, offset),
        )
        return [_row_to_asset(r) for r in cur.fetchall()]

    def pending_uploads(self, account_id: int) -> Iterable[Asset]:
        cur = self._conn.execute(
            """SELECT * FROM assets
               WHERE account_id = ? AND sync_state IN ('pending', 'failed')
                 AND local_path IS NOT NULL
               ORDER BY id""",
            (account_id,),
        )
        for r in cur.fetchall():
            yield _row_to_asset(r)

    def asset_count(self, account_id: int) -> tuple[int, int, int]:
        """Returns (total, local_only, pending)."""
        cur = self._conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN remote_id IS NULL THEN 1 ELSE 0 END) AS local_only,
                      SUM(CASE WHEN sync_state = 'pending' THEN 1 ELSE 0 END) AS pending
               FROM assets WHERE account_id = ?""",
            (account_id,),
        )
        r = cur.fetchone()
        return (r['total'] or 0, r['local_only'] or 0, r['pending'] or 0)

    def close(self) -> None:
        self._conn.close()
