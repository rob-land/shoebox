"""libsecret wrapper for storing per-account API tokens."""

from __future__ import annotations

import threading

from gi.repository import Gio, Secret

SCHEMA = Secret.Schema.new(
    'land.rob.shoebox.Account',
    Secret.SchemaFlags.NONE,
    {
        'backend': Secret.SchemaAttributeType.STRING,
        'account_id': Secret.SchemaAttributeType.STRING,
    },
)

_TIMEOUT = 5


def _cancellable_with_timeout():
    """Return a GCancellable that auto-cancels after _TIMEOUT seconds."""
    cancel = Gio.Cancellable()
    timer = threading.Timer(_TIMEOUT, cancel.cancel)
    timer.daemon = True
    timer.start()
    return cancel, timer


def store_token(account_id: int, backend: str, token: str) -> bool:
    label = f'Shoebox ({backend} account {account_id})'
    cancel, timer = _cancellable_with_timeout()
    try:
        return Secret.password_store_sync(
            SCHEMA,
            {'backend': backend, 'account_id': str(account_id)},
            Secret.COLLECTION_DEFAULT,
            label,
            token,
            cancel,
        )
    except Exception:
        return False
    finally:
        timer.cancel()


def lookup_token(account_id: int, backend: str) -> str | None:
    cancel, timer = _cancellable_with_timeout()
    try:
        return Secret.password_lookup_sync(
            SCHEMA,
            {'backend': backend, 'account_id': str(account_id)},
            cancel,
        )
    except Exception:
        return None
    finally:
        timer.cancel()


def clear_token(account_id: int, backend: str) -> bool:
    cancel, timer = _cancellable_with_timeout()
    try:
        return Secret.password_clear_sync(
            SCHEMA,
            {'backend': backend, 'account_id': str(account_id)},
            cancel,
        )
    except Exception:
        return False
    finally:
        timer.cancel()
