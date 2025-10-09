# custom_components/farmbot/binary_sensor.py
import logging
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, SIGNAL_STATE
from .entity import FarmbotEntity

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        FarmbotBusyBinarySensor(manager),
        FarmbotEstopBinarySensor(manager),
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
