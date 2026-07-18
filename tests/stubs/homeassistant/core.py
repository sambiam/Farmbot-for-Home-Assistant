"""Minimal stand-ins for homeassistant.core symbols used by the integration."""
from enum import Enum


class HomeAssistant:
    """Placeholder used only for type hints in the integration code."""


class ServiceCall:
    """Placeholder used only for type hints in the integration code."""

    def __init__(self, hass=None, domain=None, service=None, data=None):
        self.hass = hass
        self.domain = domain
        self.service = service
        self.data = data or {}


class SupportsResponse(str, Enum):
    """Stand-in for homeassistant.core.SupportsResponse."""

    NONE = "none"
    OPTIONAL = "optional"
    ONLY = "only"


def callback(func):
    """Stand-in for homeassistant.core.callback; a no-op marker decorator."""
    return func
