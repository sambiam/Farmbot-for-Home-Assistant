"""A whole photo-grid route runs as one continuous capture.

Before 2.5.0 the service schema capped a call at twelve targets, so a 77-cell
bed grid arrived as seven separate runs. Each run switched the lighting on,
drove in from the staging position, photographed twelve cells, switched the
lighting off and drove back out again -- six pointless round trips, six
lighting cycles and rows cut in half. These tests pin the run-level lifecycle.
"""

import asyncio

from homeassistant.config_entries import ConfigEntry

from custom_components.farmbot.const import (
    GRID_REPAIR_FLAT_TRAVEL_TOP_MARGIN_MM,
    GRID_REPAIR_MAX_TARGETS_PER_CALL,
)
from custom_components.farmbot.manager import FarmbotManager

from .helpers import FakeHass

BED_COLUMNS = 11
BED_ROWS = 7
X_SPACING = 373.0
Y_SPACING = 294.0
CAPTURE_Z = -1.0

# A home-up Z axis: bounds are [-1000, 0], so the top of travel is 0 and the
# capture height sits 1 mm below it.
FIRMWARE = {
    "movement_axis_nr_steps_x": 600000,
    "movement_axis_nr_steps_y": 300000,
    "movement_axis_nr_steps_z": 100000,
    "movement_step_per_mm_x": 100,
    "movement_step_per_mm_y": 100,
    "movement_step_per_mm_z": 100,
    "movement_home_up_z": 1,
}


def _run(coro):
    return asyncio.run(coro)


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
        "location_data": {"position": {"x": 310.0, "y": 40.0, "z": 0.0}},
        "informational_settings": {"busy": False, "locked": False},
    }
    return manager


def _bed_route():
    """The canonical serpentine route for an 11 x 7 bed."""
    targets = []
    for index in range(BED_COLUMNS * BED_ROWS):
        row, position = divmod(index, BED_COLUMNS)
        column = position if row % 2 == 0 else BED_COLUMNS - 1 - position
        targets.append(
            {
                "x": 235.0 + X_SPACING * column,
                "y": 195.0 + Y_SPACING * row,
                "z": CAPTURE_Z,
                "index": index,
            }
        )
    return targets


def _install_fakes(manager, calls):
    async def fake_rpc(commands, **_kwargs):
        calls.append(commands)
        return {"kind": "rpc_ok"}

    async def fake_images():
        return []

    captured = {"count": 0}

    async def fake_wait_for_grid_image(**kwargs):
        captured["count"] += 1
        target = kwargs["target"]
        return {
            "image_id": captured["count"],
            "x": target["x"],
            "y": target["y"],
            "z": target["z"],
            "distance_from_target_mm": 0,
        }

    async def fake_wait_for_grid_position(**kwargs):
        return {axis: float(kwargs["target"][axis]) for axis in ("x", "y", "z")}

    manager.async_rpc_request = fake_rpc
    manager.api.async_get_images = fake_images
    manager._wait_for_grid_image = fake_wait_for_grid_image
    manager._wait_for_grid_position = fake_wait_for_grid_position


def _axis_targets(command):
    return {
        item["args"]["axis"]: item["args"]["axis_operand"]["args"]["number"]
        for item in command["body"]
        if item["kind"] == "axis_overwrite"
    }


def test_whole_bed_grid_runs_as_one_continuous_capture():
    async def scenario():
        manager = _make_manager()
        calls = []
        _install_fakes(manager, calls)
        targets = _bed_route()

        repair_id = manager.start_grid_repair(targets=targets, firmware_config=FIRMWARE)
        await asyncio.gather(*manager._grid_repair_tasks)
        repair = manager.grid_repair(repair_id)

        assert repair["status"] == "complete"
        # Exactly one capture per cell, credited by stable identity.
        assert len(repair["frames"]) == 77
        assert [frame["target_index"] for frame in repair["frames"]] == list(range(77))
        assert repair["failed_targets"] == []

        kinds = [[item["kind"] for item in command] for command in calls]
        # One lighting cycle for the whole run, not one per batch.
        assert kinds.count(["write_pin"]) == 2
        assert kinds[0] == ["write_pin"]
        assert calls[0][0]["args"]["pin_value"] == 1
        assert kinds[-2] == ["write_pin"]
        assert calls[-2][0]["args"]["pin_value"] == 0
        assert all(kind != ["write_pin"] for kind in kinds[1:-2])

        moves = [command[0] for command in calls if command[0]["kind"] == "move"]
        # 77 cell moves plus exactly one return to the staging position.
        assert len(moves) == 78
        assert _axis_targets(moves[-1]) == {"x": 310.0, "y": 40.0, "z": 0.0}
        assert kinds[-1] == ["move"]

        # The grid traversal itself never leaves the grid: every move between
        # the first and last cell is a planned cell, in route order.
        grid_moves = [_axis_targets(move) for move in moves[:-1]]
        assert grid_moves == [
            {"x": target["x"], "y": target["y"], "z": target["z"]} for target in targets
        ]

        legs = list(zip(grid_moves, grid_moves[1:]))
        lateral = [leg for leg in legs if leg[0]["y"] == leg[1]["y"]]
        transitions = [leg for leg in legs if leg[0]["y"] != leg[1]["y"]]
        assert len(lateral) == BED_ROWS * (BED_COLUMNS - 1)
        assert len(transitions) == BED_ROWS - 1
        assert all(abs(b["x"] - a["x"]) == X_SPACING for a, b in lateral)
        assert all(
            a["x"] == b["x"] and abs(b["y"] - a["y"]) == Y_SPACING for a, b in transitions
        )
        route = sum(abs(b["x"] - a["x"]) + abs(b["y"] - a["y"]) for a, b in legs)
        assert route == BED_ROWS * (BED_COLUMNS - 1) * X_SPACING + (BED_ROWS - 1) * Y_SPACING

        await manager.async_close()

    _run(scenario())


