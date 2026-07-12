"""Minimal stand-in for homeassistant.helpers.config_validation."""
import voluptuous as vol

string = str
positive_int = vol.All(vol.Coerce(int), vol.Range(min=0))


def empty_config_schema(domain):
    """Stand-in for cv.empty_config_schema; real HA returns a voluptuous schema."""
    return vol.Schema({}, extra=vol.ALLOW_EXTRA)
