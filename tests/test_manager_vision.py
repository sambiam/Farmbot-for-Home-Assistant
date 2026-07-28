"""Isolated tests for the FarmBot Vision runtime state on FarmbotManager.

No network or MQTT calls are made; FarmbotApiClient is constructed for
real (base-URL resolution is pure/local) but never invoked here.
"""

import asyncio
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from custom_components.farmbot.const import EVENT_VISION_REQUEST, SIGNAL_VISION_STATE
from custom_components.farmbot.manager import FarmbotManager

from .fake_api import FakeVisionApi
from .helpers import FakeHass


def _run(coro):
    return asyncio.run(coro)


def _make_manager(options=None):
    hass = FakeHass()
    entry = ConfigEntry(
        entry_id="entry-1",
        unique_id="42",
        domain="farmbot",
        data={"token": "tok", "device_id": 42, "mqtt_host": "mqtt.example.com"},
        options=options or {},
    )
    manager = FarmbotManager(hass, "tok", "42", "mqtt.example.com", entry=entry)
    return hass, manager, entry


def test_grid_repair_moves_takes_photos_and_restores_position():
    async def scenario():
        _, manager, _ = _make_manager()
        manager._mqtt_connected = True
        manager._mqtt = object()
        manager.status = {
            "location_data": {"position": {"x": 10, "y": 20, "z": 0}},
            "informational_settings": {"busy": False, "locked": False},
        }
        calls = []

        async def fake_rpc(commands, **kwargs):
            calls.append(commands)
            return {"kind": "rpc_ok"}

        async def fake_images():
            return []

        captured = 0

        async def fake_wait_for_grid_image(**kwargs):
            nonlocal captured
            captured += 1
            target = kwargs["target"]
            return {
                "image_id": captured,
                **target,
                "distance_from_target_mm": 0,
            }

        async def fake_wait_for_grid_position(**kwargs):
            return dict(kwargs["target"])

        manager.async_rpc_request = fake_rpc
        manager.api.async_get_images = fake_images
        manager._wait_for_grid_image = fake_wait_for_grid_image
        manager._wait_for_grid_position = fake_wait_for_grid_position
        firmware = {
            "movement_axis_nr_steps_x": 600000,
            "movement_axis_nr_steps_y": 300000,
            "movement_axis_nr_steps_z": 100000,
            "movement_step_per_mm_x": 100,
            "movement_step_per_mm_y": 100,
            "movement_step_per_mm_z": 100,
        }
        repair_id = manager.start_grid_repair(
            targets=[
                {"x": 100, "y": 200, "z": 0},
                {"x": 300, "y": 400, "z": 0},
            ],
            firmware_config=firmware,
        )
        await asyncio.gather(*manager._grid_repair_tasks)
        repair = manager.grid_repair(repair_id)
        assert repair["status"] == "complete"
        assert repair["frames"] == [
            {
                "image_id": 1,
                "x": 100.0,
                "y": 200.0,
                "z": 0.0,
                "distance_from_target_mm": 0,
            },
            {
                "image_id": 2,
                "x": 300.0,
                "y": 400.0,
                "z": 0.0,
                "distance_from_target_mm": 0,
            },
        ]
        assert [[item["kind"] for item in command] for command in calls] == [
            ["write_pin"],
            ["move"],
            ["wait", "take_photo"],
            ["move"],
            ["wait", "take_photo"],
            ["write_pin"],
            ["move"],
        ]
        assert calls[0][0]["args"]["pin_value"] == 1
        assert calls[-2][0]["args"]["pin_value"] == 0
        assert calls[1][0]["args"] == {}
        assert calls[1][0]["body"][-1] == {"kind": "safe_z", "args": {}}
        restore_overwrites = {
            item["args"]["axis"]: item["args"]["axis_operand"]["args"]["number"]
            for item in calls[-1][0]["body"]
            if item["kind"] == "axis_overwrite"
        }
        assert restore_overwrites == {"x": 10.0, "y": 20.0, "z": 0.0}
        await manager.async_close()

    _run(scenario())


