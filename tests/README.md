# Tests

This is a small custom Home Assistant integration, not a fork of Home
Assistant core, so pulling in the full `homeassistant` package (and its very
large dependency tree) just to run unit tests was judged disproportionate
for this repository.

Instead, `tests/stubs/homeassistant/` provides a minimal, hand-written stand
-in for only the pieces of `homeassistant` that
`custom_components/farmbot/config_flow.py`, `manager.py` and `__init__.py`
actually import (`ConfigFlow` unique-ID de-duplication, form/entry/abort
results, `async_update_reload_and_abort`, `async_track_time_interval`,
dispatcher helpers). `tests/conftest.py` puts that stub package ahead of
any real Home Assistant install on `sys.path`.

**Limitation:** this validates the integration's own logic (duplicate
prevention, reauth handling, MQTT callback behaviour) in isolation, but it
is not a substitute for testing against a real Home Assistant instance —
platform setup (`switch.py`, `sensor.py`, etc.), entity registration, and
the full config entry lifecycle are not exercised here. All FarmBot HTTP
and MQTT calls are mocked; no test contacts FarmBot or any external
service.

Run with:

```
pytest
```
