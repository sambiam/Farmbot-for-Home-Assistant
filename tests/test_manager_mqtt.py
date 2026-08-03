"""Isolated tests for the paho-mqtt v2 callback behaviour in manager.py.

No real MQTT connection is made: FarmbotManager is only unit-tested by
calling its _on_connect callback directly with real
paho.mqtt.reasoncodes.ReasonCode values (the same type paho-mqtt v2 passes
to on_connect), and a mocked MQTT client.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.farmbot.const import TOPIC_FROM_DEVICE, TOPIC_LOGS, TOPIC_STATUS
from custom_components.farmbot.manager import FarmbotManager

from .helpers import FakeHass

DEVICE_ID = "42"


class FakeEntry:
    """Records how many times reauth was triggered."""

    def __init__(self):
        self.reauth_calls = 0

    def async_start_reauth(self, hass):
        self.reauth_calls += 1


def _make_manager(entry=None):
    hass = FakeHass()
    manager = FarmbotManager(
        hass, token="tok", device_id=DEVICE_ID, mqtt_host="mqtt.example.com", entry=entry
    )
    return hass, manager


def test_on_connect_success_subscribes_and_clears_auth_failed():
    entry = FakeEntry()
    _, manager = _make_manager(entry)
    manager._auth_failed = True  # simulate a prior failure being cleared on success
    client = MagicMock()
    rc = ReasonCode(PacketTypes.CONNACK, "Success")

    manager._on_connect(client, None, {}, rc, None)

    client.subscribe.assert_any_call(TOPIC_STATUS.format(device_id=DEVICE_ID))
    client.subscribe.assert_any_call(TOPIC_FROM_DEVICE.format(device_id=DEVICE_ID))
    client.subscribe.assert_any_call(TOPIC_LOGS.format(device_id=DEVICE_ID))
    assert manager._auth_failed is False
    assert entry.reauth_calls == 0


def test_on_connect_bad_auth_triggers_reauth_once_without_spam():
    entry = FakeEntry()
    _, manager = _make_manager(entry)
    client = MagicMock()
    rc = ReasonCode(PacketTypes.CONNACK, "Bad user name or password")

    manager._on_connect(client, None, {}, rc, None)
    manager._on_connect(client, None, {}, rc, None)  # second bad-auth callback

    assert entry.reauth_calls == 1  # reauth is not triggered repeatedly
    assert manager._auth_failed is True
    client.subscribe.assert_not_called()


def test_on_connect_bad_auth_without_entry_does_not_raise():
    _, manager = _make_manager(entry=None)
    client = MagicMock()
    rc = ReasonCode(PacketTypes.CONNACK, "Bad user name or password")

    manager._on_connect(client, None, {}, rc, None)  # must not raise

    assert manager._auth_failed is False
    client.subscribe.assert_not_called()


def test_on_connect_other_failure_does_not_trigger_reauth():
    entry = FakeEntry()
    _, manager = _make_manager(entry)
    client = MagicMock()
    rc = ReasonCode(PacketTypes.CONNACK, "Not authorized")

    manager._on_connect(client, None, {}, rc, None)

    assert entry.reauth_calls == 0
    assert manager._auth_failed is False
    client.subscribe.assert_not_called()


def test_pin_binding_log_records_button_input_and_fires_event():
    hass, manager = _make_manager()
    message = MagicMock()
    message.topic = TOPIC_LOGS.format(device_id=DEVICE_ID)
    message.payload = (
        b'{"message":"Button 2: Take Photo (Pi 22) triggered, executing '
        b'Take Photo","created_at":1785283200}'
    )

    manager._on_message(None, None, message)

    assert manager.button_input_count == 1
    assert manager.last_button_input["gpio"] == 22
    assert manager.last_button_input["button"] == "Button 2: Take Photo (Pi 22)"
    assert manager.last_button_input["action"] == "Take Photo"
    assert manager.last_button_input["observed_at"].startswith("2026-")
    assert hass.bus.fired == [
        (
            "farmbot_button_input",
            {
                "device_id": DEVICE_ID,
                "gpio": 22,
                "button": "Button 2: Take Photo (Pi 22)",
                "action": "Take Photo",
                "observed_at": manager.last_button_input["observed_at"],
                "press_count": 1,
                "source": "farmbot_os_pin_binding_log",
                "message": (
                    "Button 2: Take Photo (Pi 22) triggered, executing Take Photo"
                ),
            },
        )
    ]


def test_pin_binding_configuration_error_and_unrelated_logs():
    hass, manager = _make_manager()
    manager._handle_log_message(
        {"message": "Failed to find associated Sequence for: Button 4: (Pi 5)"}
    )
    manager._handle_log_message({"message": "Movement complete"})

    assert manager.button_input_count == 1
    assert manager.last_button_input["gpio"] == 5
    assert manager.last_button_input["action"] == "configuration_error"
    assert len(hass.bus.fired) == 1


def test_soil_point_identity_and_edge_triplets():
    _, manager = _make_manager()
    assert manager.is_soil_height_point(
        {
            "pointer_type": "GenericPointer",
            "meta": {"created_by": "measure-soil-height"},
        }
    )
    assert manager.is_soil_height_point(
        {"pointer_type": "GenericPointer", "meta": {"at_soil_level": "true"}}
    )
    assert not manager.is_soil_height_point(
        {"pointer_type": "GenericPointer", "name": "Soil Height", "meta": {}}
    )
    assert manager._soil_lateral_offsets(50, 15, 100) == [-15, 0, 15]
    assert manager._soil_lateral_offsets(5, 15, 100) == [0, 15, 30]
    assert manager._soil_lateral_offsets(95, 15, 100) == [-30, -15, 0]


def test_soil_capture_commands_include_safe_moves_waits_and_photos():
    commands, frames = FarmbotManager._soil_capture_commands(
        x=100,
        y=200,
        capture_z=0,
        lateral_offsets=[-15, 0, 15],
        z_offsets=[0, 25],
        z_direction=-1,
    )
    assert len(frames) == 6
    assert len(commands) == 18
    assert commands[0]["kind"] == "move"
    assert commands[0]["args"] == {}
    assert commands[0]["body"][-1] == {"kind": "safe_z", "args": {}}
    overwrites = {
        item["args"]["axis"]: item["args"]["axis_operand"]["args"]["number"]
        for item in commands[0]["body"]
        if item["kind"] == "axis_overwrite"
    }
    assert overwrites == {"x": 100.0, "y": 185.0, "z": 0.0}
    assert commands[1] == {"kind": "wait", "args": {"milliseconds": 1500}}
    assert commands[2] == {"kind": "take_photo", "args": {}}
    assert frames[-1]["z"] == -25


def test_move_command_puts_coordinates_in_farmbot_move_body():
    command = FarmbotManager._move_command(
        x=302.1,
        y=451.0,
        z=-1.2,
        speed=75,
        safe_z=True,
    )

    assert command["kind"] == "move"
    assert command["args"] == {}
    assert command["body"][-1] == {"kind": "safe_z", "args": {}}
    overwrites = {
        item["args"]["axis"]: item["args"]["axis_operand"]["args"]["number"]
        for item in command["body"]
        if item["kind"] == "axis_overwrite"
    }
    speeds = {
        item["args"]["axis"]: item["args"]["speed_setting"]["args"]["number"]
        for item in command["body"]
        if item["kind"] == "speed_overwrite"
    }
    assert overwrites == {"x": 302.1, "y": 451.0, "z": -1.2}
    assert speeds == {"x": 75, "y": 75, "z": 75}


def test_only_one_soil_capture_can_be_queued_per_bot():
    _, manager = _make_manager()
    active = MagicMock()
    active.done.return_value = False
    manager._soil_capture_tasks.add(active)
    manager._mqtt = object()
    manager._mqtt_connected = True
    manager.status = {
        "informational_settings": {"busy": False, "locked": False},
        "location_data": {"position": {"x": 0, "y": 0, "z": 0}},
    }
    firmware = {
        "movement_axis_nr_steps_x": 1000,
        "movement_axis_nr_steps_y": 1000,
        "movement_axis_nr_steps_z": 1000,
        "movement_step_per_mm_x": 10,
        "movement_step_per_mm_y": 10,
        "movement_step_per_mm_z": 10,
    }
    try:
        manager.start_soil_capture(
            point={"x": 50, "y": 50},
            firmware_config=firmware,
            capture_z=0,
            baseline_mm=15,
            z_offsets_mm=[0],
        )
    except ValueError as exc:
        assert str(exc) == "FarmBot is busy"
    else:
        raise AssertionError("a second soil capture was queued")


@pytest.mark.asyncio
async def test_same_batch_capture_queues_behind_the_finishing_capture(monkeypatch):
    _, manager = _make_manager()
    manager._mqtt = object()
    manager._mqtt_connected = True
    manager.status = {
        "informational_settings": {"busy": True, "locked": False},
        "location_data": {"position": {"x": 10, "y": 20, "z": 0}},
    }
    firmware = {
        "movement_axis_nr_steps_x": 1000,
        "movement_axis_nr_steps_y": 1000,
        "movement_axis_nr_steps_z": 1000,
        "movement_step_per_mm_x": 10,
        "movement_step_per_mm_y": 10,
        "movement_step_per_mm_z": 10,
    }
    release = asyncio.Event()
    existing = asyncio.create_task(release.wait())
    manager._soil_capture_tasks.add(existing)
    manager._soil_capture_task_batches[existing] = "batch-1"
    manager._soil_capture_batches["batch-1"] = {
        "batch_id": "batch-1",
        "original_position": {"x": 10, "y": 20, "z": 0},
    }
    received = {}

    async def fake_capture(**kwargs):
        received.update(kwargs)
        await release.wait()

    monkeypatch.setattr(manager, "_run_soil_capture", fake_capture)
    try:
        capture_id = manager.start_soil_capture(
            point={"x": 50, "y": 50},
            firmware_config=firmware,
            capture_z=0,
            baseline_mm=15,
            z_offsets_mm=[0],
            batch_id="batch-1",
        )
        await asyncio.sleep(0)
        assert capture_id in manager.soil_captures
        assert received["original_position"] is None
    finally:
        release.set()
        await asyncio.gather(*manager._soil_capture_tasks)


def test_active_soil_frames_are_claimed_before_new_photo_events():
    _, manager = _make_manager()
    manager.soil_captures["capture"] = {
        "status": "waiting_images",
        "started_at": "2026-07-26T00:00:00+00:00",
        "before_image_ids": [1],
        "expected_frames": [
            {
                "x": 10,
                "y": 20,
                "z": 0,
                "lateral_offset_mm": 0,
                "z_offset_mm": 0,
            }
        ],
    }
    manager._claim_active_soil_images(
        {
            1: {
                "id": 1,
                "created_at": "2026-07-26T00:01:00+00:00",
                "meta": {"x": 10, "y": 20, "z": 0},
            },
            2: {
                "id": 2,
                "created_at": "2026-07-26T00:01:00+00:00",
                "meta": {"x": 10, "y": 20, "z": 0},
            },
        }
    )
    assert manager._claimed_soil_image_ids == {2}


@pytest.mark.asyncio
async def test_soil_rpc_resolves_only_matching_acknowledgement():
    _, manager = _make_manager()
    manager._mqtt = object()
    manager._mqtt_connected = True

    def send(_commands, priority=600, label=None):
        asyncio.get_running_loop().call_soon(
            manager._resolve_rpc_response,
            {"kind": "rpc_ok", "args": {"label": label}},
        )
        return label

    manager.send_rpc_request = send
    result = await manager.async_rpc_request([], label="soil-ack", timeout=0.1)
    assert result["kind"] == "rpc_ok"
    assert manager._pending_rpcs == {}


@pytest.mark.asyncio
async def test_soil_rpc_error_and_timeout_fail_closed():
    _, manager = _make_manager()
    manager._mqtt = object()
    manager._mqtt_connected = True

    def reject(_commands, priority=600, label=None):
        asyncio.get_running_loop().call_soon(
            manager._resolve_rpc_response,
            {
                "kind": "rpc_error",
                "args": {"label": label},
                "body": [{"kind": "explanation", "args": {"message": "movement rejected"}}],
            },
        )
        return label

    manager.send_rpc_request = reject
    with pytest.raises(RuntimeError, match="movement rejected"):
        await manager.async_rpc_request([], label="soil-error", timeout=0.1)

    manager.send_rpc_request = lambda _commands, priority=600, label=None: label
    with pytest.raises(TimeoutError):
        await manager.async_rpc_request([], label="soil-timeout", timeout=0.001)
    assert manager._pending_rpcs == {}
