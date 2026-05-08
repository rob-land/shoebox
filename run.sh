#!/usr/bin/env bash
# Dev launcher — meson install into a local prefix and run from there.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PFX="$HERE/_install"
BUILD="$HERE/_build"

if [ ! -f "$BUILD/build.ninja" ]; then
    meson setup "$BUILD" --prefix="$PFX"
fi
meson install -C "$BUILD" >/dev/null

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
export PYTHONPATH="$PFX/lib/python$PYVER/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export GSETTINGS_SCHEMA_DIR="$PFX/share/glib-2.0/schemas${GSETTINGS_SCHEMA_DIR:+:$GSETTINGS_SCHEMA_DIR}"
export XDG_DATA_DIRS="$PFX/share:${XDG_DATA_DIRS:-/usr/share}"

exec "$PFX/bin/shoebox" "$@"