def test_flat_travel_keeps_the_camera_at_capture_height_across_the_grid():
    async def scenario():
        manager = _make_manager()
        calls = []
        _install_fakes(manager, calls)
        targets = _bed_route()

        manager.start_grid_repair(targets=targets, firmware_config=FIRMWARE)
        await asyncio.gather(*manager._grid_repair_tasks)

        moves = [command[0] for command in calls if command[0]["kind"] == "move"]
        retracts = [
            any(item["kind"] == "safe_z" for item in move["body"]) for move in moves
        ]
        # The drive in from the staging position must clear whatever is between
        # there and the first cell; so must the drive back out at the end.
        assert retracts[0] is True
        assert retracts[-1] is True
        # Everything in between stays at the capture Z, which is already within
        # a millimetre of the top of travel -- safe_z could not lift the gantry
        # any higher, so retracting and descending 76 more times buys nothing.
        assert not any(retracts[1:-1])
        assert all(_axis_targets(move)["z"] == CAPTURE_Z for move in moves[:-1])

        await manager.async_close()

    _run(scenario())


def test_a_low_capture_height_keeps_safe_z_on_every_move():
    async def scenario():
        manager = _make_manager()
        calls = []
        _install_fakes(manager, calls)
        # Well below the top of travel: lateral movement at this height could
        # meet an obstacle, so the retract stays on every single move.
        deep = -(GRID_REPAIR_FLAT_TRAVEL_TOP_MARGIN_MM + 100)
        targets = [dict(target, z=deep) for target in _bed_route()[:6]]

        manager.start_grid_repair(targets=targets, firmware_config=FIRMWARE)
        await asyncio.gather(*manager._grid_repair_tasks)

        moves = [command[0] for command in calls if command[0]["kind"] == "move"]
        assert len(moves) == 7
        assert all(any(item["kind"] == "safe_z" for item in move["body"]) for move in moves)

        await manager.async_close()

    _run(scenario())


def test_mixed_capture_heights_keep_safe_z_on_every_move():
    async def scenario():
        manager = _make_manager()
        calls = []
        _install_fakes(manager, calls)
        targets = _bed_route()[:4]
        targets[2] = dict(targets[2], z=CAPTURE_Z - 5)

        manager.start_grid_repair(targets=targets, firmware_config=FIRMWARE)
        await asyncio.gather(*manager._grid_repair_tasks)

        moves = [command[0] for command in calls if command[0]["kind"] == "move"]
        assert all(any(item["kind"] == "safe_z" for item in move["body"]) for move in moves)

        await manager.async_close()

    _run(scenario())


def test_lighting_is_restored_when_the_run_fails():
    async def scenario():
        manager = _make_manager()
        calls = []
        _install_fakes(manager, calls)

        async def never_arrives(**_kwargs):
            return None

        manager._wait_for_grid_position = never_arrives

        repair_id = manager.start_grid_repair(
            targets=_bed_route()[:3], firmware_config=FIRMWARE
        )
        await asyncio.gather(*manager._grid_repair_tasks)
        repair = manager.grid_repair(repair_id)

        assert repair["status"] == "failed"
        pin_writes = [
            command[0]["args"]["pin_value"]
            for command in calls
            if command[0]["kind"] == "write_pin"
        ]
        assert pin_writes == [1, 0]
        await manager.async_close()

    _run(scenario())


def test_an_aborted_run_names_the_cells_it_never_attempted():
    async def scenario():
        from custom_components.farmbot.const import GRID_REPAIR_MAX_CONSECUTIVE_FAILURES

        manager = _make_manager()
        calls = []
        _install_fakes(manager, calls)

        async def never_arrives(**_kwargs):
            return None

        manager._wait_for_grid_position = never_arrives
        targets = _bed_route()

        repair_id = manager.start_grid_repair(targets=targets, firmware_config=FIRMWARE)
        await asyncio.gather(*manager._grid_repair_tasks)
        repair = manager.grid_repair(repair_id)

        assert repair["status"] == "failed"
        attempted = GRID_REPAIR_MAX_CONSECUTIVE_FAILURES
        assert len(repair["failed_targets"]) == attempted
        assert [item["code"] for item in repair["failures"]] == ["movement"] * attempted
        # Everything after the abort is reported as unattempted, so the caller
        # can resume it without guessing.
        assert [item["index"] for item in repair["unattempted_targets"]] == list(
            range(attempted, len(targets))
        )
        await manager.async_close()

    _run(scenario())


def test_duplicate_target_indexes_are_rejected():
    manager = _make_manager()
    targets = _bed_route()[:3]
    targets[2] = dict(targets[2], index=0)
    try:
        manager.start_grid_repair(targets=targets, firmware_config=FIRMWARE)
    except ValueError as err:
        assert "unique" in str(err)
    else:  # pragma: no cover - the call must not be accepted
        raise AssertionError("duplicate indexes must be rejected")


def test_one_call_carries_a_whole_bed_grid():
    assert GRID_REPAIR_MAX_TARGETS_PER_CALL >= BED_COLUMNS * BED_ROWS
