"""Shared test bootstrap.

Tests import backend modules directly rather than through
``shoebox.main`` (which drags in the GTK/template stack), so the
required GI versions are declared here — the single declaration site
for the pytest entry point, mirroring main.py's role in the app.
"""

import gi

gi.require_version('Soup', '3.0')
