import logging
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from .const import DOMAIN, TOKEN_REFRESH_INTERVAL
from .manager import FarmbotManager

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: dict):
    """Validate config (none needed)."""
    return True

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

    # Forward each platform to its respective setup file
    await hass.config_entries.async_forward_entry_setups(
        entry,
        ["switch", "sensor", "button", "binary_sensor", "select"]
    )
    return True

async def async_unload_entry(hass: HomeAssistant, entry):
    """Unload a config entry."""
    manager = hass.data[DOMAIN].pop(entry.entry_id)
    await manager.disconnect_mqtt()
    return True