def test_grid_repair_retries_photo_until_a_processed_target_image_exists():
    async def scenario():
        _, manager, _ = _make_manager()
        manager._mqtt_connected = True
        manager._mqtt = object()
        manager.status = {
            "location_data": {"position": {"x": 10, "y": 20, "z": 0}},
            "informational_settings": {"busy": False, "locked": False},
        }
        calls = []

        async def fake_rpc(commands, **kwargs):
            calls.append(commands)
            return {"kind": "rpc_ok"}

        async def fake_images():
            return []

        outcomes = iter(
            [
                None,
                None,
                None,
                None,
                None,
                {
                    "image_id": 22,
                    "x": 100.0,
                    "y": 200.0,
                    "z": 0.0,
                    "distance_from_target_mm": 0,
                },
            ]
        )

        async def fake_wait_for_grid_image(**_kwargs):
            return next(outcomes)

        async def fake_wait_for_grid_position(**kwargs):
            return dict(kwargs["target"])

        manager.async_rpc_request = fake_rpc
        manager.api.async_get_images = fake_images
        manager._wait_for_grid_image = fake_wait_for_grid_image
        manager._wait_for_grid_position = fake_wait_for_grid_position
        firmware = {
            "movement_axis_nr_steps_x": 600000,
            "movement_axis_nr_steps_y": 300000,
            "movement_axis_nr_steps_z": 100000,
            "movement_step_per_mm_x": 100,
            "movement_step_per_mm_y": 100,
            "movement_step_per_mm_z": 100,
        }
        repair_id = manager.start_grid_repair(
            targets=[{"x": 100, "y": 200, "z": 0}],
            firmware_config=firmware,
        )
        await asyncio.gather(*manager._grid_repair_tasks)

        repair = manager.grid_repair(repair_id)
        assert repair["status"] == "complete"
        assert repair["photo_attempt"] == 6
        assert [[item["kind"] for item in command] for command in calls] == [
            ["write_pin"],
            ["move"],
            ["wait", "take_photo"],
            ["wait", "take_photo"],
            ["wait", "take_photo"],
            ["wait", "take_photo"],
            ["wait", "take_photo"],
            ["wait", "take_photo"],
            ["write_pin"],
            ["move"],
        ]
        await manager.async_close()

    _run(scenario())


def test_grid_repair_does_not_take_photo_when_live_position_did_not_move():
    async def scenario():
        _, manager, _ = _make_manager()
        manager._mqtt_connected = True
        manager._mqtt = object()
        manager.status = {
            "location_data": {"position": {"x": 274, "y": 721, "z": -1}},
            "informational_settings": {"busy": False, "locked": False},
        }
        calls = []

        async def fake_rpc(commands, **kwargs):
            calls.append(commands)
            return {"kind": "rpc_ok"}

        async def fake_images():
            return []

        async def did_not_move(**_kwargs):
            return None

        manager.async_rpc_request = fake_rpc
        manager.api.async_get_images = fake_images
        manager._wait_for_grid_position = did_not_move
        firmware = {
            "movement_axis_nr_steps_x": 600000,
            "movement_axis_nr_steps_y": 300000,
            "movement_axis_nr_steps_z": 100000,
            "movement_step_per_mm_x": 100,
            "movement_step_per_mm_y": 100,
            "movement_step_per_mm_z": 100,
            "movement_home_up_z": 1,
        }
        repair_id = manager.start_grid_repair(
            targets=[{"x": 302.1, "y": 451.0, "z": -1.2}],
            firmware_config=firmware,
        )
        await asyncio.gather(*manager._grid_repair_tasks)

        repair = manager.grid_repair(repair_id)
        assert repair["status"] == "failed"
        assert repair["reported_position"] == {"x": 274.0, "y": 721.0, "z": -1.0}
        assert repair["movement_failure"]["requested_target"] == {
            "x": 302.1,
            "y": 451.0,
            "z": -1.2,
        }
        assert "last live position was X 274.0, Y 721.0, Z -1.0" in repair["message"]
        assert all(item["kind"] != "take_photo" for command in calls for item in command)
        await manager.async_close()

    _run(scenario())


def test_grid_repair_refreshes_and_confirms_live_position():
    async def scenario():
        _, manager, _ = _make_manager()
        manager.status = {
            "location_data": {"position": {"x": 274, "y": 721, "z": -1}},
        }
        calls = []

        async def fake_rpc(commands, **_kwargs):
            calls.append(commands)
            manager.status = {
                "location_data": {"position": {"x": 302.1, "y": 451, "z": -1.2}},
            }
            manager._status_revision += 1
            return {"kind": "rpc_ok"}

        manager.async_rpc_request = fake_rpc
        position = await manager._wait_for_grid_position(
            target={"x": 302.1, "y": 451.0, "z": -1.2},
            timeout=0.1,
        )

        assert calls == [[{"kind": "read_status", "args": {}}]]
        assert position == {"x": 302.1, "y": 451.0, "z": -1.2}
        await manager.async_close()

    _run(scenario())


