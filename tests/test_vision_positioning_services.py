"""The positioning service handlers, called through the real service registry.

These cover the boundary the Vision app actually talks to: schema validation,
the shape of each response, and the rejection paths that keep a low-priority
grow-light or park move from ever taking a busy bot.
"""

import asyncio
from pathlib import Path

import pytest
import voluptuous as vol
import yaml
from homeassistant.config_entries import ConfigEntry

from custom_components.farmbot import (
    DOMAIN,
    SERVICE_GET_VISION_MOTION,
    SERVICE_GET_VISION_MOVE,
    SERVICE_SET_VISION_PERIPHERAL,
    SERVICE_START_VISION_MOVE,
    _async_register_services,
    _async_remove_services_if_last_entry,
)
from custom_components.farmbot.const import POSITIONING_PERIPHERAL_PINS, VISION_CAPABILITIES
from custom_components.farmbot.manager import FarmbotManager

from .fake_api import FakeVisionApi
from .helpers import FakeHass

# FakeVisionApi's default firmware config: 6000 x 3000 x 1200 mm of travel.
IN_BOUNDS = {"x": 900.0, "y": 800.0, "z": -100.0}


def _make_bot(hass, *, position=None, busy=False, locked=False, connected=True):
    entry = ConfigEntry(
        entry_id="entry-1",
        unique_id="42",
        domain="farmbot",
        data={"token": "tok", "device_id": "42", "mqtt_host": "mqtt.example.com"},
        options={},
    )
    manager = FarmbotManager(hass, "tok", "42", "mqtt.example.com", entry=entry)
    manager.api = FakeVisionApi(reauth_callback=manager._trigger_reauth_from_async)
    manager._mqtt_connected = connected
    manager._mqtt = object() if connected else None
    manager.status = {
        "location_data": {"position": position or {"x": 100.0, "y": 200.0, "z": -50.0}},
        "informational_settings": {"busy": busy, "locked": locked},
        "pins": {"7": {"value": 1}},
    }

    async def fake_rpc(commands, **_kwargs):
        """Acknowledge every RPC and let the bot 'arrive' where it was sent.

        Without the arrival the background move task would legitimately wait
        out its whole position timeout, so a test that drains it would hang.
        """
        manager.sent_rpcs.append(commands)
        for command in commands:
            if command.get("kind") == "move":
                manager.requested_target = {
                    item["args"]["axis"]: item["args"]["axis_operand"]["args"]["number"]
                    for item in command.get("body", [])
                    if item["kind"] == "axis_overwrite"
                }
            elif command.get("kind") == "read_status" and manager.requested_target:
                manager.status = {
                    **manager.status,
                    "location_data": {"position": dict(manager.requested_target)},
                }
                manager._status_revision += 1
        return {"kind": "rpc_ok"}

    manager.sent_rpcs = []
    manager.requested_target = None
    manager.async_rpc_request = fake_rpc
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager
    return manager


def _call(hass, service, data):
    return hass.services.async_call(DOMAIN, service, data)


def _ready_hass(**kwargs):
    hass = FakeHass()
    manager = _make_bot(hass, **kwargs)
    _async_register_services(hass)
    return hass, manager


async def _drain(manager):
    if manager._move_tasks:
        await asyncio.gather(*manager._move_tasks, return_exceptions=True)


# ------------------------------------------------------------- registration


def test_the_positioning_services_are_registered_and_removed_with_the_last_entry():
    hass, _manager = _ready_hass()
    services = (
        SERVICE_GET_VISION_MOTION,
        SERVICE_START_VISION_MOVE,
        SERVICE_GET_VISION_MOVE,
        SERVICE_SET_VISION_PERIPHERAL,
    )
    for service in services:
        assert hass.services.has_service(DOMAIN, service)
    del hass.data[DOMAIN]["entry-1"]
    _async_remove_services_if_last_entry(hass)
    for service in services:
        assert not hass.services.has_service(DOMAIN, service)


def test_positioning_is_advertised_as_a_capability():
    assert "vision_positioning" in VISION_CAPABILITIES


def test_every_positioning_service_is_documented_in_services_yaml():
    """Home Assistant renders the UI from this file, so drift is user-visible."""
    path = Path(__file__).resolve().parents[1] / "custom_components/farmbot/services.yaml"
    documented = yaml.safe_load(path.read_text(encoding="utf-8"))
    for service in (
        SERVICE_GET_VISION_MOTION,
        SERVICE_START_VISION_MOVE,
        SERVICE_GET_VISION_MOVE,
        SERVICE_SET_VISION_PERIPHERAL,
    ):
        assert service in documented


# ------------------------------------------------------------ motion state


def test_motion_state_returns_position_bounds_and_peripherals():
    async def scenario():
        hass, _manager = _ready_hass()
        result = await _call(hass, SERVICE_GET_VISION_MOTION, {"config_entry_id": "entry-1"})
        assert result["motion"]["position"] == {"x": 100.0, "y": 200.0, "z": -50.0}
        assert result["motion"]["connected"] is True
        assert result["motion"]["axis_bounds"]["x"] == [0.0, 6000.0]
        assert result["peripherals"]["7"] is True
        # A pin FarmBot has never reported is unknown, not off.
        assert result["peripherals"]["9"] is None

    asyncio.run(scenario())


