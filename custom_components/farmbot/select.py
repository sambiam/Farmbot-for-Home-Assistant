# custom_components/farmbot/select.py

import logging
import time
from datetime import timedelta

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import FarmbotApiError
from .const import DOMAIN, SEQUENCE_FETCH_FAILURE_LOG_INTERVAL_SECONDS
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
    """Picks (and launches) a FarmBot sequence.

    The option list comes from the FarmBot cloud API. That API is not always
    reachable -- a DNS blip used to raise straight out of ``async_update``,
    producing a full traceback every polling cycle. Sequences rarely change,
    so a transient failure keeps the previously fetched options instead.
    """

    def __init__(self, manager):
        super().__init__(manager)
        # Seed from the manager's cache so a reload does not start blank.
        self._sequences: list[dict] = manager.sequences
        self._selected = None
        self._fetch_failures = 0
        self._last_failure_log = 0.0

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        """Unavailable only until the very first successful fetch.

        After that the cached options stay usable through FarmBot API
        outages; the bot itself is reachable over MQTT regardless.
        """
        return self._manager.sequences_loaded

    async def async_added_to_hass(self) -> None:
        await self.async_update()

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
        body = [{"kind": "execute", "args": {"sequence_id": seq_id}, "body": []}]
        try:
            self._manager.send_rpc_request(body)
        except RuntimeError as err:
            raise HomeAssistantError(f"Could not launch FarmBot sequence: {err}") from err
        self._selected = seq
        self._manager.set_selected_sequence(seq)
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Refresh the sequence list, tolerating a FarmBot API outage."""
        try:
            self._sequences = await self._manager.async_fetch_sequences()
        except FarmbotApiError as err:
            self._log_fetch_failure(err)
            return
        if self._fetch_failures:
            _LOGGER.info(
                "FarmBot sequence list refreshed after %s failed attempt(s)",
                self._fetch_failures,
            )
            self._fetch_failures = 0

    def _log_fetch_failure(self, err: Exception) -> None:
        """Warn about an unreachable FarmBot API at most twice an hour.

        Expected network failures (DNS, connection reset, timeout, 5xx) are
        already classified as FarmbotApiError by the API client, so this is
        deliberately not a blanket handler: a programming error still
        propagates and is still reported by Home Assistant.
        """
        self._fetch_failures += 1
        now = time.monotonic()
        if self._fetch_failures == 1 or (
            now - self._last_failure_log >= SEQUENCE_FETCH_FAILURE_LOG_INTERVAL_SECONDS
        ):
            self._last_failure_log = now
            _LOGGER.warning(
                "Could not refresh FarmBot sequences (%s); keeping %d cached option(s)",
                err,
                len(self._sequences),
            )
        else:
            _LOGGER.debug("FarmBot sequence refresh still failing (%s)", err)
