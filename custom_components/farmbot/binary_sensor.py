
import logging
from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, SIGNAL_STATE, SIGNAL_VISION_STATE
from .entity import FarmbotDeviceStateEntity, FarmbotEntity

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


class _FarmbotInformationalBinarySensor(FarmbotDeviceStateEntity, BinarySensorEntity):
    """Binary sensor reading one flag out of FarmBot's informational settings.

    Reads the manager's merged state cache rather than the payload it was
    handed, so a partial FarmBot update that omits the flag leaves the last
    known value alone instead of resetting it to False.
    """

    _key: str
    _attr_should_poll = False

    def __init__(self, manager):
        super().__init__(manager)
        self._state = None
        self._state = self._read()

    def _read(self):
        status = self._manager.status if isinstance(self._manager.status, dict) else {}
        info = status.get("informational_settings")
        if not isinstance(info, dict) or self._key not in info:
            return self._state  # nothing reported; keep what we knew
        return bool(info.get(self._key))

    @property
    def is_on(self):
        return self._state

    async def async_added_to_hass(self):
        unsub = async_dispatcher_connect(self.hass, SIGNAL_STATE, self._update_from_state)
        self.async_on_remove(unsub)

    @callback
    def _update_from_state(self, _status=None):
        # @callback: an undecorated synchronous dispatcher target is run in an
        # executor thread by Home Assistant, where entity state APIs are not
        # safe to call.
        self._state = self._read()
        self.async_write_ha_state()


class FarmbotBusyBinarySensor(_FarmbotInformationalBinarySensor):
    _key = "busy"

    @property
    def name(self):
        return f"{self._manager.device_name} Busy"


class FarmbotEstopBinarySensor(_FarmbotInformationalBinarySensor):
    _key = "locked"

    @property
    def name(self):
        return f"{self._manager.device_name} Emergency Stop"


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

    @callback
    def _handle_update(self):
        self.async_write_ha_state()

    @callback
    def _handle_timeout_check(self, now):
        self.async_write_ha_state()

