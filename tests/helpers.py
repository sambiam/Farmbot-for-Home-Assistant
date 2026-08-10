"""Shared test doubles for the isolated FarmBot test suite."""
import inspect
import threading

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import ServiceCall


class FakeLoop:
    """Stand-in for ``hass.loop``.

    ``call_soon_threadsafe`` runs the callback immediately when it is already
    on the loop thread (which keeps the existing manager tests synchronous),
    and queues it when called from a worker thread -- the real marshalling
    behaviour. Queued callbacks run when the test calls ``run_pending()``
    from the loop thread, which is what proves the FarmBot MQTT thread never
    touches Home Assistant directly.
    """

    def __init__(self, thread_id=None):
        self.thread_id = threading.get_ident() if thread_id is None else thread_id
        self.pending = []

    def call_soon_threadsafe(self, func, *args):
        if threading.get_ident() == self.thread_id:
            func(*args)
        else:
            self.pending.append((func, args))

    def run_pending(self):
        """Run queued callbacks on the current (loop) thread."""
        queued, self.pending = self.pending, []
        for func, args in queued:
            func(*args)
        return len(queued)


class FakeStateMachine:
    """Records the states entities write, newest last."""

    def __init__(self):
        self.writes = []

    def async_set(self, entity_id, entity):
        self.writes.append((entity_id, getattr(entity, "state", None)))

    def get(self, entity_id):
        for written_id, state in reversed(self.writes):
            if written_id == entity_id:
                return state
        return None


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
        self.loop_thread_id = self.loop.thread_id
        self.data = {}
        self.services = FakeServiceRegistry()
        self.bus = FakeEventBus()
        self.states = FakeStateMachine()
        self._pending_timers = []

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro):
        coro.close()
        return None

    def fire_pending_timers(self, min_delay=0.0):
        """Fire callbacks registered through ``async_call_later``.

        Stands in for the passage of time so the optimistic-state grace
        period can be tested without sleeping.
        """
        due = [t for t in list(self._pending_timers) if t["delay"] >= min_delay]
        for timer in due:
            if timer in self._pending_timers:
                self._pending_timers.remove(timer)
            if not timer["cancelled"]:
                timer["action"](None)
        return len(due)
