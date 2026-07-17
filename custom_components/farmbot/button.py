# custom_components/farmbot/button.py

import logging

from homeassistant.components.button import ButtonEntity

from .const import EVENT_VISION_REQUEST
from .entity import FarmbotEntity

_LOGGER = logging.getLogger(__name__)

# Sequence IDs that are specific to certain FarmBot setups
MOW_WEEDS_SEQUENCE_ID = 250726
WATER_PLANTS_SEQUENCE_ID = 252674

async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data["farmbot"][entry.entry_id]

    # The FarmBot Vision button does not depend on FarmBot sequences, so it
    # is always added even if fetching sequences below fails.
    buttons = [FarmbotAnalysePlantRadiiButton(manager)]

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
