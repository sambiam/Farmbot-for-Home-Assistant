"""Isolated tests for FarmBot service registration and the move_to schema.

Exercises _async_register_services / _async_remove_services_if_last_entry
against the stub hass.services registry, and validates
SERVICE_MOVE_TO_SCHEMA directly. No network or MQTT calls are made.
"""
import pytest
import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError

from custom_components.farmbot import (
    DOMAIN,
    SERVICE_EXECUTE_SEQUENCE,
    SERVICE_MOVE_TO,
    SERVICE_MOVE_TO_SCHEMA,
    _async_register_services,
    _async_remove_services_if_last_entry,
)

from .helpers import FakeHass


class FakeManager:
    """Records calls made through the FarmBot services."""

    def __init__(self):
        self.executed_sequences = []
        self.move_calls = []

    def execute_sequence(self, sequence_id):
        self.executed_sequences.append(sequence_id)

    def move_to(self, x=None, y=None, z=None, speed=100):
        self.move_calls.append({"x": x, "y": y, "z": z, "speed": speed})


# --------------------------- SERVICE_MOVE_TO_SCHEMA ---------------------------

def test_move_to_schema_rejects_config_entry_id_only():
    with pytest.raises(vol.Invalid):
        SERVICE_MOVE_TO_SCHEMA({"config_entry_id": "entry-1"})


def test_move_to_schema_rejects_speed_without_coordinates():
    with pytest.raises(vol.Invalid):
        SERVICE_MOVE_TO_SCHEMA({"config_entry_id": "entry-1", "speed": 50})


def test_move_to_schema_accepts_x_only():
    result = SERVICE_MOVE_TO_SCHEMA({"config_entry_id": "entry-1", "x": 10})
    assert result["x"] == 10.0
    assert result["speed"] == 100


def test_move_to_schema_accepts_y_only():
    result = SERVICE_MOVE_TO_SCHEMA({"config_entry_id": "entry-1", "y": 5})
    assert result["y"] == 5.0


def test_move_to_schema_accepts_z_only():
    result = SERVICE_MOVE_TO_SCHEMA({"config_entry_id": "entry-1", "z": 5})
    assert result["z"] == 5.0


def test_move_to_schema_accepts_two_coordinates():
    result = SERVICE_MOVE_TO_SCHEMA({"config_entry_id": "entry-1", "x": 1, "y": 2})
    assert result["x"] == 1.0
    assert result["y"] == 2.0


def test_move_to_schema_accepts_all_three_coordinates():
    result = SERVICE_MOVE_TO_SCHEMA(
        {"config_entry_id": "entry-1", "x": 1, "y": 2, "z": 3}
    )
    assert (result["x"], result["y"], result["z"]) == (1.0, 2.0, 3.0)


def test_move_to_schema_accepts_zero_as_a_coordinate():
    result = SERVICE_MOVE_TO_SCHEMA({"config_entry_id": "entry-1", "x": 0})
    assert result["x"] == 0.0


def test_move_to_schema_accepts_negative_coordinate():
    result = SERVICE_MOVE_TO_SCHEMA({"config_entry_id": "entry-1", "x": -5})
    assert result["x"] == -5.0


@pytest.mark.parametrize("speed", [0, 101, -1])
def test_move_to_schema_rejects_speed_out_of_range(speed):
    with pytest.raises(vol.Invalid):
        SERVICE_MOVE_TO_SCHEMA({"config_entry_id": "entry-1", "x": 1, "speed": speed})


# --------------------------- service registration ---------------------------

def test_register_services_registers_execute_sequence_and_move_to():
    hass = FakeHass()
    _async_register_services(hass)

    assert hass.services.has_service(DOMAIN, SERVICE_EXECUTE_SEQUENCE)
    assert hass.services.has_service(DOMAIN, SERVICE_MOVE_TO)


def test_register_services_is_idempotent():
    hass = FakeHass()
    _async_register_services(hass)
    first_registration = dict(hass.services._services)

    _async_register_services(hass)

    # Same handler objects -- nothing was re-registered.
    assert hass.services._services == first_registration


def test_service_selects_correct_manager_by_config_entry_id():
    hass = FakeHass()
    manager_a, manager_b = FakeManager(), FakeManager()
    hass.data[DOMAIN] = {"entry-a": manager_a, "entry-b": manager_b}
    _async_register_services(hass)

    hass.services.call(
        DOMAIN, SERVICE_EXECUTE_SEQUENCE, {"config_entry_id": "entry-b", "sequence_id": 5}
    )

    assert manager_b.executed_sequences == [5]
    assert manager_a.executed_sequences == []


def test_service_raises_for_unknown_config_entry_id():
    hass = FakeHass()
    hass.data[DOMAIN] = {"entry-a": FakeManager()}
    _async_register_services(hass)

    with pytest.raises(ServiceValidationError):
        hass.services.call(
            DOMAIN,
            SERVICE_EXECUTE_SEQUENCE,
            {"config_entry_id": "not-loaded", "sequence_id": 1},
        )


def test_execute_sequence_passes_validated_sequence_id():
    hass = FakeHass()
    manager = FakeManager()
    hass.data[DOMAIN] = {"entry-a": manager}
    _async_register_services(hass)

    hass.services.call(
        DOMAIN, SERVICE_EXECUTE_SEQUENCE, {"config_entry_id": "entry-a", "sequence_id": "42"}
    )

    assert manager.executed_sequences == [42]


def test_move_to_passes_coordinates_and_default_speed():
    hass = FakeHass()
    manager = FakeManager()
    hass.data[DOMAIN] = {"entry-a": manager}
    _async_register_services(hass)

    hass.services.call(DOMAIN, SERVICE_MOVE_TO, {"config_entry_id": "entry-a", "x": 12})

    assert manager.move_calls == [{"x": 12.0, "y": None, "z": None, "speed": 100}]


def test_move_to_passes_explicit_speed():
    hass = FakeHass()
    manager = FakeManager()
    hass.data[DOMAIN] = {"entry-a": manager}
    _async_register_services(hass)

    hass.services.call(
        DOMAIN, SERVICE_MOVE_TO,
        {"config_entry_id": "entry-a", "x": 1, "y": 2, "z": 3, "speed": 25},
    )

    assert manager.move_calls == [{"x": 1.0, "y": 2.0, "z": 3.0, "speed": 25}]


def test_services_remain_registered_while_an_entry_is_still_loaded():
    hass = FakeHass()
    hass.data[DOMAIN] = {"entry-a": FakeManager(), "entry-b": FakeManager()}
    _async_register_services(hass)

    del hass.data[DOMAIN]["entry-a"]
    _async_remove_services_if_last_entry(hass)

    assert hass.services.has_service(DOMAIN, SERVICE_EXECUTE_SEQUENCE)
    assert hass.services.has_service(DOMAIN, SERVICE_MOVE_TO)


def test_services_removed_after_final_entry_unloads():
    hass = FakeHass()
    hass.data[DOMAIN] = {"entry-a": FakeManager()}
    _async_register_services(hass)

    del hass.data[DOMAIN]["entry-a"]
    _async_remove_services_if_last_entry(hass)

    assert not hass.services.has_service(DOMAIN, SERVICE_EXECUTE_SEQUENCE)
    assert not hass.services.has_service(DOMAIN, SERVICE_MOVE_TO)
