"""Config flow for FarmBot integration."""
import logging
import requests
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from .const import DOMAIN, API_BASE_URL

_LOGGER = logging.getLogger(__name__)

class AuthenticationError(Exception):
    """Raised when authentication fails."""

def request_token(email: str, password: str) -> dict:
    """Call FarmBot API to get the token object (encoded + unencoded)."""
    url = f"{API_BASE_URL}/tokens"
    payload = {"user": {"email": email, "password": password}}
    resp = requests.post(url, json=payload, timeout=10)

    # Log full response on error for debugging
    if resp.status_code != 200:
        body = None
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        _LOGGER.error(
            "FarmBot token request failed [%s]: %s",
            resp.status_code, body
        )

    if resp.status_code == 200:
        token_obj = resp.json().get("token") or {}
        if not token_obj.get("encoded") or not token_obj.get("unencoded"):
            _LOGGER.error("FarmBot token response missing fields: %s", token_obj)
            raise AuthenticationError
        return token_obj

    # Treat 401 and 422 as auth failures
    if resp.status_code in (401, 422):
        raise AuthenticationError

    # Let other errors bubble up
    resp.raise_for_status()

class FarmbotConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a FarmBot config flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            try:
                token_obj = await self.hass.async_add_executor_job(
                    request_token,
                    user_input["email"],
                    user_input["password"],
                )
            except AuthenticationError:
                errors["base"] = "auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error fetching FarmBot token")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input["email"],
                    data={
                        "token":     token_obj["encoded"],
                        "device_id": token_obj["unencoded"]["bot"],
                        "mqtt_host": token_obj["unencoded"]["mqtt"],
                    },
                )

        data_schema = vol.Schema(
            {
                vol.Required("email"): str,
                vol.Required("password"): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

