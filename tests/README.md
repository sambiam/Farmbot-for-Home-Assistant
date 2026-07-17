# Tests

This is a small custom Home Assistant integration, not a fork of Home
Assistant core, so pulling in the full `homeassistant` package (and its very
large dependency tree) just to run unit tests was judged disproportionate
for this repository.

Instead, `tests/stubs/homeassistant/` provides a minimal, hand-written stand
-in for only the pieces of `homeassistant` that
`custom_components/farmbot/config_flow.py`, `manager.py`, `__init__.py` and
`api.py` actually import (`ConfigFlow`/`OptionsFlow`, unique-ID
de-duplication, form/entry/abort results, `async_update_reload_and_abort`,
`async_track_time_interval`, dispatcher helpers, `SupportsResponse`,
translated exceptions, `homeassistant.util.dt`). `tests/conftest.py` puts
that stub package ahead of any real Home Assistant install on `sys.path`.

Real `aiohttp` and `Pillow` *are* installed (see `requirements-test.txt`) --
they're genuine runtime dependencies of `api.py`/`image_utils.py`, not
Home Assistant internals, so there's no reason to stub them. `aiohttp`
network calls are still never made in tests: `tests/fake_aiohttp.py`
provides a scripted fake `ClientSession`/response pair that `api.py`'s
tests drive directly, and `tests/fake_api.py` provides a fake
`FarmbotApiClient` double (recording calls, scripting responses/failures)
that the service-handler tests swap onto `FarmbotManager.api`.

The dispatcher stub (`tests/stubs/homeassistant/helpers/dispatcher.py`)
actually dispatches -- listeners registered via `async_dispatcher_connect`
for a given `(hass, signal)` pair are invoked by `async_dispatcher_send` --
which is what lets `test_manager_vision.py` assert that FarmBot Vision
status updates only dispatch when something actually changed.

**Limitation:** this validates the integration's own logic (duplicate
prevention, reauth handling, MQTT callback behaviour, service handlers,
FarmBot Vision validation) in isolation, but it is not a substitute for
testing against a real Home Assistant instance — entity platform setup
(`switch.py`, `sensor.py`, `binary_sensor.py`, `button.py`, `select.py`),
entity registration, and the full config-entry setup/unload lifecycle
(`async_setup_entry`/`async_unload_entry`) are not exercised here, since
that would require stubbing much more of `homeassistant.components.*` and
`homeassistant.helpers.entity_platform` than is proportionate for this
repository. All FarmBot HTTP and MQTT calls are mocked or faked; no test
contacts FarmBot, any Vision app, or any other external service.

Run with:

```
pytest
```
