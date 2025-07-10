
import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN
from .manager import FarmbotManager

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """No YAML configuration supported."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FarmBot via config flow and start MQTT."""
    token     = entry.data["token"]
    device_id = entry.data["device_id"]
    mqtt_host = entry.data["mqtt_host"]

    manager = FarmbotManager(hass, token, device_id, mqtt_host)
    manager.connect_mqtt()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    await hass.config_entries.async_forward_entry_setups(
        entry,
        ["switch", "sensor", "select"]
    )
    _LOGGER.debug("FarmBot manager instantiated and MQTT connected")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Disconnect MQTT and unload platforms."""
    manager = hass.data[DOMAIN].pop(entry.entry_id)
    manager.disconnect_mqtt()
    return True
