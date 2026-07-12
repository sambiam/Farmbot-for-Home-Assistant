"""Isolated tests for custom_components/farmbot config-entry migration.

Exercises async_migrate_entry against the stub ConfigEntry/ConfigEntries,
covering legacy (version 1, no unique_id) entries and duplicate-bot-id
conflicts. No network or MQTT calls are made.
"""
import asyncio

from homeassistant.config_entries import ConfigEntry

from custom_components.farmbot import async_migrate_entry
from custom_components.farmbot.config_flow import FarmbotConfigFlow

from .helpers import FakeHass


def _run(coro):
    return asyncio.run(coro)


def test_migrate_legacy_entry_assigns_unique_id_from_device_id():
    entry = ConfigEntry(
        entry_id="legacy-1",
        unique_id=None,
        domain="farmbot",
        data={"token": "tok", "device_id": 42, "mqtt_host": "mqtt.example.com"},
        version=1,
    )
    hass = FakeHass(entries=[entry])

    result = _run(async_migrate_entry(hass, entry))

    assert result is True
    assert entry.unique_id == "42"
    assert entry.version == FarmbotConfigFlow.VERSION
    # Entry data is preserved untouched.
    assert entry.data == {"token": "tok", "device_id": 42, "mqtt_host": "mqtt.example.com"}


def test_migrate_entry_with_existing_correct_unique_id_is_unchanged():
    entry = ConfigEntry(
        entry_id="already-migrated",
        unique_id="42",
        domain="farmbot",
        data={"token": "tok", "device_id": 42, "mqtt_host": "mqtt.example.com"},
        version=1,
    )
    hass = FakeHass(entries=[entry])

    result = _run(async_migrate_entry(hass, entry))

    assert result is True
    assert entry.unique_id == "42"
    assert entry.version == FarmbotConfigFlow.VERSION
    assert entry.data == {"token": "tok", "device_id": 42, "mqtt_host": "mqtt.example.com"}


def test_migrate_entry_with_no_device_id_fails_safely():
    entry = ConfigEntry(
        entry_id="no-device-id",
        unique_id=None,
        domain="farmbot",
        data={"token": "tok", "mqtt_host": "mqtt.example.com"},
        version=1,
    )
    hass = FakeHass(entries=[entry])

    result = _run(async_migrate_entry(hass, entry))

    assert result is False
    # Entry is left untouched -- no unique_id and no version bump.
    assert entry.unique_id is None
    assert entry.version == 1


def test_migrate_duplicate_legacy_bot_ids_are_not_silently_accepted():
    already_migrated = ConfigEntry(
        entry_id="first",
        unique_id="99",
        domain="farmbot",
        data={"token": "tok-1", "device_id": 99, "mqtt_host": "a.example.com"},
        version=2,
    )
    conflicting = ConfigEntry(
        entry_id="second",
        unique_id=None,
        domain="farmbot",
        data={"token": "tok-2", "device_id": 99, "mqtt_host": "b.example.com"},
        version=1,
    )
    hass = FakeHass(entries=[already_migrated, conflicting])

    result = _run(async_migrate_entry(hass, conflicting))

    assert result is False
    # The conflicting entry is not silently given someone else's identity.
    assert conflicting.unique_id is None
    assert conflicting.version == 1
    # The original entry's identity is untouched.
    assert already_migrated.unique_id == "99"


def test_migrate_two_unmigrated_entries_with_same_bot_id_only_one_succeeds():
    first = ConfigEntry(
        entry_id="dup-a",
        unique_id=None,
        domain="farmbot",
        data={"token": "tok-a", "device_id": 7, "mqtt_host": "a.example.com"},
        version=1,
    )
    second = ConfigEntry(
        entry_id="dup-b",
        unique_id=None,
        domain="farmbot",
        data={"token": "tok-b", "device_id": 7, "mqtt_host": "b.example.com"},
        version=1,
    )
    hass = FakeHass(entries=[first, second])

    first_result = _run(async_migrate_entry(hass, first))
    second_result = _run(async_migrate_entry(hass, second))

    assert first_result is True
    assert first.unique_id == "7"
    assert second_result is False
    assert second.unique_id is None
