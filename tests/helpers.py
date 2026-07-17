"""Shared test doubles for the isolated FarmBot test suite."""
import inspect

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import ServiceCall


class FakeLoop:
    """Stand-in for hass.loop; runs call_soon_threadsafe synchronously."""

    def call_soon_threadsafe(self, func, *args):
        func(*args)


class FakeEventBus:
    """Minimal stand-in for ``hass.bus``; records fired events."""

    def __init__(self):
        self.fired = []

    def async_fire(self, event_type, event_data=None):
        self.fired.append((event_type, dict(event_data or {})))


class FakeServiceRegistry:
    """Minimal stand-in for ``hass.services``."""

    def __init__(self):
        self._services = {}

    def has_service(self, domain, service):
        return (domain, service) in self._services

    def async_register(self, domain, service, func, schema=None, supports_response=None):
        self._services[(domain, service)] = (func, schema, supports_response)

    def async_remove(self, domain, service):
        self._services.pop((domain, service), None)

    def call(self, domain, service, data):
        """Validate `data` and invoke a *synchronous* handler directly.

        Kept for the existing execute_sequence/move_to tests, which
        register plain (non-async) handlers.
        """
        func, schema, _ = self._services[(domain, service)]
        validated = schema(data) if schema is not None else data
        return func(ServiceCall(domain=domain, service=service, data=validated))

    async def async_call(self, domain, service, data, return_response=False):
        """Validate `data` and invoke a handler, awaiting it if it's async.

        Mirrors real Home Assistant's ``hass.services.async_call`` closely
        enough for testing the FarmBot Vision bridge's async service
        handlers, including those registered with ``supports_response``.
        """
        func, schema, _ = self._services[(domain, service)]
        validated = schema(data) if schema is not None else data
        result = func(ServiceCall(domain=domain, service=service, data=validated))
        if inspect.isawaitable(result):
            result = await result
        return result


class FakeHass:
    """Minimal stand-in for HomeAssistant used by config_flow/manager tests."""

    def __init__(self, entries=None):
        self.config_entries = ConfigEntries(entries=entries)
        self.loop = FakeLoop()
        self.data = {}
        self.services = FakeServiceRegistry()
        self.bus = FakeEventBus()

    async def async_add_executor_job(self, func, *args):
        return func(*args)
