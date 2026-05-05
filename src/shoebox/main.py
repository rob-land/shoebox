import sys

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Soup', '3.0')
gi.require_version('Secret', '1')

from .application import ShoeboxApplication


def run(version: str) -> int:
    app = ShoeboxApplication(version=version)
    return app.run(sys.argv)
