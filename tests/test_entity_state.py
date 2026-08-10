"""Entity-level tests for FarmBot state reconciliation, availability and threading.

These drive the real code path end to end: a FarmBot MQTT payload goes into
``FarmbotManager._on_message``, is merged into the manager's cached state,
crosses the thread boundary through ``hass.loop.call_soon_threadsafe``, is
dispatched on ``SIGNAL_STATE`` and lands on a real switch entity.

The Home Assistant stubs are deliberately strict about the two rules that
were being broken:

* ``async_write_ha_state`` raises if called off the loop thread (Core
  reports this as "calls async_write_ha_state from a thread other than the
  event loop"), and
* the dispatcher stub runs an undecorated synchronous target in a worker
  thread, exactly as Core's ``HassJobType.Executor`` classification does.

Together they mean a regression in either direction fails the suite instead
of only showing up in a user's log.
"""
import json
import threading
import time
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest

from custom_components.farmbot.const import (
    DEVICE_STATE_STALE_SECONDS,
    TOPIC_STATUS,
)
from custom_components.farmbot.manager import FarmbotManager
from custom_components.farmbot.switch import (
    FarmbotEmergencyStopSwitch,
    FarmbotPeripheralSwitch,
)

from .helpers import FakeHass

DEVICE_ID = "42"
LIGHTING_PIN = 7


def _status_message(body: dict):
    """A paho message carrying one FarmBot status payload."""
    message = MagicMock()
    message.topic = TOPIC_STATUS.format(device_id=DEVICE_ID)
    message.payload = json.dumps(body).encode()
    return message


def _make_manager():
    hass = FakeHass()
    manager = FarmbotManager(
        hass, token="tok", device_id=DEVICE_ID, mqtt_host="mqtt.example.com"
    )
    client = MagicMock()
    client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
    manager._mqtt = client
    manager._mqtt_connected = True
    manager._mqtt_connected_at = time.monotonic()
    manager._last_status_at = time.monotonic()
    return hass, manager, client


async def _make_switch(pin=LIGHTING_PIN, label="Lighting"):
    hass, manager, client = _make_manager()
    entity = FarmbotPeripheralSwitch(manager, pin, label)
    entity.hass = hass
    entity.entity_id = f"switch.farmbot_{label.lower()}"
    await entity.async_added_to_hass()
    return hass, manager, client, entity


def _report(manager, pins=None, **rest):
    """Deliver a FarmBot status payload the way paho would."""
    body = dict(rest)
    if pins is not None:
        body["pins"] = {str(pin): {"value": value, "mode": 0} for pin, value in pins.items()}
    manager._on_message(None, None, _status_message(body))


# --------------------------------------------------------------------------
# Optimistic switch behaviour
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_packet_does_not_revert_a_pending_command():
    """OFF -> user asks for ON -> a stale OFF packet must not flick it back."""
    _hass, manager, _client, entity = await _make_switch()
    _report(manager, pins={LIGHTING_PIN: 0})
    assert entity.state == "off"

    await entity.async_turn_on()
    assert entity.state == "on"  # immediately, without waiting for FarmBot

    # A status packet generated before the write_pin was applied.
    _report(manager, pins={LIGHTING_PIN: 0})
    assert entity.state == "on"
    assert entity._pending_state is True

    # FarmBot catches up and confirms.
    _report(manager, pins={LIGHTING_PIN: 1})
    assert entity.state == "on"
    assert entity._pending_state is None  # pending command cleared


@pytest.mark.asyncio
async def test_unconfirmed_command_reverts_once_the_grace_period_lapses():
    """A command FarmBot never applies must not leave the entity lying."""
    hass, manager, _client, entity = await _make_switch()
    _report(manager, pins={LIGHTING_PIN: 0})

    await entity.async_turn_on()
    assert entity.state == "on"

    # FarmBot keeps insisting the pin is off, and never confirms.
    _report(manager, pins={LIGHTING_PIN: 0})
    assert entity.state == "on"

    assert hass.fire_pending_timers() == 1  # the reconciliation deadline
    assert entity.state == "off"
    assert entity._pending_state is None


