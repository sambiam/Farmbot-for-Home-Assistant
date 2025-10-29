# custom_components/farmbot/sensor.py

from datetime import timedelta
import logging

from homeassistant.components.sensor import SensorEntity
from .const import DOMAIN
from .entity import FarmbotEntity

_LOGGER = logging.getLogger(__name__)

# Poll every 5 seconds
SCAN_INTERVAL = timedelta(seconds=3)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up FarmBot X/Y/Z coordinate sensors (polling version)."""
    manager = hass.data[DOMAIN][entry.entry_id]
    sensors = [
        FarmbotCoordinateSensor(manager, "x"),
        FarmbotCoordinateSensor(manager, "y"),
        FarmbotCoordinateSensor(manager, "z"),
    ]
    async_add_entities(sensors)
    _LOGGER.debug("Added 3 FarmBot coordinate sensors (polling every 5s)")

class FarmbotCoordinateSensor(FarmbotEntity, SensorEntity):
    """Polling-based sensor for one axis of FarmBot’s position."""

    def __init__(self, manager, axis):
        super().__init__(manager)
        self._axis = axis
        self._state = None

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_coord_{self._axis}"

    @property
    def name(self):
        return f"FarmBot {self._axis.upper()}"

    @property
    def native_value(self):
        return self._state

    @property
    def should_poll(self):
        return True

    async def async_update(self):
        """Called every SCAN_INTERVAL to refresh axis value."""
        pos = (
            self._manager.status
            .get("location_data", {})
            .get("position", {})
        )
        val = pos.get(self._axis)
        if val is None:
            _LOGGER.debug("Sensor %s: position key missing", self._axis)
            return
        if val != self._state:
            _LOGGER.debug("Sensor %s: %s → %s", self._axis, self._state, val)
            self._state = val
            self.async_write_ha_state()
