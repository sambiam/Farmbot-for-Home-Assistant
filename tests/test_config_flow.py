"""Isolated tests for custom_components/farmbot/config_flow.py.

No network calls are made: requests.post is always mocked.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
import requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import AbortFlow

from custom_components.farmbot.config_flow import (
    AuthenticationError,
    FarmbotConfigFlow,
    request_token,
)

from .helpers import FakeHass

VALID_TOKEN_OBJ = {
    "encoded": "encoded-jwt",
    "unencoded": {"bot": 42, "mqtt": "mqtt.example.com"},
}


def _run(coro):
    return asyncio.run(coro)


def _make_response(status_code, json_body=None, text_body=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text_body
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no json")
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status_code} error")
    else:
        resp.raise_for_status.return_value = None
    return resp


# --------------------------- request_token() ---------------------------

def test_request_token_success():
    with patch("custom_components.farmbot.config_flow.requests.post") as post:
        post.return_value = _make_response(200, {"token": VALID_TOKEN_OBJ})
        result = request_token("user@example.com", "hunter2")
    assert result == VALID_TOKEN_OBJ


@pytest.mark.parametrize("status_code", [401, 422])
def test_request_token_invalid_auth(status_code):
    with patch("custom_components.farmbot.config_flow.requests.post") as post:
        post.return_value = _make_response(status_code, {"error": "bad credentials"})
        with pytest.raises(AuthenticationError):
            request_token("user@example.com", "wrong-password")


def test_request_token_unexpected_error():
    with patch("custom_components.farmbot.config_flow.requests.post") as post:
        post.return_value = _make_response(500, text_body="boom")
        with pytest.raises(requests.HTTPError):
            request_token("user@example.com", "hunter2")


# --------------------------- async_step_user() ---------------------------

def _flow(hass):
    flow = FarmbotConfigFlow()
    flow.hass = hass
    flow.context = {}
    return flow


def test_async_step_user_creates_entry_with_bot_id_unique_id():
    hass = FakeHass()
    flow = _flow(hass)
    with patch(
        "custom_components.farmbot.config_flow.request_token",
        return_value=VALID_TOKEN_OBJ,
    ):
        result = _run(
            flow.async_step_user({"email": "user@example.com", "password": "hunter2"})
        )

    assert result["type"] == "create_entry"
    assert result["data"]["device_id"] == 42
    assert flow.unique_id == "42"  # stable string form of the bot ID


def test_async_step_user_invalid_auth_shows_form_error():
    hass = FakeHass()
    flow = _flow(hass)
    with patch(
        "custom_components.farmbot.config_flow.request_token",
        side_effect=AuthenticationError,
    ):
        result = _run(
            flow.async_step_user({"email": "user@example.com", "password": "wrong"})
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "auth"}


def test_async_step_user_unexpected_error_shows_form_error():
    hass = FakeHass()
    flow = _flow(hass)
    with patch(
        "custom_components.farmbot.config_flow.request_token",
        side_effect=RuntimeError("network exploded"),
    ):
        result = _run(
            flow.async_step_user({"email": "user@example.com", "password": "hunter2"})
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "unknown"}


def test_async_step_user_duplicate_bot_aborts_already_configured():
    existing = ConfigEntry(
        entry_id="existing-entry",
        unique_id="42",
        domain="farmbot",
        data={"device_id": 42},
    )
    hass = FakeHass(entries=[existing])
    flow = _flow(hass)

    with patch(
        "custom_components.farmbot.config_flow.request_token",
        return_value=VALID_TOKEN_OBJ,
    ):
        with pytest.raises(AbortFlow) as excinfo:
            _run(
                flow.async_step_user(
                    {"email": "user@example.com", "password": "hunter2"}
                )
            )

    assert excinfo.value.reason == "already_configured"
    # No second entry was created for the same bot.
    assert hass.config_entries.async_entries("farmbot") == [existing]


# --------------------------- reauth flow ---------------------------

def test_reauth_confirm_updates_existing_entry_not_a_new_one():
    existing = ConfigEntry(
        entry_id="existing-entry",
        unique_id="42",
        domain="farmbot",
        data={"token": "old-token", "device_id": 42, "mqtt_host": "old.example.com"},
    )
    hass = FakeHass(entries=[existing])
    flow = _flow(hass)
    flow.context = {"entry_id": "existing-entry"}

    _run(flow.async_step_reauth(None))
    assert flow._reauth_entry is existing

    new_token_obj = {
        "encoded": "new-jwt",
        "unencoded": {"bot": 42, "mqtt": "new.example.com"},
    }
    with patch(
        "custom_components.farmbot.config_flow.request_token",
        return_value=new_token_obj,
    ):
        result = _run(
            flow.async_step_reauth_confirm(
                {"email": "user@example.com", "password": "new-password"}
            )
        )

    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert existing.data["token"] == "new-jwt"
    assert existing.data["mqtt_host"] == "new.example.com"
    # Still exactly one entry -- reauth updated it in place.
    assert hass.config_entries.async_entries("farmbot") == [existing]


def test_reauth_confirm_invalid_auth_shows_form_error():
    existing = ConfigEntry(
        entry_id="existing-entry",
        unique_id="42",
        domain="farmbot",
        data={"token": "old-token", "device_id": 42, "mqtt_host": "old.example.com"},
    )
    hass = FakeHass(entries=[existing])
    flow = _flow(hass)
    flow.context = {"entry_id": "existing-entry"}
    _run(flow.async_step_reauth(None))

    with patch(
        "custom_components.farmbot.config_flow.request_token",
        side_effect=AuthenticationError,
    ):
        result = _run(
            flow.async_step_reauth_confirm(
                {"email": "user@example.com", "password": "wrong"}
            )
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "auth"}
    # Original data untouched on failure.
    assert existing.data["token"] == "old-token"