def test_grid_repair_rejects_new_photo_taken_at_the_old_position():
    async def scenario():
        _, manager, _ = _make_manager()
        now = dt_util.utcnow()

        async def fake_images():
            return [
                {
                    "id": 91,
                    "device_id": 42,
                    "created_at": now.isoformat(),
                    "attachment_processed_at": now.isoformat(),
                    "meta": {"x": 10, "y": 20, "z": 0},
                },
                {
                    "id": 92,
                    "device_id": 42,
                    "created_at": now.isoformat(),
                    "attachment_processed_at": now.isoformat(),
                    "meta": {"x": 100, "y": 200, "z": 0},
                },
            ]

        manager.api.async_get_images = fake_images
        frame = await manager._wait_for_grid_image(
            before=set(),
            target={"x": 100.0, "y": 200.0, "z": 0.0},
            started_at=now,
            timeout=0.1,
        )

        assert frame["image_id"] == 92
        assert frame["distance_from_target_mm"] == 0
        await manager.async_close()

    _run(scenario())


# --------------------------- options ---------------------------


def test_vision_options_returns_defaults_when_unset():
    _, manager, _ = _make_manager()
    options = manager.vision_options()
    assert options["vision_enabled"] is False
    assert options["vision_heartbeat_timeout_minutes"] == 10


def test_vision_options_reads_live_from_entry_without_reload():
    hass, manager, entry = _make_manager()
    assert manager.vision_options()["vision_enabled"] is False
    entry.options = {"vision_enabled": True, "vision_heartbeat_timeout_minutes": 20}
    assert manager.vision_options()["vision_enabled"] is True
    assert manager.vision_options()["vision_heartbeat_timeout_minutes"] == 20


def test_vision_options_without_entry_returns_defaults():
    hass = FakeHass()
    manager = FarmbotManager(hass, "tok", "42", "mqtt.example.com", entry=None)
    assert manager.vision_options()["vision_enabled"] is False


# --------------------------- heartbeat / availability ---------------------------


def test_vision_is_available_false_before_any_heartbeat():
    _, manager, _ = _make_manager()
    assert manager.vision_is_available() is False


def test_vision_is_available_true_right_after_report():
    _, manager, _ = _make_manager()
    manager.update_vision_status(available=True, status="idle")
    assert manager.vision_is_available() is True


def test_vision_is_available_false_after_timeout():
    _, manager, _ = _make_manager(options={"vision_heartbeat_timeout_minutes": 10})
    manager.update_vision_status(available=True, status="idle")
    future = manager.vision_last_heartbeat + timedelta(minutes=11)
    assert manager.vision_is_available(now=future) is False


def test_vision_is_available_true_just_under_timeout():
    _, manager, _ = _make_manager(options={"vision_heartbeat_timeout_minutes": 10})
    manager.update_vision_status(available=True, status="idle")
    future = manager.vision_last_heartbeat + timedelta(minutes=9)
    assert manager.vision_is_available(now=future) is True


def test_new_processed_image_fires_one_targeted_analysis_request():
    hass, manager, _ = _make_manager()
    manager.api = FakeVisionApi(
        images=[
            {
                "id": 10,
                "device_id": 42,
                "created_at": "2020-01-01T00:00:00+00:00",
                "attachment_processed_at": "2020-01-01T00:00:10+00:00",
            }
        ]
    )

    # Historical images establish the baseline and are never replayed.
    assert _run(manager.async_poll_new_vision_images()) == []
    assert hass.bus.fired == []

    manager.api.images[11] = {
        "id": 11,
        "device_id": "device_42",
        "created_at": dt_util.utcnow().isoformat(),
        "attachment_processed_at": dt_util.utcnow().isoformat(),
    }
    assert _run(manager.async_poll_new_vision_images()) == [11]
    assert hass.bus.fired == [
        (
            EVENT_VISION_REQUEST,
            {
                "config_entry_id": "entry-1",
                "device_id": "42",
                "plant_ids": [],
                "image_id": 11,
            },
        )
    ]

    # Polling the same image again is idempotent.
    assert _run(manager.async_poll_new_vision_images()) == []
    assert len(hass.bus.fired) == 1


