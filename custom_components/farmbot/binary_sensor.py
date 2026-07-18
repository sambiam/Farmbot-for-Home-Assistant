
import logging
from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, SIGNAL_STATE, SIGNAL_VISION_STATE
from .entity import FarmbotEntity

_LOGGER = logging.getLogger(__name__)

# How often to re-check whether the vision heartbeat has timed out. This is
# not polling FarmBot Vision itself -- availability is still driven entirely
# by reported heartbeats; this only re-evaluates the already-known heartbeat
# age so "available" flips to "unavailable" without waiting for a new report.
_VISION_TIMEOUT_CHECK_INTERVAL = timedelta(minutes=1)

async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        FarmbotBusyBinarySensor(manager),
        FarmbotEstopBinarySensor(manager),
        FarmbotVisionAvailableBinarySensor(manager),
    ])


class FarmbotBusyBinarySensor(FarmbotEntity, BinarySensorEntity):
    def __init__(self, manager):
        super().__init__(manager)
        self._state = False

    @property
    def name(self):
        return f"{self._manager.device_name} Busy"

    @property
    def is_on(self):
        return self._state

    async def async_added_to_hass(self):
        unsub = async_dispatcher_connect(self.hass, SIGNAL_STATE, self._update_from_state)
        self.async_on_remove(unsub)

    def _update_from_state(self, status):
        busy = status.get("informational_settings", {}).get("busy", False)
        if busy != self._state:
            self._state = busy
            self.schedule_update_ha_state()


class FarmbotEstopBinarySensor(FarmbotEntity, BinarySensorEntity):
    def __init__(self, manager):
        super().__init__(manager)
        self._state = False

    @property
    def name(self):
        return f"{self._manager.device_name} Emergency Stop"

    @property
    def is_on(self):
        return self._state

    async def async_added_to_hass(self):
        unsub = async_dispatcher_connect(self.hass, SIGNAL_STATE, self._update_from_state)
        self.async_on_remove(unsub)
    def _update_from_state(self, status):
        locked = status.get("informational_settings", {}).get("locked", False)
        if locked != self._state:
            self._state = locked
            self.schedule_update_ha_state()


class FarmbotVisionAvailableBinarySensor(FarmbotEntity, BinarySensorEntity):
    """On when a valid FarmBot Vision heartbeat was received recently."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_vision_available"

    @property
    def name(self):
        return "FarmBot Vision Available"

    @property
    def is_on(self):
        return self._manager.vision_is_available()

    @property
    def extra_state_attributes(self):
        attrs = {}
        if self._manager.vision_app_version is not None:
            attrs["app_version"] = self._manager.vision_app_version
        if self._manager.vision_last_heartbeat is not None:
            attrs["last_heartbeat"] = self._manager.vision_last_heartbeat.isoformat()
        return attrs

    async def async_added_to_hass(self):
        unsub_signal = async_dispatcher_connect(
            self.hass, SIGNAL_VISION_STATE, self._handle_update
        )
        self.async_on_remove(unsub_signal)
        unsub_timer = async_track_time_interval(
            self.hass, self._handle_timeout_check, _VISION_TIMEOUT_CHECK_INTERVAL
        )
        self.async_on_remove(unsub_timer)

    def _handle_update(self):
        self.schedule_update_ha_state()

    def _handle_timeout_check(self, now):
        self.schedule_update_ha_state()