@pytest.mark.asyncio
async def test_pending_command_lapses_even_without_the_timer():
    """The deadline is also enforced lazily, not only by the scheduled timer."""
    _hass, manager, _client, entity = await _make_switch()
    _report(manager, pins={LIGHTING_PIN: 0})
    await entity.async_turn_on()

    entity._pending_deadline = time.monotonic() - 1  # deadline already passed
    _report(manager, pins={LIGHTING_PIN: 0})

    assert entity.state == "off"
    assert entity._pending_state is None


@pytest.mark.asyncio
async def test_newest_command_wins_over_a_delayed_packet():
    """OFF -> user ON -> user OFF, with a stale ON packet in flight."""
    hass, manager, _client, entity = await _make_switch()
    _report(manager, pins={LIGHTING_PIN: 0})

    await entity.async_turn_on()
    await entity.async_turn_off()
    assert entity.state == "off"

    # The first command's reply arrives late; the newer OFF must win.
    _report(manager, pins={LIGHTING_PIN: 1})
    assert entity.state == "off"

    # The first command's timeout must not clear the second command either.
    assert entity._pending_state is False

    _report(manager, pins={LIGHTING_PIN: 0})
    assert entity.state == "off"
    assert entity._pending_state is None
    hass.fire_pending_timers()
    assert entity.state == "off"


@pytest.mark.asyncio
async def test_command_that_cannot_be_published_does_not_move_the_entity():
    """No optimistic state for a command that never reached FarmBot."""
    from homeassistant.exceptions import HomeAssistantError

    _hass, manager, client, entity = await _make_switch()
    _report(manager, pins={LIGHTING_PIN: 0})
    client.publish.return_value.rc = mqtt.MQTT_ERR_NO_CONN

    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()

    assert entity.state == "off"
    assert entity._pending_state is None

    # Same when the client is not connected at all.
    manager._mqtt_connected = False
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_off()
    assert entity._pending_state is None


@pytest.mark.asyncio
async def test_emergency_stop_switch_shares_the_reconciler():
    hass, manager, _client, _entity = await _make_switch()
    estop = FarmbotEmergencyStopSwitch(manager)
    estop.hass = hass
    estop.entity_id = "switch.farmbot_emergency_stop"
    await estop.async_added_to_hass()

    _report(manager, informational_settings={"locked": False})
    assert estop.state == "off"

    await estop.async_turn_on()
    assert estop.state == "on"

    _report(manager, informational_settings={"locked": False})  # stale
    assert estop.state == "on"

    _report(manager, informational_settings={"locked": True})
    assert estop.state == "on"
    assert estop._pending_state is None


# --------------------------------------------------------------------------
# Lighting availability
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_update_without_pins_keeps_the_light_available_and_on():
    """The regression that made a working light go unavailable/off."""
    _hass, manager, _client, entity = await _make_switch()
    _report(manager, pins={LIGHTING_PIN: 1})
    assert entity.state == "on"

    # A perfectly valid FarmBot update that simply does not mention pins.
    _report(manager, location_data={"position": {"x": 1, "y": 2, "z": 3}})

    assert entity.available is True
    assert entity.state == "on"
    assert manager.pin_is_on(LIGHTING_PIN) is True
    assert manager.status["location_data"]["position"]["x"] == 1


@pytest.mark.asyncio
async def test_pins_payload_naming_other_pins_keeps_this_pin():
    _hass, manager, _client, entity = await _make_switch()
    _report(manager, pins={LIGHTING_PIN: 1})
    _report(manager, pins={8: 1})  # only the water pin changed

    assert entity.state == "on"
    assert manager.pin_is_on(8) is True


@pytest.mark.asyncio
async def test_disconnection_makes_the_light_unavailable_and_reconnection_restores_it():
    _hass, manager, client, entity = await _make_switch()
    _report(manager, pins={LIGHTING_PIN: 1})
    assert entity.available is True

    manager._on_disconnect(client, None, None, 7)
    assert entity.available is False
    assert entity.state == "unavailable"

    manager._on_connect(client, None, {}, 0, None)
    assert entity.available is True
    assert entity.state == "on"  # the last confirmed value survived


