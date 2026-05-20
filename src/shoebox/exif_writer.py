"""Optional EXIF/XMP writer using GExiv2.

GExiv2 ships with the GNOME platform runtime so no PyPI/wheel dep is
needed, but importing it can still fail on stripped runtimes or older
hosts. Every function in this module is a graceful no-op when GExiv2 is
unavailable — the caller falls back to remote-only edits.
"""

from __future__ import annotations

import logging
from datetime import datetime

from gi.repository import GLib

log = logging.getLogger(__name__)


_gexiv2 = None
_gexiv2_checked = False


def _gexiv2_module():
    """Lazy GExiv2 import. Returns the module or None."""
    global _gexiv2, _gexiv2_checked
    if _gexiv2_checked:
        return _gexiv2
    _gexiv2_checked = True
    try:
        import gi
        gi.require_version('GExiv2', '0.10')
        from gi.repository import GExiv2  # noqa: WPS433
        _gexiv2 = GExiv2
    except (ImportError, ValueError) as e:
        log.info('GExiv2 unavailable; local EXIF writes will no-op: %s', e)
        _gexiv2 = None
    return _gexiv2


def is_available() -> bool:
    return _gexiv2_module() is not None


def write_metadata(
    path: str,
    *,
    taken_at: int | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    description: str | None = None,
) -> bool:
    """Write the given fields into the file's EXIF/XMP tags.

    Returns True on success, False if GExiv2 isn't available or writing
    failed. Fields left as None are not touched. Pass latitude AND
    longitude together to update GPS (or neither); a single coord update
    is silently ignored.
    """
    gx = _gexiv2_module()
    if gx is None:
        return False

    try:
        meta = gx.Metadata()
        meta.open_path(path)

        if taken_at is not None:
            dt = datetime.fromtimestamp(taken_at)
            # EXIF datetime format: "YYYY:MM:DD HH:MM:SS" (yes, colons in date).
            iso = dt.strftime('%Y:%m:%d %H:%M:%S')
            meta.set_tag_string('Exif.Photo.DateTimeOriginal', iso)
            meta.set_tag_string('Exif.Photo.DateTimeDigitized', iso)
            meta.set_tag_string('Exif.Image.DateTime', iso)

        if latitude is not None and longitude is not None:
            # GExiv2.set_gps_info(longitude, latitude, altitude) — order!
            meta.set_gps_info(longitude, latitude, 0.0)

        if description is not None:
            # XMP handles unicode cleanly; EXIF ImageDescription stays
            # for older readers. Skip Exif.Photo.UserComment because
            # the charset prefix dance is brittle and most readers
            # prefer the two above.
            meta.set_tag_string('Xmp.dc.description', description)
            meta.set_tag_string('Exif.Image.ImageDescription', description)

        meta.save_file(path)
        return True
    except GLib.Error as e:
        log.warning('GExiv2 write failed for %s: %s', path, e)
        return False
