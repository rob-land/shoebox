"""Backends for self-hosted photo services."""

from __future__ import annotations

from typing import Type

from .base import Backend, BackendError, RemoteAsset
from .immich import ImmichBackend

# Registry of available backends keyed by short name.
BACKENDS: dict[str, type[Backend]] = {
    ImmichBackend.name: ImmichBackend,
}


def get(name: str) -> type[Backend]:
    if name not in BACKENDS:
        raise KeyError(f'unknown backend: {name}')
    return BACKENDS[name]


__all__ = ['Backend', 'BackendError', 'RemoteAsset', 'BACKENDS', 'get']
