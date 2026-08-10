"""Minimal stand-in for homeassistant.components.select."""
from homeassistant.helpers.entity import Entity


class SelectEntity(Entity):
    @property
    def options(self):
        return []

    @property
    def current_option(self):
        return None
