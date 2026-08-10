"""Minimal stand-in for homeassistant.helpers.event."""


def async_track_time_interval(hass, action, interval):
    """Stand-in that does not actually schedule anything; returns an unsub callable."""
    def _unsub():
        return None
    return _unsub


def async_call_later(hass, delay, action):
    """Record a delayed callback on ``hass`` so a test can fire it explicitly.

    Real Home Assistant schedules ``action`` on the event loop after
    ``delay`` seconds. Tests drive time themselves via
    ``FakeHass.fire_pending_timers()`` rather than sleeping.
    """
    timers = getattr(hass, "_pending_timers", None)
    if timers is None:
        timers = []
        hass._pending_timers = timers
    handle = {"delay": delay, "action": action, "cancelled": False}
    timers.append(handle)

    def _cancel():
        handle["cancelled"] = True
        if handle in timers:
            timers.remove(handle)

    return _cancel
