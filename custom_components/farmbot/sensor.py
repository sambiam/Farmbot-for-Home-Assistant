# custom_components/farmbot/sensor.py

import logging
from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_BUTTON_INPUT, SIGNAL_VISION_STATE
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
        FarmbotLastButtonInputSensor(manager),
        FarmbotVisionStatusSensor(manager),
        FarmbotVisionLastAnalysisSensor(manager),
        FarmbotVisionRecommendationsSensor(manager),
        FarmbotVisionUncertainPlantsSensor(manager),
    ]
    async_add_entities(sensors)
    _LOGGER.debug("Added %d FarmBot sensors", len(sensors))

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


class FarmbotLastButtonInputSensor(FarmbotEntity, SensorEntity):
    """Timestamp and details of the last Pi GPIO PinBinding trigger."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_should_poll = False

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_last_button_input"

    @property
    def name(self):
        return f"{self._manager.device_name} Last Button Input"

    @property
    def native_value(self):
        event = self._manager.last_button_input
        return event.get("observed_at_datetime") if event else None

    @property
    def extra_state_attributes(self):
        event = self._manager.last_button_input
        if not event:
            return {"press_count": self._manager.button_input_count}
        return {
            key: value
            for key, value in event.items()
            if key != "observed_at_datetime"
        }

    async def async_added_to_hass(self):
        unsub = async_dispatcher_connect(
            self.hass, SIGNAL_BUTTON_INPUT, self._handle_update
        )
        self.async_on_remove(unsub)

    def _handle_update(self):
        self.schedule_update_ha_state()


class _FarmbotVisionSensor(FarmbotEntity, SensorEntity):
    """Base class for the dispatch-driven FarmBot Vision sensors.

    Vision sensors are never polled: they only update when
    farmbot.report_vision_status stores a new value on the manager and
    dispatches SIGNAL_VISION_STATE.
    """

    _attr_should_poll = False

    async def async_added_to_hass(self):
        unsub = async_dispatcher_connect(self.hass, SIGNAL_VISION_STATE, self._handle_update)
        self.async_on_remove(unsub)

    def _handle_update(self):
        self.schedule_update_ha_state()


class FarmbotVisionStatusSensor(_FarmbotVisionSensor):
    """Last-reported FarmBot Vision job status."""

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_vision_status"

    @property
    def name(self):
        return "FarmBot Vision Status"

    @property
    def native_value(self):
        if not self._manager.vision_is_available():
            return "unavailable"
        return self._manager.vision_status

    @property
    def extra_state_attributes(self):
        return {
            "job_id": self._manager.vision_job_id,
            "message": self._manager.vision_message,
        }


class FarmbotVisionLastAnalysisSensor(_FarmbotVisionSensor):
    """Timestamp of the last completed FarmBot Vision analysis."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_vision_last_analysis"

    @property
    def name(self):
        return "FarmBot Vision Last Analysis"

    @property
    def native_value(self):
        return self._manager.vision_last_completed_at


class FarmbotVisionRecommendationsSensor(_FarmbotVisionSensor):
    """Count of plant-radius recommendations from the last analysis."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_vision_recommendations"

    @property
    def name(self):
        return "FarmBot Vision Recommendations"

    @property
    def native_value(self):
        return self._manager.vision_recommendations


class FarmbotVisionUncertainPlantsSensor(_FarmbotVisionSensor):
    """Count of plants the last FarmBot Vision analysis was uncertain about."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_vision_uncertain_plants"

    @property
    def name(self):
        return "FarmBot Vision Uncertain Plants"

    @property
    def native_value(self):
        return self._manager.vision_uncertain
