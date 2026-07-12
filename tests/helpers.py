"""Shared test doubles for the isolated FarmBot test suite."""
from homeassistant.config_entries import ConfigEntries


class FakeLoop:
    """Stand-in for hass.loop; runs call_soon_threadsafe synchronously."""

    def call_soon_threadsafe(self, func, *args):
        func(*args)


class FakeHass:
    """Minimal stand-in for HomeAssistant used by config_flow/manager tests."""

    def __init__(self, entries=None):
        self.config_entries = ConfigEntries(entries=entries)
        self.loop = FakeLoop()
        self.data = {}

    async def async_add_executor_job(self, func, *args):
        return func(*args)
