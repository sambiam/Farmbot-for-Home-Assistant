# custom_components/farmbot/switch.py

import logging
from datetime import timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_STATE
from .entity import FarmbotOptimisticEntity

_LOGGER = logging.getLogger(__name__)

# FarmBot pushes state over MQTT, so this is only a safety net for a missed
# message; it also re-evaluates availability without waiting for a packet.
SCAN_INTERVAL = timedelta(seconds=3)

async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data[DOMAIN][entry.entry_id]

    # Hard-coded pin→label map, including original and new peripherals
    pin_label_map = {
        2:  "Rotary Tool",
        3:  "Rotary Tool Reverse",
        7:  "Lighting",
        9:  "Vacuum",
        8:  "Water",
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

    # Deliberately not update_before_add: these entities seed themselves from
    # the manager's cached state in async_added_to_hass. A pre-add update that
    # raised used to make Home Assistant drop the entity from the platform
    # entirely, leaving it stuck as `unavailable` in the registry until the
    # next reload -- which is how a perfectly usable light "became
    # unavailable".
    async_add_entities(switches)
    _LOGGER.debug("Added %d hard-coded FarmBot switches", len(switches))


class FarmbotPeripheralSwitch(FarmbotOptimisticEntity, SwitchEntity):
    """MQTT-based switch for a hard-coded FarmBot peripheral pin.

    State reconciliation (optimistic writes, stale-packet rejection, bounded
    grace period) lives in FarmbotOptimisticEntity and is shared with the
    emergency-stop switch below.
    """

    def __init__(self, manager, pin, label):
        super().__init__(manager)
        self._pin = pin
        self._label = label

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_pin_{self._pin}"

    @property
    def name(self):
        return f"FarmBot {self._label}"

    async def async_turn_on(self, **kwargs):
        await self._async_write_pin(True)

    async def async_turn_off(self, **kwargs):
        await self._async_write_pin(False)

    async def _async_write_pin(self, value: bool) -> None:
        _LOGGER.debug(
            "Turning %s pin %s (%s)", "ON" if value else "OFF", self._pin, self._label
        )
        try:
            self._manager.send_write_pin(self._pin, int(value))
        except RuntimeError as err:
            # The command never reached FarmBot, so the entity must not move.
            raise HomeAssistantError(
                f"Could not send FarmBot {self._label} command: {err}"
            ) from err
        self._async_command_sent(value)

    async def async_added_to_hass(self):
        unsub = async_dispatcher_connect(self.hass, SIGNAL_STATE, self._handle_state)
        self.async_on_remove(unsub)
        # Seed from whatever FarmBot has already reported.
        self._reported_state = self._manager.pin_is_on(self._pin)

    @callback
    def _handle_state(self, _status=None):
        """Handle a FarmBot state broadcast. Event loop only.

        @callback matters: Home Assistant classifies an undecorated
        synchronous dispatcher target as an executor job and runs it in a
        worker thread, which is what produced "calls async_write_ha_state
        from a thread other than the event loop".

        The payload argument is ignored on purpose. The manager owns the one
        authoritative merged state; reading this pin from there means a
        payload that omitted `pins` cannot be mistaken for "the pin is off".
        """
        self._async_farmbot_reported(self._manager.pin_is_on(self._pin))

    async def async_update(self):
        """Safety net for a missed MQTT message; also refreshes availability.

        Reconciles without writing state: Home Assistant writes it itself
        once a polled update returns.
        """
        self._reconcile_report(self._manager.pin_is_on(self._pin))


class FarmbotEmergencyStopSwitch(FarmbotOptimisticEntity, SwitchEntity):
    """Emergency-stop (lock/unlock) via MQTT RPC."""

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_emergency_stop"

    @property
    def name(self):
        return "FarmBot Emergency Stop"

    async def async_turn_on(self, **kwargs):
        _LOGGER.debug("Emergency Stop → LOCK")
        await self._async_send_lock(True, "emergency_lock")

    async def async_turn_off(self, **kwargs):
        _LOGGER.debug("Emergency Stop → UNLOCK")
        await self._async_send_lock(False, "emergency_unlock")

    async def _async_send_lock(self, value: bool, kind: str) -> None:
        try:
            self._manager.send_rpc_request([{"kind": kind, "args": {}}], priority=9000)
        except RuntimeError as err:
            raise HomeAssistantError(f"Could not send FarmBot {kind}: {err}") from err
        self._async_command_sent(value)

    async def async_added_to_hass(self):
        unsub = async_dispatcher_connect(self.hass, SIGNAL_STATE, self._handle_state)
        self.async_on_remove(unsub)
        self._reported_state = self._manager.is_locked()

    @callback
    def _handle_state(self, _status=None):
        self._async_farmbot_reported(self._manager.is_locked())

    async def async_update(self):
        self._reconcile_report(self._manager.is_locked())
