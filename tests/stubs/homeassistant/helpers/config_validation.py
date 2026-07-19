"""Minimal stand-in for homeassistant.helpers.config_validation."""
import voluptuous as vol

def string(value):
    """Stand-in for cv.string that mirrors real HA's None/list/dict rejection.

    Real Home Assistant's ``cv.string`` raises ``vol.Invalid`` for ``None``
    and for list/dict values, coercing everything else with ``str()``. A naive
    ``string = str`` alias would silently accept ``None`` (as the literal
    ``"None"``), hiding schema bugs where a nullable field is declared with a
    bare ``cv.string`` -- exactly the failure mode this stub must reproduce.
    """
    if value is None:
        raise vol.Invalid("string value is None")
    if isinstance(value, (list, dict)):
        raise vol.Invalid("value should be a string")
    return str(value)


positive_int = vol.All(vol.Coerce(int), vol.Range(min=0))


def boolean(value):
    """Stand-in for cv.boolean; validates/coerces common boolean spellings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if lowered in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
    raise vol.Invalid(f"invalid boolean value {value!r}")


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
