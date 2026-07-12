"""Minimal stand-in for homeassistant.exceptions."""


class HomeAssistantError(Exception):
    """Base stand-in exception."""


class ServiceValidationError(HomeAssistantError):
    """Stand-in for homeassistant.exceptions.ServiceValidationError."""
