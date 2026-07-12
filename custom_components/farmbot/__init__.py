import logging
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, TOKEN_REFRESH_INTERVAL
from .manager import FarmbotManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "sensor", "button", "binary_sensor", "select"]

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

SERVICE_EXECUTE_SEQUENCE = "execute_sequence"
SERVICE_MOVE_TO = "move_to"

SERVICE_EXECUTE_SEQUENCE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("sequence_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)

SERVICE_MOVE_TO_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Optional("x"): vol.Coerce(float),
        vol.Optional("y"): vol.Coerce(float),
        vol.Optional("z"): vol.Coerce(float),
        vol.Optional("speed", default=100): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
    }
)


def _get_manager(hass: HomeAssistant, config_entry_id: str) -> FarmbotManager:
    """Look up the FarmbotManager for a loaded config entry, or raise."""
    manager = hass.data.get(DOMAIN, {}).get(config_entry_id)
    if manager is None:
        raise ServiceValidationError(
            f"FarmBot config entry '{config_entry_id}' is not loaded"
        )
    return manager


def _async_register_services(hass: HomeAssistant) -> None:
    """Register FarmBot services once, shared across all config entries."""
    if hass.services.has_service(DOMAIN, SERVICE_EXECUTE_SEQUENCE):
        return

    def execute_sequence(call: ServiceCall) -> None:
        manager = _get_manager(hass, call.data["config_entry_id"])
        manager.execute_sequence(call.data["sequence_id"])

    def move_to(call: ServiceCall) -> None:
        manager = _get_manager(hass, call.data["config_entry_id"])
        manager.move_to(
            x=call.data.get("x"),
            y=call.data.get("y"),
            z=call.data.get("z"),
            speed=call.data["speed"],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_EXECUTE_SEQUENCE,
        execute_sequence,
        schema=SERVICE_EXECUTE_SEQUENCE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_MOVE_TO, move_to, schema=SERVICE_MOVE_TO_SCHEMA
    )


def _async_remove_services_if_last_entry(hass: HomeAssistant) -> None:
    """Remove FarmBot services once no config entries remain loaded."""
    if hass.data.get(DOMAIN):
        return
    hass.services.async_remove(DOMAIN, SERVICE_EXECUTE_SEQUENCE)
    hass.services.async_remove(DOMAIN, SERVICE_MOVE_TO)


async def async_setup_entry(hass: HomeAssistant, entry):
    """Set up FarmBot from a config entry."""
    token     = entry.data["token"]
    device_id = entry.data["device_id"]
    mqtt_host = entry.data["mqtt_host"]

    manager = FarmbotManager(hass, token, device_id, mqtt_host, entry=entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    # Check and refresh token immediately on startup
    _LOGGER.info("Checking token expiry on startup")
    await manager.async_check_and_refresh_token()

    # Connect to MQTT without blocking the event loop
    await manager.connect_mqtt()

    # Schedule periodic token refresh check
    async def _periodic_token_check(now):
        """Periodic callback to check and refresh token."""
        _LOGGER.debug("Periodic token refresh check")
        await manager.async_check_and_refresh_token()

    refresh_interval = timedelta(seconds=TOKEN_REFRESH_INTERVAL)
    entry.async_on_unload(
        async_track_time_interval(hass, _periodic_token_check, refresh_interval)
    )
    _LOGGER.info("Token refresh scheduler started (interval: %s)", refresh_interval)

    _async_register_services(hass)

    # Forward each platform to its respective setup file
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry):
    """Unload a config entry."""
    # Unload all platforms first so they can be re-setup on reload
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        _LOGGER.warning("Failed to unload one or more FarmBot platforms")
        return False

    manager = hass.data[DOMAIN].pop(entry.entry_id, None)
    if manager:
        await manager.disconnect_mqtt()

    _async_remove_services_if_last_entry(hass)
    return True


