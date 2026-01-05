# custom_components/farmbot/button.py

import logging
from homeassistant.components.button import ButtonEntity
from .entity import FarmbotEntity

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    manager = hass.data["farmbot"][entry.entry_id]
    buttons = [MowWeedsButton(manager), WaterPlantsButton(manager)]
    async_add_entities(buttons)
    _LOGGER.debug("Added %d buttons", len(buttons))

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