def test_motion_state_reuses_the_cached_firmware_config_across_polls():
    async def scenario():
        hass, manager = _ready_hass()
        for _ in range(4):
            await _call(hass, SERVICE_GET_VISION_MOTION, {"config_entry_id": "entry-1"})
        assert manager.api.calls.count("async_get_firmware_config") == 1

    asyncio.run(scenario())


# ------------------------------------------------------------------- moving


def test_a_move_is_queued_and_reports_its_resolved_target():
    async def scenario():
        hass, manager = _ready_hass()
        result = await _call(
            hass, SERVICE_START_VISION_MOVE, {"config_entry_id": "entry-1", **IN_BOUNDS}
        )
        assert result["status"] == "queued"
        assert result["target"] == IN_BOUNDS
        status = await _call(
            hass,
            SERVICE_GET_VISION_MOVE,
            {"config_entry_id": "entry-1", "move_id": result["move_id"]},
        )
        assert status["target"] == IN_BOUNDS
        await _drain(manager)

    asyncio.run(scenario())


def test_a_move_needs_at_least_one_axis():
    async def scenario():
        hass, _manager = _ready_hass()
        with pytest.raises(vol.Invalid):
            await _call(hass, SERVICE_START_VISION_MOVE, {"config_entry_id": "entry-1"})

    asyncio.run(scenario())


def test_an_out_of_bounds_move_is_rejected_with_a_reason_rather_than_queued():
    async def scenario():
        hass, manager = _ready_hass()
        result = await _call(
            hass, SERVICE_START_VISION_MOVE, {"config_entry_id": "entry-1", "x": 99999}
        )
        assert result["status"] == "rejected"
        assert "outside FarmBot bounds" in result["message"]
        assert manager.sent_rpcs == []

    asyncio.run(scenario())


def test_a_busy_bot_rejects_the_move_so_a_low_priority_caller_can_retry():
    async def scenario():
        hass, manager = _ready_hass(busy=True)
        result = await _call(
            hass, SERVICE_START_VISION_MOVE, {"config_entry_id": "entry-1", **IN_BOUNDS}
        )
        assert result["status"] == "rejected"
        assert result["message"] == "FarmBot is busy"
        assert manager.sent_rpcs == []

    asyncio.run(scenario())


def test_an_emergency_stopped_bot_rejects_the_move():
    async def scenario():
        hass, _manager = _ready_hass(locked=True)
        result = await _call(
            hass, SERVICE_START_VISION_MOVE, {"config_entry_id": "entry-1", **IN_BOUNDS}
        )
        assert result["status"] == "rejected"
        assert "emergency-stopped" in result["message"]

    asyncio.run(scenario())


def test_an_unknown_move_id_reports_failed_rather_than_raising():
    async def scenario():
        hass, _manager = _ready_hass()
        result = await _call(
            hass,
            SERVICE_GET_VISION_MOVE,
            {"config_entry_id": "entry-1", "move_id": "11111111-1111-4111-8111-111111111111"},
        )
        assert result["status"] == "failed"
        assert "not found" in result["message"]

    asyncio.run(scenario())


def test_a_move_id_must_be_a_uuid():
    async def scenario():
        hass, _manager = _ready_hass()
        with pytest.raises(vol.Invalid):
            await _call(
                hass, SERVICE_GET_VISION_MOVE, {"config_entry_id": "entry-1", "move_id": "nope"}
            )

    asyncio.run(scenario())


# --------------------------------------------------------------- peripherals


def test_switching_the_grow_light_writes_the_pin():
    async def scenario():
        hass, manager = _ready_hass()
        result = await _call(
            hass,
            SERVICE_SET_VISION_PERIPHERAL,
            {"config_entry_id": "entry-1", "pin": 7, "value": True},
        )
        assert result == {
            "status": "applied",
            "pin": 7,
            "value": True,
            "message": "Pin 7 switched on",
        }
        assert manager.sent_rpcs == [
            [{"kind": "write_pin", "args": {"pin_number": 7, "pin_value": 1, "pin_mode": 0}}]
        ]

    asyncio.run(scenario())


def test_only_the_switchable_peripheral_pins_are_accepted():
    async def scenario():
        hass, _manager = _ready_hass()
        for pin in POSITIONING_PERIPHERAL_PINS:
            result = await _call(
                hass,
                SERVICE_SET_VISION_PERIPHERAL,
                {"config_entry_id": "entry-1", "pin": pin, "value": False},
            )
            assert result["status"] == "applied"
        # Not a general-purpose write-any-pin escape hatch.
        with pytest.raises(vol.Invalid):
            await _call(
                hass,
                SERVICE_SET_VISION_PERIPHERAL,
                {"config_entry_id": "entry-1", "pin": 13, "value": True},
            )

    asyncio.run(scenario())


def test_a_disconnected_bot_rejects_a_peripheral_write():
    async def scenario():
        hass, manager = _ready_hass(connected=False)
        result = await _call(
            hass,
            SERVICE_SET_VISION_PERIPHERAL,
            {"config_entry_id": "entry-1", "pin": 7, "value": True},
        )
        assert result["status"] == "rejected"
        assert "not connected" in result["message"]
        assert manager.sent_rpcs == []

    asyncio.run(scenario())
