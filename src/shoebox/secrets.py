"""libsecret wrapper for storing per-account API tokens."""

from __future__ import annotations

from typing import Optional


from gi.repository import Secret

SCHEMA = Secret.Schema.new(
    'land.rob.shoebox.Account',
    Secret.SchemaFlags.NONE,
    {
        'backend': Secret.SchemaAttributeType.STRING,
        'account_id': Secret.SchemaAttributeType.STRING,
    },
)


def store_token(account_id: int, backend: str, token: str) -> bool:
    label = f'Shoebox ({backend} account {account_id})'
    return Secret.password_store_sync(
        SCHEMA,
        {'backend': backend, 'account_id': str(account_id)},
        Secret.COLLECTION_DEFAULT,
        label,
        token,
        None,
    )


def lookup_token(account_id: int, backend: str) -> Optional[str]:
    return Secret.password_lookup_sync(
        SCHEMA,
        {'backend': backend, 'account_id': str(account_id)},
        None,
    )


def clear_token(account_id: int, backend: str) -> bool:
    return Secret.password_clear_sync(
        SCHEMA,
        {'backend': backend, 'account_id': str(account_id)},
        None,
    )
