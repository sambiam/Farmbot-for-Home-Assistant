"""Supervised positioning: target resolution, bounds, busy gating and arrival.

A positioning move is an ordinary FarmBot OS ``move``, so the value this layer
adds is entirely in what it refuses and in what it insists on observing before
reporting success. These tests cover exactly that.
"""

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntry

from custom_components.farmbot.manager import FarmbotManager

from .helpers import FakeHass

# 2000 mm x 1000 mm x 500 mm of travel, with Z homing upwards (so Z is
# negative-down, which is the stock FarmBot arrangement).
FIRMWARE = {
    "movement_axis_nr_steps_x": 200000,
    "movement_axis_nr_steps_y": 100000,
    "movement_axis_nr_steps_z": 50000,
    "movement_step_per_mm_x": 100,
    "movement_step_per_mm_y": 100,
    "movement_step_per_mm_z": 100,
    "movement_home_up_z": 1,
}


def _make_manager(position=None, *, busy=False, locked=False, connected=True):
    hass = FakeHass()
    entry = ConfigEntry(
        entry_id="entry-1",
        unique_id="42",
        domain="farmbot",
        data={"token": "tok", "device_id": 42, "mqtt_host": "mqtt.example.com"},
        options={},
    )
    manager = FarmbotManager(hass, "tok", "42", "mqtt.example.com", entry=entry)
    manager._mqtt_connected = connected
    manager._mqtt = object() if connected else None
    manager.status = {
        "location_data": {"position": position or {"x": 100.0, "y": 200.0, "z": -50.0}},
        "informational_settings": {"busy": busy, "locked": locked},
    }
    return manager


def _install_rpc(manager, calls, *, arrives_at=None):
    """Acknowledge every RPC, and let ``read_status`` report an arrival."""

    async def fake_rpc(commands, **_kwargs):
        calls.append(commands)
        kinds = {command.get("kind") for command in commands}
        if "read_status" in kinds and arrives_at is not None:
            manager.status = {
                **manager.status,
                "location_data": {"position": dict(arrives_at)},
            }
            manager._status_revision += 1
        return {"kind": "rpc_ok"}

    manager.async_rpc_request = fake_rpc


def _run(manager, **kwargs):
    defaults = {
        "x": None,
        "y": None,
        "z": None,
        "relative": False,
        "safe_z": True,
        "speed": 100,
        "firmware_config": FIRMWARE,
    }
    return manager.start_move(**{**defaults, **kwargs})


# --------------------------------------------------------------- target maths


def _resolve(manager, **requested):
    """Resolve a target the way ``start_move`` does, without spawning its task."""
    relative = requested.pop("relative", False)
    bounds = manager.soil_motion_state(FIRMWARE)["axis_bounds"]
    return manager._resolve_move_target(
        requested={axis: requested.get(axis) for axis in ("x", "y", "z")},
        relative=relative,
        bounds=bounds,
    )


def test_an_omitted_axis_keeps_its_current_value():
    manager = _make_manager({"x": 100.0, "y": 200.0, "z": -50.0})
    assert _resolve(manager, x=900) == {"x": 900.0, "y": 200.0, "z": -50.0}


def test_a_relative_move_offsets_each_supplied_axis_from_the_live_position():
    manager = _make_manager({"x": 100.0, "y": 200.0, "z": -50.0})
    assert _resolve(manager, y=-150, relative=True) == {"x": 100.0, "y": 50.0, "z": -50.0}


def test_a_move_without_a_reported_position_is_refused_rather_than_guessed():
    manager = _make_manager()
    manager.status = {**manager.status, "location_data": {"position": {}}}
    with pytest.raises(ValueError, match="has not reported a position"):
        _run(manager, x=500)


# ----------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("axis", "value"),
    [("x", 2500.0), ("x", -1.0), ("y", 1200.0), ("z", 10.0), ("z", -600.0)],
)
def test_a_target_outside_the_axis_bounds_is_refused(axis, value):
    manager = _make_manager()
    with pytest.raises(ValueError, match="outside FarmBot bounds"):
        _run(manager, **{axis: value})


def test_a_relative_move_that_would_leave_the_bed_is_refused():
    manager = _make_manager({"x": 100.0, "y": 200.0, "z": -50.0})
    with pytest.raises(ValueError, match="outside FarmBot bounds"):
        _run(manager, x=-500, relative=True)


def test_a_non_finite_target_is_refused():
    manager = _make_manager()
    with pytest.raises(ValueError, match="finite"):
        _run(manager, x=float("inf"))


def test_a_disconnected_or_locked_bot_is_refused():
    with pytest.raises(ValueError, match="not connected"):
        _run(_make_manager(connected=False), x=500)
    with pytest.raises(ValueError, match="emergency-stopped"):
        _run(_make_manager(locked=True), x=500)


def test_a_busy_bot_is_refused():
    with pytest.raises(ValueError, match="busy"):
        _run(_make_manager(busy=True), x=500)


