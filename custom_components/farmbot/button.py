# custom_components/farmbot/button.py

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import EVENT_VISION_REQUEST, SIGNAL_SEQUENCE_SELECTED
from .entity import FarmbotEntity

_LOGGER = logging.getLogger(__name__)

# Sequence IDs that are specific to certain FarmBot setups
MOW_WEEDS_SEQUENCE_ID = 250726
WATER_PLANTS_SEQUENCE_ID = 252674

async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data["farmbot"][entry.entry_id]

    # The FarmBot Vision button and the "launch selected sequence" button do
    # not depend on FarmBot sequences being fetched, so they are always
    # added even if fetching sequences below fails.
    buttons = [
        FarmbotAnalysePlantRadiiButton(manager),
        FarmbotLaunchSelectedSequenceButton(manager),
    ]

    # Fetch available sequences to determine which sequence-specific buttons to add
    try:
        sequences = await hass.async_add_executor_job(manager.fetch_sequences)
        available_sequence_ids = {seq["id"] for seq in sequences}
        _LOGGER.debug("Available sequence IDs: %s", available_sequence_ids)
    except Exception as e:
        _LOGGER.warning("Failed to fetch sequences, no sequence buttons will be added: %s", e)
        available_sequence_ids = set()

    if MOW_WEEDS_SEQUENCE_ID in available_sequence_ids:
        buttons.append(MowWeedsButton(manager))
        _LOGGER.info("Added MowWeedsButton (sequence %d found)", MOW_WEEDS_SEQUENCE_ID)
    else:
        _LOGGER.debug("MowWeedsButton not added (sequence %d not found)", MOW_WEEDS_SEQUENCE_ID)

    if WATER_PLANTS_SEQUENCE_ID in available_sequence_ids:
        buttons.append(WaterPlantsButton(manager))
        _LOGGER.info("Added WaterPlantsButton (sequence %d found)", WATER_PLANTS_SEQUENCE_ID)
    else:
        _LOGGER.debug(
            "WaterPlantsButton not added (sequence %d not found)", WATER_PLANTS_SEQUENCE_ID
        )

    async_add_entities(buttons)
    _LOGGER.debug("Added %d button(s)", len(buttons))

class MowWeedsButton(FarmbotEntity, ButtonEntity):
    @property
    def unique_id(self):
        return f"{self._manager.device_id}_mow_weeds"

    @property
    def name(self):
        return "FarmBot Mow Weeds"

    async def async_press(self):
        _LOGGER.debug("MowWeedsButton pressed")
        body = [{
            "kind": "execute",
            "args": {"sequence_id": 250726},
            "body": [{
                "kind": "parameter_application",
                "args": {
                    "label": "weeds",
                    "data_value": {
                        "kind": "point_group",
                        "args": {"point_group_id": 112772}
                    }
                }
            }]
        }]
        self._manager.send_rpc_request(body, priority=600)

class WaterPlantsButton(FarmbotEntity, ButtonEntity):
    @property
    def unique_id(self):
        return f"{self._manager.device_id}_water_plants"

    @property
    def name(self):
        return "FarmBot Water Plants"

    async def async_press(self):
        _LOGGER.debug("WaterPlantsButton pressed")
        body = [{
            "kind": "execute",
            "args": {"sequence_id": 252674},
            "body": []
        }]
        self._manager.send_rpc_request(body, priority=600)


class FarmbotLaunchSelectedSequenceButton(FarmbotEntity, ButtonEntity):
    """Repeatedly (re-)launches whichever sequence is picked in FarmBot Sequence.

    The select entity triggers a run the moment its option changes, but
    selecting the same option twice in a row does not fire a state change
    in Home Assistant, so it cannot be used to run a sequence again. This
    button reads the manager's currently selected sequence and executes it
    on every press, regardless of whether the selection changed.
    """

    def __init__(self, manager):
        super().__init__(manager)
        self._selected = manager.selected_sequence

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_launch_selected_sequence"

    @property
    def name(self):
        return "FarmBot Launch Selected Sequence"

    @property
    def available(self):
        return self._selected is not None

    @property
    def extra_state_attributes(self):
        if not self._selected:
            return {}
        return {
            "sequence_id": self._selected["id"],
            "sequence_name": self._selected["name"],
        }

    async def async_added_to_hass(self):
        async_dispatcher_connect(self.hass, SIGNAL_SEQUENCE_SELECTED, self._update_selected)

    def _update_selected(self, seq):
        self._selected = seq
        # This callback can be invoked from a thread other than the event
        # loop, so use the thread-safe state update instead of
        # async_write_ha_state (see
        # https://developers.home-assistant.io/docs/asyncio_thread_safety/#async_write_ha_state).
        self.schedule_update_ha_state()

    async def async_press(self):
        seq = self._selected
        if not seq:
            _LOGGER.warning("Launch Selected Sequence pressed but no sequence is selected")
            return
        _LOGGER.debug("Launching selected sequence %s (%s)", seq["id"], seq["name"])
        self._manager.execute_sequence(seq["id"])


class FarmbotAnalysePlantRadiiButton(FarmbotEntity, ButtonEntity):
    """Requests a FarmBot Vision plant-radius analysis.

    This only fires the ``farmbot_vision_request`` Home Assistant event
    (the same event farmbot.request_vision_analysis fires); it does not
    connect to the FarmBot Vision app directly.
    """

    @property
    def unique_id(self):
        return f"{self._manager.device_id}_vision_analyse_plant_radii"

    @property
    def name(self):
        return "FarmBot Analyse Plant Radii"

    async def async_press(self):
        _LOGGER.debug("FarmbotAnalysePlantRadiiButton pressed")
        self.hass.bus.async_fire(
            EVENT_VISION_REQUEST,
            {
                "config_entry_id": self._manager.entry_id,
                "device_id": self._manager.device_id,
                "plant_ids": [],
                "mode": "recommend",
            },
        )
