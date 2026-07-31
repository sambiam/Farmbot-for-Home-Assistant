"""The raw G-code run lifecycle in FarmbotManager.

Everything on this path bypasses FarmBot OS's motion planning, so the run has
to be conservative in ways the CeleryScript paths do not: it must refuse to
start on a disconnected, locked or busy bot, must validate the entire program
before publishing the first chunk, and must hand the return trip back to
FarmBot OS's supervised movement.
"""

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntry

from custom_components.farmbot.gcode import GcodeError
from custom_components.farmbot.manager import FarmbotManager

from .helpers import FakeHass

FIRMWARE = {
    "movement_axis_nr_steps_x": 200000,
    "movement_axis_nr_steps_y": 100000,
    "movement_axis_nr_steps_z": 50000,
    "movement_step_per_mm_x": 100,
    "movement_step_per_mm_y": 100,
    "movement_step_per_mm_z": 100,
    "movement_max_spd_x": 1000,
    "movement_max_spd_y": 1000,
    "movement_max_spd_z": 500,
    "movement_home_up_z": 1,
}

# A square, drawn the only way the firmware allows: absolute G00 segments.
SQUARE = [
    "G21",
    "G90",
    "G00 X400 Y400 F600",
    "G00 X600 Y400",
    "G00 X600 Y600",
    "G00 X400 Y600",
    "G00 X400 Y400",
]


def _make_manager():
    hass = FakeHass()
    entry = ConfigEntry(
        entry_id="entry-1",
        unique_id="42",
        domain="farmbot",
        data={"token": "tok", "device_id": 42, "mqtt_host": "mqtt.example.com"},
        options={},
    )
    manager = FarmbotManager(hass, "tok", "42", "mqtt.example.com", entry=entry)
    manager._mqtt_connected = True
    manager._mqtt = object()
    manager.status = {
        "location_data": {"position": {"x": 100.0, "y": 100.0, "z": 0.0}},
        "informational_settings": {"busy": False, "locked": False},
    }
    return manager


def _install_rpc(manager, calls):
    async def fake_rpc(commands, **_kwargs):
        calls.append(commands)
        return {"kind": "rpc_ok"}

    manager.async_rpc_request = fake_rpc


def test_a_run_publishes_lua_gcode_then_restores_position():
    async def scenario():
        manager = _make_manager()
        calls = []
        _install_rpc(manager, calls)

        run_id = manager.start_gcode_run(
            lines=SQUARE, firmware_config=FIRMWARE, feed_mm_per_min=600
        )
        await asyncio.gather(*manager._gcode_tasks)
        run = manager.gcode_run(run_id)

        assert run["status"] == "complete"
        assert run["moves"] == 5
        assert run["chunks_sent"] == run["chunks_total"] == 1

        # One Lua node carrying raw firmware G-code...
        assert calls[0][0]["kind"] == "lua"
        lua = calls[0][0]["args"]["lua"]
        assert lua.count('gcode("G00"') == 5
        assert "X = 600" in lua and "Y = 600" in lua

        # ...then the return trip through FarmBot OS's own planner, with safe Z.
        assert calls[-1][0]["kind"] == "move"
        assert any(item["kind"] == "safe_z" for item in calls[-1][0]["body"])

    asyncio.run(scenario())


def test_return_to_start_can_be_declined():
    async def scenario():
        manager = _make_manager()
        calls = []
        _install_rpc(manager, calls)

        manager.start_gcode_run(
            lines=SQUARE,
            firmware_config=FIRMWARE,
            feed_mm_per_min=600,
            return_to_start=False,
        )
        await asyncio.gather(*manager._gcode_tasks)

        assert [command[0]["kind"] for command in calls] == ["lua"]

    asyncio.run(scenario())