def test_a_move_blocks_a_second_move_and_a_grid_repair():
    async def scenario():
        manager = _make_manager()
        _install_rpc(manager, [], arrives_at=None)
        _run(manager, x=500)
        with pytest.raises(ValueError, match="busy"):
            _run(manager, x=600)
        with pytest.raises(ValueError, match="busy"):
            manager.start_grid_repair(
                targets=[{"x": 500.0, "y": 500.0, "z": -50.0}], firmware_config=FIRMWARE
            )
        for task in list(manager._move_tasks):
            task.cancel()
        await asyncio.gather(*manager._move_tasks, return_exceptions=True)

    asyncio.run(scenario())


# ------------------------------------------------------------------- the run


def test_a_completed_move_sends_one_safe_z_move_and_confirms_arrival():
    async def scenario():
        manager = _make_manager()
        calls = []
        _install_rpc(manager, calls, arrives_at={"x": 900.0, "y": 200.0, "z": -50.0})

        move_id, _target = _run(manager, x=900)
        await asyncio.gather(*manager._move_tasks)
        record = manager.move(move_id)

        assert record["status"] == "complete"
        assert record["reported_position"] == {"x": 900.0, "y": 200.0, "z": -50.0}
        move = calls[0][0]
        assert move["kind"] == "move"
        assert any(item["kind"] == "safe_z" for item in move["body"])
        overwrites = {
            item["args"]["axis"]: item["args"]["axis_operand"]["args"]["number"]
            for item in move["body"]
            if item["kind"] == "axis_overwrite"
        }
        assert overwrites == {"x": 900.0, "y": 200.0, "z": -50.0}

    asyncio.run(scenario())


def test_safe_z_can_be_declined():
    async def scenario():
        manager = _make_manager()
        calls = []
        _install_rpc(manager, calls, arrives_at={"x": 900.0, "y": 200.0, "z": -50.0})
        _run(manager, x=900, safe_z=False)
        await asyncio.gather(*manager._move_tasks)
        assert not any(item["kind"] == "safe_z" for item in calls[0][0]["body"])

    asyncio.run(scenario())


def test_an_accepted_rpc_that_never_arrives_is_reported_as_failed(monkeypatch):
    """An ``rpc_ok`` only proves the command was queued, never that it worked."""

    async def scenario():
        manager = _make_manager()
        _install_rpc(manager, [], arrives_at={"x": 100.0, "y": 200.0, "z": -50.0})
        monkeypatch.setattr(
            "custom_components.farmbot.manager.MOVE_POSITION_TIMEOUT_SECONDS", 0.05
        )
        move_id, _target = _run(manager, x=900)
        await asyncio.gather(*manager._move_tasks)
        record = manager.move(move_id)
        assert record["status"] == "failed"
        assert "did not reach" in record["message"]

    asyncio.run(scenario())


def test_a_failing_move_rpc_is_reported_rather_than_raised():
    async def scenario():
        manager = _make_manager()

        async def failing_rpc(_commands, **_kwargs):
            raise RuntimeError("MQTT publish failed")

        manager.async_rpc_request = failing_rpc
        move_id, _target = _run(manager, x=900)
        await asyncio.gather(*manager._move_tasks)
        record = manager.move(move_id)
        assert record["status"] == "failed"
        assert "MQTT publish failed" in record["message"]

    asyncio.run(scenario())


def test_an_unknown_move_id_has_no_record():
    assert _make_manager().move("no-such-move") is None


# ----------------------------------------------------------- peripheral pins


def test_a_peripheral_write_is_an_acknowledged_write_pin():
    async def scenario():
        manager = _make_manager()
        calls = []
        _install_rpc(manager, calls)
        await manager.async_set_peripheral(pin=7, value=True)
        assert calls == [
            [{"kind": "write_pin", "args": {"pin_number": 7, "pin_value": 1, "pin_mode": 0}}]
        ]

    asyncio.run(scenario())


def test_a_pin_outside_the_switchable_set_is_refused():
    async def scenario():
        manager = _make_manager()
        _install_rpc(manager, [])
        with pytest.raises(ValueError, match="not a switchable"):
            await manager.async_set_peripheral(pin=13, value=True)

    asyncio.run(scenario())


def test_peripheral_state_reports_unknown_pins_as_none_not_off():
    manager = _make_manager()
    manager.status = {**manager.status, "pins": {"7": {"value": 1}}}
    peripherals = manager.positioning_peripherals()
    assert peripherals["7"] is True
    assert peripherals["9"] is None


# -------------------------------------------------------- motion config cache


def test_motion_config_is_cached_so_a_polling_ui_costs_no_rest_traffic():
    async def scenario():
        manager = _make_manager()
        fetches = []

        async def fake_config():
            fetches.append(1)
            return FIRMWARE

        manager.api.async_get_firmware_config = fake_config
        assert await manager.async_motion_config() == FIRMWARE
        await manager.async_motion_config()
        await manager.async_motion_config()
        assert len(fetches) == 1

    asyncio.run(scenario())


def test_a_failed_refresh_keeps_the_previous_config_rather_than_losing_bounds():
    async def scenario():
        from custom_components.farmbot.api import FarmbotApiError

        manager = _make_manager()
        manager._motion_config = FIRMWARE
        manager._motion_config_at = 0.0  # stale, so a refresh is attempted

        async def failing_config():
            raise FarmbotApiError("FarmBot API unreachable")

        manager.api.async_get_firmware_config = failing_config
        assert await manager.async_motion_config() == FIRMWARE

    asyncio.run(scenario())
