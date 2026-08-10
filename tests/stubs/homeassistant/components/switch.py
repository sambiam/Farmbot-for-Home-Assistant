"""Minimal stand-in for homeassistant.components.switch."""
from homeassistant.helpers.entity import Entity

STATE_ON = "on"
STATE_OFF = "off"
STATE_UNKNOWN = "unknown"
STATE_UNAVAILABLE = "unavailable"


class ToggleEntity(Entity):
    @property
    def is_on(self):
        return None

    @property
    def state(self):
        """Mirror Core: unavailable wins, then unknown for an is_on of None."""
        if not self.available:
            return STATE_UNAVAILABLE
        is_on = self.is_on
        if is_on is None:
            return STATE_UNKNOWN
        return STATE_ON if is_on else STATE_OFF


class SwitchEntity(ToggleEntity):
    """Stand-in for homeassistant.components.switch.SwitchEntity."""
