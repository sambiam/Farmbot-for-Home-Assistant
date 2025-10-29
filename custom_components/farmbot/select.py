# custom_components/farmbot/select.py

import logging
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.select import SelectEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import FarmbotEntity

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(minutes=5)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    manager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FarmbotSequenceSelect(manager)], update_before_add=False)

class FarmbotSequenceSelect(FarmbotEntity, SelectEntity):
    def __init__(self, manager):
        super().__init__(manager)
        self._sequences = []
        self._selected = None

    @property
    def should_poll(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await self.async_update()
        self.async_write_ha_state()

    @property
    def unique_id(self) -> str:
        return f"{self._manager.device_id}_sequence_select"

    @property
    def name(self) -> str:
        return "FarmBot Sequence"

    @property
    def options(self) -> list[str]:
        return [f"{s['id']}: {s['name']}" for s in self._sequences]

    @property
    def current_option(self) -> str | None:
        if not self._selected:
            return None
        return f"{self._selected['id']}: {self._selected['name']}"

    async def async_select_option(self, option: str) -> None:
        seq_id = int(option.split(":", 1)[0])
        seq = next((s for s in self._sequences if s["id"] == seq_id), None)
        if not seq:
            _LOGGER.error("Selected sequence ID %s not found", seq_id)
            return
        self._selected = seq
        body = [{"kind": "execute", "args": {"sequence_id": seq_id}, "body": []}]
        self._manager.send_rpc_request(body)
        self.async_write_ha_state()

    async def async_update(self) -> None:
        seqs = await self.hass.async_add_executor_job(self._manager.fetch_sequences)
        if seqs != self._sequences:
            self._sequences = seqs
            self.async_schedule_update_ha_state()
