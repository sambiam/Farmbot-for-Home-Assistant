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
        entry_id="entry-1", unique_id="42", domain="farmbot",
        data={"token": "tok", "device_id": 42, "mqtt_host": "mqtt.example.com"},
        options=options or {},
    )
    manager = FarmbotManager(hass, "tok", "42", "mqtt.example.com", entry=entry)
    return hass, manager, entry


# --------------------------- options ---------------------------

def test_vision_options_returns_defaults_when_unset():
    _, manager, _ = _make_manager()
    options = manager.vision_options()
    assert options["vision_enabled"] is False
    assert options["vision_heartbeat_timeout_minutes"] == 10
    assert options["allow_automatic_radius_increases"] is False
    assert options["allow_vision_curve_writes"] is False
    assert options["maximum_plant_radius_mm"] == 500
    assert options["minimum_automatic_confidence"] == 0.90


def test_vision_options_reads_live_from_entry_without_reload():
    hass, manager, entry = _make_manager()
    assert manager.vision_options()["vision_enabled"] is False
    entry.options = {"vision_enabled": True, "allow_vision_curve_writes": True}
    assert manager.vision_options()["vision_enabled"] is True
    assert manager.vision_options()["allow_vision_curve_writes"] is True


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
        available=True, status="running", job_id="job-1",
        last_completed_at="2026-07-17T10:00:00+00:00",
        plants_analysed=5, recommendations=2, automatically_applied=1,
        uncertain=1, message="ok", app_version="1.2.3",
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
