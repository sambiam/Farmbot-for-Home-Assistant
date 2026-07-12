"""Isolated tests for the paho-mqtt v2 callback behaviour in manager.py.

No real MQTT connection is made: FarmbotManager is only unit-tested by
calling its _on_connect callback directly with real
paho.mqtt.reasoncodes.ReasonCode values (the same type paho-mqtt v2 passes
to on_connect), and a mocked MQTT client.
"""
from unittest.mock import MagicMock

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.farmbot.const import TOPIC_LOGS, TOPIC_STATUS
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