def test_image_is_requested_only_after_farmbot_finishes_processing_it():
    hass, manager, _ = _make_manager()
    manager.api = FakeVisionApi()
    assert _run(manager.async_poll_new_vision_images()) == []

    manager.api.images[12] = {
        "id": 12,
        "device_id": 42,
        "created_at": dt_util.utcnow().isoformat(),
        "attachment_processed_at": None,
    }
    assert _run(manager.async_poll_new_vision_images()) == []
    manager.api.images[12]["attachment_processed_at"] = dt_util.utcnow().isoformat()
    assert _run(manager.async_poll_new_vision_images()) == [12]
    assert hass.bus.fired[-1][1]["image_id"] == 12


def test_vision_availability_does_not_trust_apps_self_reported_flag():
    """The app's `available` flag is stored as an attribute, never the source of truth."""
    _, manager, _ = _make_manager()
    manager.update_vision_status(available=False, status="idle")
    # A recent heartbeat means "available" from HA's perspective regardless
    # of what the app claimed about itself.
    assert manager.vision_is_available() is True
    assert manager.vision_app_reported_available is False


# --------------------------- update_vision_status / dispatch ---------------------------


def test_update_vision_status_stores_all_fields():
    _, manager, _ = _make_manager()
    manager.update_vision_status(
        available=True,
        status="running",
        job_id="job-1",
        last_completed_at="2026-07-17T10:00:00+00:00",
        plants_analysed=5,
        recommendations=2,
        automatically_applied=1,
        uncertain=1,
        message="ok",
        app_version="1.2.3",
    )
    assert manager.vision_status == "running"
    assert manager.vision_job_id == "job-1"
    assert manager.vision_last_completed_at == dt_util.parse_datetime("2026-07-17T10:00:00+00:00")
    assert manager.vision_plants_analysed == 5
    assert manager.vision_recommendations == 2
    assert manager.vision_automatically_applied == 1
    assert manager.vision_uncertain == 1
    assert manager.vision_message == "ok"
    assert manager.vision_app_version == "1.2.3"


def test_update_vision_status_dispatches_signal_on_change():
    hass, manager, _ = _make_manager()
    received = []
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    async_dispatcher_connect(hass, SIGNAL_VISION_STATE, lambda: received.append(1))

    changed = manager.update_vision_status(available=True, status="running")
    assert changed is True
    assert received == [1]


def test_update_vision_status_skips_dispatch_for_identical_repeat():
    hass, manager, _ = _make_manager()
    received = []
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    async_dispatcher_connect(hass, SIGNAL_VISION_STATE, lambda: received.append(1))

    manager.update_vision_status(available=True, status="running", job_id="job-1")
    changed = manager.update_vision_status(available=True, status="running", job_id="job-1")

    assert changed is False
    assert received == [1]  # only the first report dispatched


def test_update_vision_status_dispatches_again_after_a_real_change():
    hass, manager, _ = _make_manager()
    received = []
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    async_dispatcher_connect(hass, SIGNAL_VISION_STATE, lambda: received.append(1))

    manager.update_vision_status(available=True, status="running")
    manager.update_vision_status(available=True, status="idle")

    assert received == [1, 1]


# --------------------------- reauth dedup across subsystems ---------------------------


class _FakeReauthEntry:
    def __init__(self):
        self.reauth_calls = 0

    def async_start_reauth(self, hass):
        self.reauth_calls += 1


def test_trigger_reauth_from_async_only_fires_once():
    hass = FakeHass()
    entry = _FakeReauthEntry()
    manager = FarmbotManager(hass, "tok", "42", "mqtt.example.com", entry=entry)

    manager._trigger_reauth_from_async()
    manager._trigger_reauth_from_async()

    assert entry.reauth_calls == 1
    assert manager._auth_failed is True


def test_trigger_reauth_from_async_shares_flag_with_mqtt_trigger():
    """A reauth already triggered by MQTT must suppress an API-triggered reauth."""
    hass = FakeHass()
    entry = _FakeReauthEntry()
    manager = FarmbotManager(hass, "tok", "42", "mqtt.example.com", entry=entry)

    manager._auth_failed = True  # simulate MQTT already having triggered reauth
    manager._trigger_reauth_from_async()

    assert entry.reauth_calls == 0
