"""Minimal stand-in for homeassistant.helpers.aiohttp_client."""


def async_get_clientsession(hass):
    """Not exercised by the isolated test suite; raise if accidentally used."""
    raise NotImplementedError(
        "async_get_clientsession is not implemented in the isolated test stub"
    )
