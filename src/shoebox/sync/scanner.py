"""Scan local sync directories for new image files."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

IMAGE_SUFFIXES = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif',
    '.tiff', '.tif', '.bmp', '.avif', '.dng', '.cr2', '.nef', '.arw',
}
VIDEO_SUFFIXES = {
    '.mp4', '.mov', '.m4v', '.mkv', '.webm', '.avi', '.3gp',
}


def _sha1_of_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with path.open('rb') as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def iter_files(root: Path, recursive: bool = True) -> Iterator[Path]:
    if not root.exists():
        return
    if recursive:
        yield from (p for p in root.rglob('*')
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES)
    else:
        yield from (p for p in root.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES)


def scan(root: Path, recursive: bool = True):
    """Yield (path, checksum, mtime, size) for each media file under *root*."""
    for path in iter_files(root, recursive):
        try:
            stat = path.stat()
        except OSError:
            continue
        try:
            checksum = _sha1_of_file(path)
        except OSError:
            continue
        yield path, checksum, int(stat.st_mtime), stat.st_size
