"""Shared test doubles for the isolated FarmBot test suite."""
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import ServiceCall


class FakeLoop:
    """Stand-in for hass.loop; runs call_soon_threadsafe synchronously."""

    def call_soon_threadsafe(self, func, *args):
        func(*args)


class FakeServiceRegistry:
    """Minimal stand-in for ``hass.services``."""

    def __init__(self):
        self._services = {}

    def has_service(self, domain, service):
        return (domain, service) in self._services

    def async_register(self, domain, service, func, schema=None):
        self._services[(domain, service)] = (func, schema)

    def async_remove(self, domain, service):
        self._services.pop((domain, service), None)

    def call(self, domain, service, data):
        """Validate `data` against the registered schema and invoke the handler."""
        func, schema = self._services[(domain, service)]
        validated = schema(data) if schema is not None else data
        return func(ServiceCall(domain=domain, service=service, data=validated))


class FakeHass:
    """Minimal stand-in for HomeAssistant used by config_flow/manager tests."""

    def __init__(self, entries=None):
        self.config_entries = ConfigEntries(entries=entries)
        self.loop = FakeLoop()
        self.data = {}
        self.services = FakeServiceRegistry()

    async def async_add_executor_job(self, func, *args):
        return func(*args)
