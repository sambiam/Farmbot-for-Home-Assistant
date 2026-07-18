"""Isolated tests for custom_components/farmbot/config_flow.py's OptionsFlow.

No network calls: this only exercises the schema defaults and the
create-entry result, matching how the real OptionsFlow is driven.
"""
import asyncio

from homeassistant.config_entries import ConfigEntry

from custom_components.farmbot.config_flow import FarmbotConfigFlow, FarmbotOptionsFlow


def _run(coro):
    return asyncio.run(coro)


def _flow_for(entry):
    flow = FarmbotOptionsFlow()
    flow.config_entry = entry
    return flow


def test_async_get_options_flow_returns_options_flow_instance():
    entry = ConfigEntry(entry_id="e1", unique_id="42", domain="farmbot", data={})
    result = FarmbotConfigFlow.async_get_options_flow(entry)
    assert isinstance(result, FarmbotOptionsFlow)


def test_options_form_defaults_match_documented_defaults():
    entry = ConfigEntry(entry_id="e1", unique_id="42", domain="farmbot", data={}, options={})
    flow = _flow_for(entry)
    result = _run(flow.async_step_init(None))
    assert result["type"] == "form"
    schema_dict = result["data_schema"].schema
    defaults = {str(key): key.default() for key in schema_dict}
    assert defaults["vision_enabled"] is False
    assert defaults["vision_heartbeat_timeout_minutes"] == 10
    assert defaults["allow_automatic_radius_increases"] is False
    assert defaults["allow_vision_curve_writes"] is False
    assert defaults["maximum_plant_radius_mm"] == 500
    assert defaults["minimum_automatic_confidence"] == 0.90


def test_options_form_shows_currently_saved_values_as_defaults():
    entry = ConfigEntry(
        entry_id="e1", unique_id="42", domain="farmbot", data={},
        options={"vision_enabled": True, "maximum_plant_radius_mm": 250},
    )
    flow = _flow_for(entry)
    result = _run(flow.async_step_init(None))
    schema_dict = result["data_schema"].schema
    defaults = {str(key): key.default() for key in schema_dict}
    assert defaults["vision_enabled"] is True
    assert defaults["maximum_plant_radius_mm"] == 250


def test_options_step_saves_submitted_values():
    entry = ConfigEntry(entry_id="e1", unique_id="42", domain="farmbot", data={}, options={})
    flow = _flow_for(entry)
    submitted = {
        "vision_enabled": True,
        "vision_heartbeat_timeout_minutes": 5,
        "allow_automatic_radius_increases": False,
        "allow_vision_curve_writes": True,
        "maximum_plant_radius_mm": 300,
        "minimum_automatic_confidence": 0.95,
    }
    result = _run(flow.async_step_init(submitted))
    assert result == {"type": "create_entry", "title": "", "data": submitted}


def test_options_flow_never_asks_for_credentials():
    entry = ConfigEntry(entry_id="e1", unique_id="42", domain="farmbot", data={}, options={})
    flow = _flow_for(entry)
    result = _run(flow.async_step_init(None))
    field_names = {str(key) for key in result["data_schema"].schema}
    assert "email" not in field_names
    assert "password" not in field_names
