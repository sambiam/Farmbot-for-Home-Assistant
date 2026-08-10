"""Minimal stand-in for homeassistant.components.button."""
from homeassistant.helpers.entity import Entity


class ButtonEntity(Entity):
    async def async_press(self):
        return None
