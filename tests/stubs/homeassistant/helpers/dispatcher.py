"""Minimal stand-in for homeassistant.helpers.dispatcher."""


def async_dispatcher_send(hass, signal, *args):
    """Stand-in that records nothing and does not dispatch anywhere."""
    return None


def async_dispatcher_connect(hass, signal, target):
    """Stand-in that does not actually connect anything; returns an unsub callable."""
    def _unsub():
        return None
    return _unsub
