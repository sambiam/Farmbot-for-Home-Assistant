"""Minimal stand-in for homeassistant.helpers.config_validation."""
import voluptuous as vol

string = str
positive_int = vol.All(vol.Coerce(int), vol.Range(min=0))


def empty_config_schema(domain):
    """Stand-in for cv.empty_config_schema; real HA returns a voluptuous schema."""
    return vol.Schema({}, extra=vol.ALLOW_EXTRA)


def has_at_least_one_key(*keys):
    """Stand-in for cv.has_at_least_one_key; validates at least one key is present."""

    def validate(obj):
        if not isinstance(obj, dict):
            raise vol.Invalid("expected dictionary")
        for key in obj:
            if key in keys:
                return obj
        raise vol.Invalid(f"must contain at least one of {', '.join(keys)}.")

    return validate