@pytest.mark.asyncio
async def test_a_silent_bot_eventually_goes_unavailable():
    """Broker connected but the device stopped talking."""
    _hass, manager, _client, entity = await _make_switch()
    _report(manager, pins={LIGHTING_PIN: 1})
    assert entity.available is True

    manager._last_status_at = time.monotonic() - DEVICE_STATE_STALE_SECONDS - 1
    assert entity.available is False

    _report(manager, pins={LIGHTING_PIN: 1})
    assert entity.available is True


@pytest.mark.asyncio
async def test_cloud_api_failure_does_not_affect_mqtt_backed_entities():
    """Cloud REST health and realtime device state are independent."""
    from custom_components.farmbot.api import FarmbotApiError

    _hass, manager, _client, entity = await _make_switch()
    _report(manager, pins={LIGHTING_PIN: 1})

    manager._note_vision_poll_failure(FarmbotApiError("Cannot connect to host my.farm.bot:443"))

    assert entity.available is True
    assert entity.state == "on"


@pytest.mark.asyncio
async def test_unreported_pin_is_unknown_rather_than_off_or_unavailable():
    _hass, manager, _client, entity = await _make_switch()
    _report(manager, location_data={"position": {"x": 0}})

    assert entity.available is True
    assert entity.state == "unknown"


# --------------------------------------------------------------------------
# Thread safety
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_update_from_a_worker_thread_is_marshalled_onto_the_loop():
    """A FarmBot callback on the MQTT thread must not touch entity state."""
    hass, manager, _client, entity = await _make_switch()
    errors = []

    def mqtt_thread():
        try:
            _report(manager, pins={LIGHTING_PIN: 1})
        except BaseException as err:  # noqa: BLE001 - re-raised on the main thread
            errors.append(err)

    worker = threading.Thread(target=mqtt_thread)
    worker.start()
    worker.join()

    assert not errors, errors
    # The state was merged on the MQTT thread, but nothing was dispatched
    # there: the dispatch is waiting to run on the event loop.
    assert manager.pin_is_on(LIGHTING_PIN) is True
    assert entity.write_count == 0
    assert len(hass.loop.pending) == 1

    hass.loop.run_pending()  # i.e. the event loop gets to it
    assert entity.state == "on"
    assert entity.write_count == 1


def test_every_dispatcher_target_is_marked_as_a_callback():
    """Undecorated dispatcher targets are run in an executor thread by Core.

    ``HassJob`` classifies a plain synchronous callable as
    ``HassJobType.Executor``, which is how ``async_write_ha_state`` ended up
    being called off the event loop. Anything connected to a FarmBot signal
    has to carry the ``@callback`` marker.
    """
    from custom_components.farmbot import binary_sensor, button, sensor, switch

    targets = [
        switch.FarmbotPeripheralSwitch._handle_state,
        switch.FarmbotEmergencyStopSwitch._handle_state,
        binary_sensor._FarmbotInformationalBinarySensor._update_from_state,
        binary_sensor.FarmbotVisionAvailableBinarySensor._handle_update,
        binary_sensor.FarmbotVisionAvailableBinarySensor._handle_timeout_check,
        sensor.FarmbotLastButtonInputSensor._handle_update,
        sensor._FarmbotVisionSensor._handle_update,
        button.FarmbotLaunchSelectedSequenceButton._update_selected,
    ]
    unmarked = [t.__qualname__ for t in targets if not getattr(t, "_hass_callback", False)]
    assert not unmarked, f"dispatcher targets missing @callback: {unmarked}"


@pytest.mark.asyncio
async def test_an_unmarked_dispatcher_target_would_be_caught():
    """Guards the guard: the stubs really do reproduce the original failure."""
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    from custom_components.farmbot.const import SIGNAL_STATE

    hass, manager, _client, entity = await _make_switch()

    def unmarked_handler(_status=None):  # deliberately missing @callback
        entity.async_write_ha_state()

    async_dispatcher_connect(hass, SIGNAL_STATE, unmarked_handler)

    with pytest.raises(RuntimeError, match="thread other than the event loop"):
        _report(manager, pins={LIGHTING_PIN: 1})
