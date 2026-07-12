"""Minimal stand-in for the ``homeassistant`` package.

Only implements the small surface actually imported by the FarmBot custom
component modules under test (``config_flow.py``, ``manager.py`` and
``__init__.py``). See ``tests/README.md`` for why this exists instead of a
real Home Assistant install.
"""
