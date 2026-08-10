"""Manager-level tests: cached state merging, redaction and network resilience."""
import json
import time
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest

from custom_components.farmbot.api import FarmbotApiError
from custom_components.farmbot.const import (
    DEVICE_STATE_STALE_SECONDS,
    TOPIC_STATUS,
    VISION_IMAGE_POLL_BACKOFF_MAX_SECONDS,
    VISION_IMAGE_POLL_INTERVAL_SECONDS,
)
from custom_components.farmbot.manager import FarmbotManager, merge_state, redact_state

from .helpers import FakeHass

DEVICE_ID = "42"


def _make_manager():
    hass = FakeHass()
    manager = FarmbotManager(
        hass, token="tok", device_id=DEVICE_ID, mqtt_host="mqtt.example.com"
    )
    return hass, manager


def _status(manager, body):
    message = MagicMock()
    message.topic = TOPIC_STATUS.format(device_id=DEVICE_ID)
    message.payload = json.dumps(body).encode()
    manager._on_message(None, None, message)


# --------------------------------------------------------------------------
# State merging
# --------------------------------------------------------------------------


def test_merge_state_keeps_untouched_branches():
    base = {
        "pins": {"7": {"value": 1}, "8": {"value": 0}},
        "location_data": {"position": {"x": 1, "y": 2, "z": 3}},
        "informational_settings": {"busy": False, "locked": False},
    }
    merged = merge_state(base, {"location_data": {"position": {"x": 9}}})

    assert merged["pins"] == {"7": {"value": 1}, "8": {"value": 0}}
    assert merged["location_data"]["position"] == {"x": 9, "y": 2, "z": 3}
    assert merged["informational_settings"]["locked"] is False
    # The previous tree is untouched, so a concurrent reader stays consistent.
    assert base["location_data"]["position"]["x"] == 1


def test_merge_state_lets_incoming_values_win_including_null():
    merged = merge_state(
        {"location_data": {"position": {"x": 1}}},
        {"location_data": {"position": {"x": None}}},
    )
    # null is FarmBot saying "this axis position is genuinely unknown",
    # which is information, not a gap.
    assert merged["location_data"]["position"]["x"] is None


def test_merge_state_replaces_lists_and_mismatched_types():
    assert merge_state({"pins": {"7": {"value": 1}}}, {"pins": [{"number": 7}]}) == {
        "pins": [{"number": 7}]
    }


def test_partial_status_payload_does_not_erase_pins():
    _hass, manager = _make_manager()
    _status(manager, {"pins": {"7": {"value": 1, "mode": 0}}})
    _status(manager, {"location_data": {"position": {"x": 10, "y": 20, "z": 0}}})

    assert manager.pin_is_on(7) is True
    assert manager.status["location_data"]["position"]["y"] == 20
    assert manager._status_revision == 2


def test_status_payload_wrapped_in_body_is_unwrapped():
    _hass, manager = _make_manager()
    _status(manager, {"body": {"pins": {"7": {"value": 1}}}})
    assert manager.pin_is_on(7) is True


def test_non_dict_status_payload_is_ignored_not_fatal():
    _hass, manager = _make_manager()
    _status(manager, {"pins": {"7": {"value": 1}}})
    _status(manager, {"body": ["unexpected"]})

    assert manager.pin_is_on(7) is True  # cached state survived


# --------------------------------------------------------------------------
# Pin reads must be total
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        {},
        {"pins": None},
        {"pins": []},
        {"pins": "nonsense"},
        {"pins": {"8": {"value": 1}}},
        [],
    ],
)
def test_pin_reads_never_raise_for_odd_shapes(status):
    """An exception here used to cost the entity its place on the platform."""
    _hass, manager = _make_manager()
    manager.status = status
    assert manager.pin_is_on(7) is None
    assert manager.is_locked() is None


def test_pin_reads_accept_the_list_shape():
    _hass, manager = _make_manager()
    manager.status = {"pins": [{"number": 7, "value": 1}]}
    assert manager.pin_is_on(7) is True


def test_pin_value_zero_is_off_not_unknown():
    _hass, manager = _make_manager()
    manager.status = {"pins": {"7": {"value": 0}}}
    assert manager.pin_is_on(7) is False


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


def test_availability_requires_a_connection_and_a_recent_sign_of_life():
    _hass, manager = _make_manager()
    assert manager.device_is_available() is False  # never connected

    manager._mqtt = MagicMock()
    manager._on_connect(manager._mqtt, None, {}, 0, None)
    assert manager.device_is_available() is True  # connected, awaiting status

    manager._mqtt_connected_at = time.monotonic() - DEVICE_STATE_STALE_SECONDS - 1
    assert manager.device_is_available() is False  # silent since connecting

    _status(manager, {"pins": {"7": {"value": 1}}})
    assert manager.device_is_available() is True

    manager._on_disconnect(manager._mqtt, None, None, 7)
    assert manager.device_is_available() is False


def test_disconnect_notifies_entities():
    """Entities are told about a dropped link instead of waiting for a poll."""
    from homeassistant.core import callback
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    from custom_components.farmbot.const import SIGNAL_STATE

    hass, manager = _make_manager()
    seen = []

    @callback
    def _listener(*args):
        seen.append(args)

    async_dispatcher_connect(hass, SIGNAL_STATE, _listener)
    manager._on_disconnect(MagicMock(), None, None, 7)
    assert len(seen) == 1


