from homeassistant.helpers.entity import Entity
from .const import DOMAIN

class FarmbotEntity(Entity):
    """Base class for all FarmBot entities."""

    def __init__(self, manager):
        self._manager = manager

    @property
    def device_info(self):
        """Group everything under one FarmBot device."""
        return {
            "identifiers": {(DOMAIN, self._manager.device_id)},
            "name":        f"FarmBot {self._manager.device_id}",
            "manufacturer":"FarmBot.io",
        }
