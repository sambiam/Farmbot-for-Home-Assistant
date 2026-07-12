"""Config flow for FarmBot integration."""
import logging

import requests
import voluptuous as vol
from homeassistant import config_entries

from .const import API_BASE_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

class AuthenticationError(Exception):
    """Raised when authentication fails."""

def _safe_error_message(resp: requests.Response) -> str:
    """Return a short, non-sensitive server error message for logging.

    Never returns raw response bodies: those can echo back account details
    (or, in principle, credentials) supplied in the request.
    """
    try:
        payload = resp.json()
    except ValueError:
        return "no additional details"
    if isinstance(payload, dict):
        message = payload.get("error") or payload.get("message")
        if isinstance(message, str) and message:
            return message[:200]
    return "no additional details"

def request_token(email: str, password: str) -> dict:
    """Call FarmBot API to get the token object (encoded + unencoded)."""
    url = f"{API_BASE_URL}/tokens"
    payload = {"user": {"email": email, "password": password}}
    resp = requests.post(url, json=payload, timeout=10)

    if resp.status_code != 200:
        _LOGGER.error(
            "FarmBot token request failed [%s]: %s",
            resp.status_code, _safe_error_message(resp),
        )

    if resp.status_code == 200:
        token_obj = resp.json().get("token") or {}
        if not token_obj.get("encoded") or not token_obj.get("unencoded"):
            _LOGGER.error(
                "FarmBot token response missing expected fields (has_encoded=%s, has_unencoded=%s)",
                bool(token_obj.get("encoded")), bool(token_obj.get("unencoded")),
            )
            raise AuthenticationError
        return token_obj

    # Treat 401 and 422 as auth failures
    if resp.status_code in (401, 422):
        raise AuthenticationError

    # Let other errors bubble up
    resp.raise_for_status()

class FarmbotConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a FarmBot config flow."""

    VERSION = 2

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
                bot_id = str(token_obj["unencoded"]["bot"])
                await self.async_set_unique_id(bot_id)
                self._abort_if_unique_id_configured()
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

    async def async_step_reauth(self, user_input=None):
        """Handle reauth flow when token expires or MQTT auth fails."""
        # Store entry_id from context for later update
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    def _expected_reauth_bot_id(self) -> str | None:
        """Return the bot ID the reauth entry must match, or None if unknown.

        Prefers the entry's unique_id (assigned at creation, or by config-entry
        migration for legacy entries); falls back to the stored device_id for
        entries that somehow still lack a unique_id.
        """
        unique_id = self._reauth_entry.unique_id
        if unique_id is not None:
            return str(unique_id)
        device_id = self._reauth_entry.data.get("device_id")
        if device_id is not None:
            return str(device_id)
        return None

    async def async_step_reauth_confirm(self, user_input=None):
        """Confirm reauth with email/password."""
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
                _LOGGER.exception("Unexpected error during reauth token fetch")
                errors["base"] = "unknown"
            else:
                expected_bot_id = self._expected_reauth_bot_id()
                returned_bot_id = str(token_obj["unencoded"]["bot"])

                if expected_bot_id is None:
                    _LOGGER.error(
                        "Reauth for FarmBot entry %s rejected: entry has no unique_id "
                        "or device_id to verify identity against",
                        self._reauth_entry.entry_id,
                    )
                    errors["base"] = "wrong_account"
                elif returned_bot_id != expected_bot_id:
                    _LOGGER.error(
                        "Reauth for FarmBot entry %s rejected: credentials belong to "
                        "bot %s, expected bot %s",
                        self._reauth_entry.entry_id, returned_bot_id, expected_bot_id,
                    )
                    errors["base"] = "wrong_account"
                else:
                    # Update existing entry with new credentials and trigger reload
                    return self.async_update_reload_and_abort(
                        self._reauth_entry,
                        data={
                            "token": token_obj["encoded"],
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
            step_id="reauth_confirm",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "device_id": self._reauth_entry.data.get("device_id", "unknown")
            },
        )