def test_a_long_program_is_split_into_bounded_chunks():
    """`gcode()` blocks until the firmware answers, so one node must not hold it all."""

    async def scenario():
        manager = _make_manager()
        calls = []
        _install_rpc(manager, calls)
        lines = ["G90"] + [f"G00 X{400 + n}" for n in range(50)]

        run_id = manager.start_gcode_run(
            lines=lines, firmware_config=FIRMWARE, feed_mm_per_min=600
        )
        await asyncio.gather(*manager._gcode_tasks)
        run = manager.gcode_run(run_id)

        lua_calls = [command for command in calls if command[0]["kind"] == "lua"]
        assert len(lua_calls) == 3
        assert run["chunks_total"] == 3
        assert run["chunks_sent"] == 3
        assert run["status"] == "complete"

    asyncio.run(scenario())


def test_an_emergency_stop_partway_through_stops_the_run():
    async def scenario():
        manager = _make_manager()
        calls = []

        async def fake_rpc(commands, **_kwargs):
            calls.append(commands)
            if len(calls) == 1:
                manager.status["informational_settings"]["locked"] = True
            return {"kind": "rpc_ok"}

        manager.async_rpc_request = fake_rpc
        lines = ["G90"] + [f"G00 X{400 + n}" for n in range(50)]

        run_id = manager.start_gcode_run(
            lines=lines, firmware_config=FIRMWARE, feed_mm_per_min=600
        )
        await asyncio.gather(*manager._gcode_tasks)
        run = manager.gcode_run(run_id)

        assert run["status"] == "failed"
        assert "emergency-stopped" in run["message"]
        assert run["chunks_sent"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda m: setattr(m, "_mqtt_connected", False), "not connected"),
        (
            lambda m: m.status["informational_settings"].update(locked=True),
            "emergency-stopped",
        ),
        (lambda m: m.status["informational_settings"].update(busy=True), "busy"),
    ],
)
def test_a_run_is_refused_on_an_unavailable_bot(mutate, expected):
    manager = _make_manager()
    _install_rpc(manager, [])
    mutate(manager)

    with pytest.raises(GcodeError, match=expected):
        manager.start_gcode_run(lines=SQUARE, firmware_config=FIRMWARE, feed_mm_per_min=600)


def test_an_out_of_bounds_program_sends_nothing_at_all():
    """Rejection is all-or-nothing: a bad line must not strand the gantry."""

    async def scenario():
        manager = _make_manager()
        calls = []
        _install_rpc(manager, calls)

        with pytest.raises(GcodeError, match="outside the"):
            manager.start_gcode_run(
                lines=["G90", "G00 X400", "G00 X99999"],
                firmware_config=FIRMWARE,
                feed_mm_per_min=600,
            )

        assert calls == []
        assert manager.gcode_runs == {}

    asyncio.run(scenario())


def test_plan_gcode_reports_the_program_without_moving():
    manager = _make_manager()
    calls = []
    _install_rpc(manager, calls)

    program, _state = manager.plan_gcode(
        lines=SQUARE, firmware_config=FIRMWARE, feed_mm_per_min=600
    )

    assert len(program.moves) == 5
    assert program.extent()["x"] == (100.0, 600.0)
    assert calls == []
    assert manager.gcode_runs == {}


def test_a_second_run_is_refused_while_one_is_in_flight():
    async def scenario():
        manager = _make_manager()
        release = asyncio.Event()

        async def fake_rpc(_commands, **_kwargs):
            await release.wait()
            return {"kind": "rpc_ok"}

        manager.async_rpc_request = fake_rpc
        manager.start_gcode_run(lines=SQUARE, firmware_config=FIRMWARE, feed_mm_per_min=600)
        await asyncio.sleep(0)

        with pytest.raises(GcodeError, match="busy"):
            manager.start_gcode_run(lines=SQUARE, firmware_config=FIRMWARE, feed_mm_per_min=600)

        release.set()
        await asyncio.gather(*manager._gcode_tasks)

    asyncio.run(scenario())
