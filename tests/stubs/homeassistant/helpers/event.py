"""Minimal stand-in for homeassistant.helpers.event."""


def async_track_time_interval(hass, action, interval):
    """Stand-in that does not actually schedule anything; returns an unsub callable."""
    def _unsub():
        return None
    return _unsub
