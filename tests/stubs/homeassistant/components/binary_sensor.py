"""Minimal stand-in for homeassistant.components.binary_sensor."""
from enum import Enum

from homeassistant.components.switch import ToggleEntity


class BinarySensorDeviceClass(str, Enum):
    CONNECTIVITY = "connectivity"


class BinarySensorEntity(ToggleEntity):
    """Stand-in for homeassistant.components.binary_sensor.BinarySensorEntity."""
