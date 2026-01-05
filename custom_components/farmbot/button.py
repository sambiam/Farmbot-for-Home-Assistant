# custom_components/farmbot/button.py

import logging
from homeassistant.components.button import ButtonEntity
from .entity import FarmbotEntity

_LOGGER = logging.getLogger(__name__)

# Sequence IDs that are specific to certain FarmBot setups
MOW_WEEDS_SEQUENCE_ID = 250726
WATER_PLANTS_SEQUENCE_ID = 252674

async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data["farmbot"][entry.entry_id]

    # Fetch available sequences to determine which buttons to add
    try:
        sequences = await hass.async_add_executor_job(manager.fetch_sequences)
        available_sequence_ids = {seq["id"] for seq in sequences}
        _LOGGER.debug("Available sequence IDs: %s", available_sequence_ids)
    except Exception as e:
        _LOGGER.warning("Failed to fetch sequences, no buttons will be added: %s", e)
        return

    # Only add buttons if their sequences exist
    buttons = []

    if MOW_WEEDS_SEQUENCE_ID in available_sequence_ids:
        buttons.append(MowWeedsButton(manager))
        _LOGGER.info("Added MowWeedsButton (sequence %d found)", MOW_WEEDS_SEQUENCE_ID)
    else:
        _LOGGER.debug("MowWeedsButton not added (sequence %d not found)", MOW_WEEDS_SEQUENCE_ID)

    if WATER_PLANTS_SEQUENCE_ID in available_sequence_ids:
        buttons.append(WaterPlantsButton(manager))
        _LOGGER.info("Added WaterPlantsButton (sequence %d found)", WATER_PLANTS_SEQUENCE_ID)
    else:
        _LOGGER.debug("WaterPlantsButton not added (sequence %d not found)", WATER_PLANTS_SEQUENCE_ID)

    if buttons:
        async_add_entities(buttons)
        _LOGGER.debug("Added %d button(s)", len(buttons))
    else:
        _LOGGER.info("No buttons added - required sequences not found")

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
