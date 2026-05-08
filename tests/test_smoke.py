"""Smoke test — confirm the package imports cleanly.

The real test suite goes here; for now this just exercises the
pyproject pythonpath wiring and the Meson `pytest` target.
"""

def test_package_imports():
    import shoebox
    assert shoebox.__doc__
