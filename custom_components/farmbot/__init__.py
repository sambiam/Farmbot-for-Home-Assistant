
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from .manager import FarmbotManager

async def async_setup(hass: HomeAssistant, config: dict):
    """Validate config (none needed)."""
    return True

async def async_setup_entry(hass: HomeAssistant, entry):
    """Set up FarmBot from a config entry."""
    token     = entry.data["token"]
    device_id = entry.data["device_id"]
    mqtt_host = entry.data["mqtt_host"]

    manager = FarmbotManager(hass, token, device_id, mqtt_host)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    # Connect to MQTT without blocking the event loop
    await manager.connect_mqtt()

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


