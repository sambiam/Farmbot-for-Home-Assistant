"""Minimal stand-ins for homeassistant.core symbols used by the integration."""


class HomeAssistant:
    """Placeholder used only for type hints in the integration code."""


class ServiceCall:
    """Placeholder used only for type hints in the integration code."""

    def __init__(self, hass=None, domain=None, service=None, data=None):
        self.hass = hass
        self.domain = domain
        self.service = service
        self.data = data or {}