# --------------------------------------------------------------------------
# Log redaction
# --------------------------------------------------------------------------


def test_redact_state_hides_the_farmware_environment():
    redacted = redact_state(
        {
            "user_env": {"MY_API_KEY": "super-secret"},
            "informational_settings": {"busy": False, "token": "abc"},
            "pins": {"7": {"value": 1}},
        }
    )

    assert redacted["user_env"] == "<redacted>"
    assert redacted["informational_settings"]["token"] == "<redacted>"
    assert redacted["informational_settings"]["busy"] is False
    assert redacted["pins"] == {"7": {"value": 1}}
    assert "super-secret" not in json.dumps(redacted)


def test_redacted_status_is_a_copy():
    _hass, manager = _make_manager()
    _status(manager, {"user_env": {"KEY": "secret"}, "pins": {"7": {"value": 1}}})
    assert manager.redacted_status()["user_env"] == "<redacted>"
    assert manager.status["user_env"] == {"KEY": "secret"}


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------


def test_publish_raises_when_mqtt_is_down_instead_of_silently_dropping():
    _hass, manager = _make_manager()
    with pytest.raises(RuntimeError, match="not connected"):
        manager.send_write_pin(7, 1)

    client = MagicMock()
    client.publish.return_value.rc = mqtt.MQTT_ERR_NO_CONN
    manager._mqtt = client
    manager._mqtt_connected = True
    with pytest.raises(RuntimeError, match="rc="):
        manager.send_write_pin(7, 1)

    client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
    manager.send_write_pin(7, 1)  # must not raise


# --------------------------------------------------------------------------
# Vision image polling: DNS/API failures
# --------------------------------------------------------------------------


class _FailingImages:
    """Fails ``failures`` times with a DNS-style error, then succeeds."""

    def __init__(self, failures, result=None):
        self.remaining = failures
        self.result = result or []
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise FarmbotApiError(
                "Network error calling FarmBot (/images): Cannot connect to host "
                "my.farm.bot:443 ssl:default [Timeout while contacting DNS servers]"
            )
        return self.result


@pytest.mark.asyncio
async def test_image_poll_backs_off_and_recovers(caplog):
    _hass, manager = _make_manager()
    manager._known_ready_vision_image_ids = set()
    api = _FailingImages(failures=3)
    manager.api.async_get_images = api

    assert await manager.async_poll_new_vision_images() == []
    assert manager._vision_poll_failures == 1
    first_deadline = manager._vision_poll_retry_after
    assert first_deadline > time.monotonic()

    # A poll that fires inside the backoff window does not hit the network.
    assert await manager.async_poll_new_vision_images() == []
    assert api.calls == 1

    # Once the window elapses the next attempt runs and the delay doubles.
    manager._vision_poll_retry_after = 0.0
    await manager.async_poll_new_vision_images()
    assert api.calls == 2
    assert manager._vision_poll_failures == 2
    assert manager._vision_poll_retry_after - time.monotonic() > VISION_IMAGE_POLL_INTERVAL_SECONDS

    # Identical repeated errors are not re-warned.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1

    # Third failure, then connectivity returns and polling resumes normally.
    manager._vision_poll_retry_after = 0.0
    await manager.async_poll_new_vision_images()
    manager._vision_poll_retry_after = 0.0
    await manager.async_poll_new_vision_images()

    assert api.calls == 4
    assert manager._vision_poll_failures == 0
    assert manager._vision_poll_retry_after == 0.0
    assert manager._vision_poll_last_error is None


def test_image_poll_backoff_is_bounded():
    _hass, manager = _make_manager()
    for _ in range(30):
        manager._note_vision_poll_failure(FarmbotApiError("boom"))
    assert (
        manager._vision_poll_retry_after - time.monotonic()
        <= VISION_IMAGE_POLL_BACKOFF_MAX_SECONDS + 1
    )


@pytest.mark.asyncio
async def test_image_poll_failure_keeps_the_known_image_baseline():
    _hass, manager = _make_manager()
    manager._known_ready_vision_image_ids = {1, 2, 3}
    manager.api.async_get_images = _FailingImages(failures=1)

    assert await manager.async_poll_new_vision_images() == []
    assert manager._known_ready_vision_image_ids == {1, 2, 3}


# --------------------------------------------------------------------------
# Sequence fetching
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequences_are_cached_and_survive_an_api_failure():
    _hass, manager = _make_manager()

    async def ok():
        return [{"id": 1, "name": "Water"}, {"id": "2", "label": "Mow"}, {"name": "no id"}]

    manager.api.async_get_sequences = ok
    assert await manager.async_fetch_sequences() == [
        {"id": 1, "name": "Water"},
        {"id": 2, "name": "Mow"},
    ]
    assert manager.sequences_loaded is True

    async def dns_failure():
        raise FarmbotApiError("Network error calling FarmBot (/sequences): DNS failure")

    manager.api.async_get_sequences = dns_failure
    with pytest.raises(FarmbotApiError):
        await manager.async_fetch_sequences()

    assert manager.sequences == [{"id": 1, "name": "Water"}, {"id": 2, "name": "Mow"}]
    assert manager.sequences_loaded is True
