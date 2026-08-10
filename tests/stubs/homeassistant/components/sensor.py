"""Minimal stand-in for homeassistant.components.sensor."""
from enum import Enum

from homeassistant.helpers.entity import Entity


class SensorDeviceClass(str, Enum):
    TIMESTAMP = "timestamp"


class SensorStateClass(str, Enum):
    MEASUREMENT = "measurement"


class SensorEntity(Entity):
    @property
    def native_value(self):
        return None

    @property
    def state(self):
        if not self.available:
            return "unavailable"
        return self.native_value
