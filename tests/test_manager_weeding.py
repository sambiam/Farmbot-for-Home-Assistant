"""Adaptive rotary weeding validation and current-recovery Lua."""

import pytest

from custom_components.farmbot.manager import FarmbotManager

SETTINGS = {
    "motor_pin": 2,
    "current_pin": 60,
    "max_load": 115,
    "tool_height_mm": 80,
    "max_attempts": 3,
    "cut_speed_percent": 50,
    "approach_speed_percent": 100,
    "height_step_mm": 10,
    "manage_tool": False,
    "verify_tool_on_mount": False,
    "verify_tool_on_unmount": False,
}
WEED = {
    "weed_id": 4,
    "transit_start": {"x": 400, "y": 400},
    "start": {"x": 450, "y": 500},
    "end": {"x": 550, "y": 500},
    "soil_z": -430,
    "travel_z": 0,
}


def test_lua_watches_current_reverses_slows_raises_and_always_retracts():
    source = FarmbotManager._weeding_lua(WEED, SETTINGS)
    assert "watch_pin(current" in source
    assert "off(motor)" in source
    assert "math.floor(cutspd/2)" in source
    assert "zoff=zoff+10" in source
    assert "fromx=tox" in source
    assert source.index("move({x=transitx,y=transity,z=safez") < source.index(
        "move({x=ax,y=ay,z=safez"
    )
    assert "move({x=fromx,y=fromy,z=safez" in source
    assert source.rstrip().endswith("move({x=bx,y=by,z=safez,speed=approach})")


def test_lua_only_slows_the_descent_below_the_soil_risk_height():
    source = FarmbotManager._weeding_lua(WEED, SETTINGS)
    assert "local riskz=-300" in source
    assert "if targetz < riskz then" in source
    assert "move({x=fromx,y=fromy,z=riskz,speed=approach})" in source
    assert "move({x=fromx,y=fromy,z=targetz,speed=25})" in source
    assert "move({x=fromx,y=fromy,z=targetz,speed=approach})" in source


def test_lua_uses_validated_tall_plant_approach_waypoints():
    source = FarmbotManager._weeding_lua(
        {**WEED, "approach_waypoints": [{"x": 300, "y": 350}, {"x": 400, "y": 400}]},
        SETTINGS,
    )
    first = "move({x=300,y=350,z=safez,speed=approach})"
    second = "move({x=400,y=400,z=safez,speed=approach})"
    assert source.index(first) < source.index(second) < source.index("move({x=ax,y=ay")


def test_weeding_plan_rejects_out_of_bounds_before_movement():
    manager = object.__new__(FarmbotManager)
    manager.soil_motion_state = lambda _firmware: {
        "connected": True,
        "locked": False,
        "busy": False,
        "position": {"x": 0, "y": 0, "z": 0},
        "axis_bounds": {"x": (0, 1000), "y": (0, 1000), "z": (-500, 0)},
    }
    invalid = {
        **WEED,
        "start": {"x": 900, "y": 500},
        "end": {"x": 1050, "y": 500},
    }
    with pytest.raises(ValueError, match="end X"):
        manager.plan_weeding(weeds=[invalid], settings=SETTINGS, firmware_config={})


def test_farmbot_slot_uses_standard_mount_and_dismount_helpers():
    settings = {
        **SETTINGS,
        "manage_tool": True,
        "tool_name": "Rotary Tool",
        "tool_id": 12,
        "tool_slot_x": 4.2,
        "tool_slot_y": 576.8,
        "tool_slot_z": -386,
        "tool_pullout_direction": 1,
        "tool_slot_from_bot": True,
        "verify_tool_on_mount": True,
        "verify_tool_on_unmount": True,
    }
    mount = FarmbotManager._mount_tool_lua(settings)
    dismount = FarmbotManager._dismount_tool_lua(settings)
    assert 'mount_tool("Rotary Tool")' in mount
    assert mount.startswith("find_home()")
    assert "dismount_tool()" in dismount
    assert dismount.endswith("find_home()")


def test_tool_loading_and_unloading_do_not_require_verification_by_default():
    settings = {
        **SETTINGS,
        "manage_tool": True,
        "tool_name": "Rotary Tool",
        "tool_id": 12,
        "tool_slot_x": 4.2,
        "tool_slot_y": 576.8,
        "tool_slot_z": -386,
        "tool_pullout_direction": 1,
        "tool_slot_from_bot": True,
    }
    mount = FarmbotManager._mount_tool_lua(settings)
    dismount = FarmbotManager._dismount_tool_lua(settings)
    assert "verify_tool" not in mount and "mount_tool(" not in mount
    assert "verify_tool" not in dismount and "dismount_tool(" not in dismount
    assert "local fbv_tool_id=12" in mount
    assert "update_device({mounted_tool_id=fbv_tool_id})" in mount
    assert "update_device({mounted_tool_id=0})" in dismount


def test_manual_slot_uses_farmbot_mount_motion_and_user_coordinates():
    settings = {
        **SETTINGS,
        "manage_tool": True,
        "tool_name": "Rotary Tool",
        "tool_id": None,
        "tool_slot_x": 4.2,
        "tool_slot_y": 576.8,
        "tool_slot_z": -386,
        "tool_pullout_direction": 1,
        "tool_slot_from_bot": False,
    }
    mount = FarmbotManager._mount_tool_lua(settings)
    dismount = FarmbotManager._dismount_tool_lua(settings)
    assert 'get_tool{name="Rotary Tool"}' in mount
    assert "move_absolute(104.2,576.8,-336,50)" in mount
    assert "move_absolute(4.2,576.8,-386,50)" in mount
    assert "move_absolute(104.2,576.8,-386,50)" in dismount
    assert "move_absolute(4.2,576.8,-386,50)" in dismount
