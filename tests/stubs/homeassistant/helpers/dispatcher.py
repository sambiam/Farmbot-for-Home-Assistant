"""Minimal stand-in for homeassistant.helpers.dispatcher.

Unlike the earlier no-op stand-in, this one actually dispatches: listeners
registered via ``async_dispatcher_connect`` for a given ``hass``/``signal``
pair are invoked by ``async_dispatcher_send``. Listener storage lives on
the ``hass`` instance itself so tests using separate ``FakeHass`` objects
never see each other's listeners.
"""


def async_dispatcher_connect(hass, signal, target):
    """Register `target` to be called by async_dispatcher_send(hass, signal, ...)."""
    listeners = getattr(hass, "_dispatcher_listeners", None)
    if listeners is None:
        listeners = {}
        hass._dispatcher_listeners = listeners
    listeners.setdefault(signal, []).append(target)

    def _unsub():
        remaining = listeners.get(signal)
        if remaining and target in remaining:
            remaining.remove(target)

    return _unsub


def async_dispatcher_send(hass, signal, *args):
    listeners = getattr(hass, "_dispatcher_listeners", None) or {}
    for target in list(listeners.get(signal, [])):
        target(*args)
