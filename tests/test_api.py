"""Unit tests for custom_components/farmbot/api.py (FarmbotApiClient).

Uses tests/fake_aiohttp.py to script FarmBot HTTP responses -- no real
network access, no real Home Assistant.
"""
import asyncio
import base64
import json as json_module

import pytest

from custom_components.farmbot import api as api_module
from custom_components.farmbot.api import (
    FarmbotApiClient,
    FarmbotApiError,
    FarmbotAuthError,
    FarmbotResponseTooLargeError,
    FarmbotUntrustedUrlError,
    resolve_api_base_url,
)
from tests.fake_aiohttp import FakeResponse, FakeSession


def _run(coro):
    return asyncio.run(coro)


def _jwt(payload: dict) -> str:
    def _b64(obj):
        return base64.urlsafe_b64encode(json_module.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{_b64({'alg': 'HS256'})}.{_b64(payload)}.signature"


class FakeHassForApi:
    """Minimal hass double: only needs to satisfy aiohttp_client.async_get_clientsession."""

    def __init__(self, session):
        self._session = session


def _client(session, token="tok", device_id="42", reauth_callback=None):
    hass = FakeHassForApi(session)
    client = FarmbotApiClient(hass, token, device_id, reauth_callback=reauth_callback)
    client._session = lambda: session  # bypass real aiohttp_client lookup
    return client


# --------------------------- base URL resolution ---------------------------

def test_resolve_api_base_url_falls_back_without_issuer():
    assert resolve_api_base_url("not-a-jwt") == api_module.API_BASE_URL


def test_resolve_api_base_url_uses_https_issuer():
    token = _jwt({"iss": "https://my.farm.bot", "bot": 1})
    assert resolve_api_base_url(token) == "https://my.farm.bot/api"


def test_resolve_api_base_url_rejects_non_https_issuer():
    token = _jwt({"iss": "http://my.farm.bot", "bot": 1})
    assert resolve_api_base_url(token) == api_module.API_BASE_URL


def test_resolve_api_base_url_rejects_private_ip_issuer():
    token = _jwt({"iss": "https://192.168.1.1", "bot": 1})
    assert resolve_api_base_url(token) == api_module.API_BASE_URL


# --------------------------- basic GET / retries ---------------------------

def test_async_get_points_returns_list():
    session = FakeSession(
        [FakeResponse(status=200, json_body=[{"id": 1, "pointer_type": "Plant"}])]
    )
    client = _client(session)
    result = _run(client.async_get_points())
    assert result == [{"id": 1, "pointer_type": "Plant"}]


def test_get_retries_on_server_error_then_succeeds():
    session = FakeSession([
        FakeResponse(status=503, json_body={"error": "temporary"}),
        FakeResponse(status=200, json_body=[]),
    ])
    client = _client(session)
    result = _run(client.async_get_points())
    assert result == []
    assert len(session.calls) == 2


def test_get_does_not_retry_client_errors():
    session = FakeSession([FakeResponse(status=404, json_body={"error": "not found"})])
    client = _client(session)
    with pytest.raises(FarmbotApiError):
        _run(client.async_get_points())
    assert len(session.calls) == 1


def test_patch_is_not_retried_on_server_error():
    session = FakeSession([FakeResponse(status=503, json_body={"error": "boom"})])
    client = _client(session)
    with pytest.raises(FarmbotApiError):
        _run(client.async_patch_plant_radius(1, 150.0))
    assert len(session.calls) == 1


# --------------------------- auth failure handling ---------------------------

def test_401_raises_auth_error_and_triggers_reauth_once():
    calls = []
    session = FakeSession([
        FakeResponse(status=401, json_body={"error": "expired"}),
        FakeResponse(status=401, json_body={"error": "expired"}),
    ])
    client = _client(session, reauth_callback=lambda: calls.append(1))

    with pytest.raises(FarmbotAuthError):
        _run(client.async_get_points())
    with pytest.raises(FarmbotAuthError):
        _run(client.async_get_curves())

    # The client itself calls the callback every time (dedup is the
    # manager's job, see test_manager_vision.py); but it must never retry
    # a 401 as if it were transient.
    assert calls == [1, 1]
    assert len(session.calls) == 2  # one attempt each, no retries


def test_error_detail_never_includes_full_response_body():
    session = FakeSession([
        FakeResponse(status=422, json_body={"error": "bad", "token": "super-secret-value"})
    ])
    client = _client(session)
    with pytest.raises(FarmbotApiError) as excinfo:
        _run(client.async_get_points())
    assert "super-secret-value" not in str(excinfo.value)


# --------------------------- response size limits ---------------------------

def test_json_response_over_limit_is_rejected():
    huge = [{"id": i, "junk": "x" * 100} for i in range(200000)]
    session = FakeSession([FakeResponse(status=200, json_body=huge)])
    client = _client(session)
    with pytest.raises(FarmbotResponseTooLargeError):
        _run(client.async_get_points())


def test_image_download_over_limit_is_rejected():
    session = FakeSession([
        FakeResponse(
            status=200,
            body=b"x" * (api_module.MAX_IMAGE_DOWNLOAD_BYTES + 1),
            content_type="image/jpeg",
            headers={"Content-Length": str(api_module.MAX_IMAGE_DOWNLOAD_BYTES + 1)},
        )
    ])
    client = _client(session)
    with pytest.raises(FarmbotResponseTooLargeError):
        _run(client.async_download_image("https://cdn.example.com/photo.jpg"))


# --------------------------- image download validation ---------------------------

def test_download_image_rejects_non_https_url():
    session = FakeSession([])
    client = _client(session)
    with pytest.raises(FarmbotUntrustedUrlError):
        _run(client.async_download_image("http://cdn.example.com/photo.jpg"))


def test_download_image_rejects_redirect_to_non_https():
    session = FakeSession([
        FakeResponse(status=200, body=b"data", content_type="image/jpeg", url_scheme="http")
    ])
    client = _client(session)
    with pytest.raises(FarmbotUntrustedUrlError):
        _run(client.async_download_image("https://cdn.example.com/photo.jpg"))


def test_download_image_rejects_non_image_content_type():
    session = FakeSession(
        [FakeResponse(status=200, body=b"<html></html>", content_type="text/html")]
    )
    client = _client(session)
    with pytest.raises(FarmbotApiError):
        _run(client.async_download_image("https://cdn.example.com/photo.jpg"))


def test_download_image_success_returns_bytes_and_content_type():
    session = FakeSession([
        FakeResponse(status=200, body=b"\xff\xd8\xff", content_type="image/jpeg")
    ])
    client = _client(session)
    data, content_type = _run(client.async_download_image("https://cdn.example.com/photo.jpg"))
    assert data == b"\xff\xd8\xff"
    assert content_type == "image/jpeg"
    # No Authorization header should be sent to the (third-party) image host.
    method, url, kwargs = session.calls[0]
    assert "headers" not in kwargs or "Authorization" not in (kwargs.get("headers") or {})


# --------------------------- camera calibration ---------------------------

def test_camera_calibration_available_when_core_keys_present():
    envs = [
        {"key": "CAMERA_CALIBRATION_coord_scale", "value": "1.23"},
        {"key": "CAMERA_CALIBRATION_center_pixel_location_x", "value": "320"},
        {"key": "CAMERA_CALIBRATION_center_pixel_location_y", "value": "240"},
        {"key": "CAMERA_CALIBRATION_camera_z", "value": "300"},
        {"key": "CAMERA_CALIBRATION_total_rotation_angle", "value": "1.5"},
        {"key": "SOME_OTHER_ENV", "value": "ignored"},
    ]
    session = FakeSession([FakeResponse(status=200, json_body=envs)])
    client = _client(session)
    result = _run(client.async_get_camera_calibration())
    assert result["available"] is True
    assert result["coord_scale"] == pytest.approx(1.23)
    assert result["camera_z"] == pytest.approx(300)


def test_camera_calibration_unavailable_when_core_keys_missing():
    envs = [{"key": "CAMERA_CALIBRATION_coord_scale", "value": "1.23"}]
    session = FakeSession([FakeResponse(status=200, json_body=envs)])
    client = _client(session)
    result = _run(client.async_get_camera_calibration())
    assert result == {"available": False}


def test_camera_calibration_unavailable_when_endpoint_errors():
    # GET is idempotent and retried up to MAX_RETRIES times on 5xx.
    session = FakeSession([FakeResponse(status=500, json_body={"error": "boom"})] * 3)
    client = _client(session)
    result = _run(client.async_get_camera_calibration())
    assert result == {"available": False}


def test_camera_calibration_unavailable_on_non_numeric_value():
    envs = [
        {"key": "CAMERA_CALIBRATION_coord_scale", "value": "not-a-number"},
        {"key": "CAMERA_CALIBRATION_center_pixel_location_x", "value": "320"},
        {"key": "CAMERA_CALIBRATION_center_pixel_location_y", "value": "240"},
        {"key": "CAMERA_CALIBRATION_camera_z", "value": "300"},
        {"key": "CAMERA_CALIBRATION_total_rotation_angle", "value": "1.5"},
    ]
    session = FakeSession([FakeResponse(status=200, json_body=envs)])
    client = _client(session)
    result = _run(client.async_get_camera_calibration())
    assert result == {"available": False}
