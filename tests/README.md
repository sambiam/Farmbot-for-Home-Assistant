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

## Threading fidelity

Two stubs are deliberately strict, because the bugs they guard against are
threading bugs that only ever showed up in a running Home Assistant:

* `helpers/dispatcher.py` reproduces Core's `HassJob` classification. A
  dispatcher target marked `@callback` runs inline on the calling (event
  loop) thread; an **undecorated synchronous target is
  `HassJobType.Executor` and is run in a worker thread**, which is what made
  `switch.py` call `async_write_ha_state` off the loop.
* `helpers/entity.py` raises if `async_write_ha_state` is called from a
  thread other than the one that created `FakeHass`, mirroring Core's
  `hass.verify_event_loop_thread` check.

`FakeLoop.call_soon_threadsafe` only runs a callback inline when it is
already on the loop thread and otherwise queues it for `run_pending()`, so
`test_entity_state.py` can assert that a FarmBot MQTT callback arriving on a
worker thread genuinely crosses onto the loop before any entity is touched.

`FakeHass.fire_pending_timers()` stands in for the passage of time for
`async_call_later`, so the optimistic-state grace period is tested without
sleeping.

**Limitation:** this validates the integration's own logic (duplicate
prevention, reauth handling, MQTT callback behaviour, entity state
reconciliation and availability, service handlers, FarmBot Vision
validation) in isolation, but it is not a substitute for testing against a
real Home Assistant instance. The entity stubs
(`homeassistant/components/{switch,select,sensor,binary_sensor,button}.py`,
`homeassistant/helpers/entity.py`) model entity *behaviour* -- state
resolution, availability, thread rules -- not entity *registration*: the
platform setup functions, the entity registry and the full config-entry
setup/unload lifecycle (`async_setup_entry`/`async_unload_entry`) are still
not exercised here. All FarmBot HTTP and MQTT calls are mocked or faked; no
test contacts FarmBot, any Vision app, or any other external service.

Run with:

```
pytest
```
