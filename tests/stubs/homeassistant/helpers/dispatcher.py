"""Minimal stand-in for homeassistant.helpers.dispatcher.

Faithful on the one point that matters for this integration: **how Home
Assistant decides which thread a dispatcher target runs on**.

``async_dispatcher_connect`` wraps its target in a ``HassJob``, whose type
comes from ``get_hassjob_callable_job_type``:

* a coroutine function -> ``HassJobType.Coroutinefunction`` (awaited on the
  loop),
* a function marked with ``@callback`` -> ``HassJobType.Callback``, run
  inline on the event loop,
* **anything else -> ``HassJobType.Executor``, run in a worker thread.**

That last rule is why an undecorated ``def _update_from_state(self, status)``
ended up calling ``async_write_ha_state`` off the event loop. The stub
reproduces it, so a target that loses its ``@callback`` marker again is
executed in a real thread here too and trips the thread check in the
``Entity`` stub.

Listener storage lives on the ``hass`` instance itself so tests using
separate ``FakeHass`` objects never see each other's listeners.
"""
import functools
import inspect
import threading


def _is_callback(target):
    check = target
    while isinstance(check, functools.partial):
        check = check.func
    return getattr(check, "_hass_callback", False)


def _is_coroutine_function(target):
    check = target
    while isinstance(check, functools.partial):
        check = check.func
    return inspect.iscoroutinefunction(check)


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
        if _is_callback(target):
            target(*args)  # inline, on the calling (event loop) thread
        elif _is_coroutine_function(target):
            hass.async_create_task(target(*args))
        else:
            # HassJobType.Executor: Home Assistant hands this to the thread
            # pool. Joining keeps the test deterministic; the thread identity
            # is what the assertion is about.
            error = []

            def _run():
                try:
                    target(*args)
                except BaseException as err:  # surfaced to the caller below
                    error.append(err)

            worker = threading.Thread(target=_run)
            worker.start()
            worker.join()
            if error:
                raise error[0]
