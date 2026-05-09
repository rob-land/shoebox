"""Thin GSettings wrapper."""

from __future__ import annotations

from gi.repository import Gio

SCHEMA_ID = 'land.rob.shoebox'


def get() -> Gio.Settings:
    return Gio.Settings.new(SCHEMA_ID)
