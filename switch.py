# custom_components/farmbot/switch.py

from datetime import timedelta
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, SIGNAL_STATE
from .entity import FarmbotEntity

_LOGGER = logging.getLogger(__name__)

# Poll every 3 seconds in case an MQTT message is missed
SCAN_INTERVAL = timedelta(seconds=3)

async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data[DOMAIN][entry.entry_id]

    # Hard-coded pin→label map, including original and new peripherals
    pin_label_map = {
        2:  "Rotary Tool",
        3:  "Rotary Tool Reverse",
        7:  "Lighting",
        8:  "Vacuum",
        9:  "Water",
       10:  "Peripheral 4",
       12:  "Peripheral 5",
    }

    # Build switches for exactly those pins
    switches = [
        FarmbotPeripheralSwitch(manager, pin, label)
        for pin, label in sorted(pin_label_map.items())
    ]

    # Always include your emergency-stop switch
    switches.append(FarmbotEmergencyStopSwitch(manager))

    async_add_entities(switches, update_before_add=True)
    _LOGGER.debug("Added %d hard-coded FarmBot switches", len(switches))


class FarmbotPeripheralSwitch(FarmbotEntity, SwitchEntity):
    """MQTT-based switch for a hard-coded FarmBot peripheral pin."""

    def __init__(self, manager, pin, label):
        super().__init__(manager)
        self._pin = pin
        self._label = label
        self._state = False

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_pin_{self._pin}"

    @property
    def name(self):
        return f"FarmBot {self._label}"

    @property
    def is_on(self):
        return self._state

    async def async_turn_on(self, **kwargs):
        _LOGGER.debug("Turning ON pin %s (%s)", self._pin, self._label)
        self._manager.send_write_pin(self._pin, 1)

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug("Turning OFF pin %s (%s)", self._pin, self._label)
        self._manager.send_write_pin(self._pin, 0)

    async def async_added_to_hass(self):
        # Subscribe to incoming MQTT status updates
        async_dispatcher_connect(self.hass, SIGNAL_STATE, self._update_from_state)
        # Initial state fetch
        await self.async_update()

    def _update_from_state(self, status):
        entry = status.get("pins", {}).get(str(self._pin), {})
        if isinstance(entry, dict):
            val = entry.get("value", 0)
        else:
            val = entry or 0
        new_state = bool(val)
        if new_state != self._state:
            self._state = new_state
            self.async_write_ha_state()

    async def async_update(self):
        """Fallback polling in case dispatcher missed an update."""
        entry = self._manager.status.get("pins", {}).get(str(self._pin), {})
        if isinstance(entry, dict):
            val = entry.get("value", 0)
        else:
            val = entry or 0
        new_state = bool(val)
        if new_state != self._state:
            self._state = new_state
            self.async_write_ha_state()


class FarmbotEmergencyStopSwitch(FarmbotEntity, SwitchEntity):
    """Emergency-stop (lock/unlock) via MQTT RPC."""

    def __init__(self, manager):
        super().__init__(manager)
        self._state = False

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_emergency_stop"

    @property
    def name(self):
        return "FarmBot Emergency Stop"

    @property
    def is_on(self):
        return self._state

    async def async_turn_on(self, **kwargs):
        _LOGGER.debug("Emergency Stop → LOCK")
        self._manager.send_rpc_request(
            [{"kind": "emergency_lock", "args": {}}],
            priority=9000,
        )

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug("Emergency Stop → UNLOCK")
        self._manager.send_rpc_request(
            [{"kind": "emergency_unlock", "args": {}}],
            priority=9000,
        )

    async def async_added_to_hass(self):
        async_dispatcher_connect(self.hass, SIGNAL_STATE, self._update_from_state)
        await self.async_update()

    def _update_from_state(self, status):
        locked = bool(status.get("informational_settings", {}).get("locked", False))
        if locked != self._state:
            self._state = locked
            self.async_write_ha_state()

    async def async_update(self):
        locked = bool(
            self._manager.status
            .get("informational_settings", {})
            .get("locked", False)
        )
        if locked != self._state:
            self._state = locked
            self.async_write_ha_state()
