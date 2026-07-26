"""Async FarmBot REST API client.

Every FarmBot HTTP call the integration makes -- including calls made on
behalf of the FarmBot Vision bridge -- goes through :class:`FarmbotApiClient`.
This is the one place that:

- attaches the FarmBot bearer token,
- enforces request timeouts and response-size limits,
- retries transient network/server failures with bounded backoff (never
  retrying validation or authorization failures),
- detects FarmBot authentication failures (401/403) and triggers
  reauthentication at most once, and
- redacts signed URLs and credentials from log output.

FarmBot credentials (email, password, JWT, MQTT credentials) never leave
this module; callers only ever see the typed dict/list results below.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
import urllib.parse
from typing import Any

import aiohttp
from homeassistant.helpers import aiohttp_client

from .const import (
    API_BASE_URL,
    AUTH_FAILURE_LOG_INTERVAL_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    MAX_IMAGE_DOWNLOAD_BYTES,
    MAX_JSON_RESPONSE_BYTES,
    MAX_RETRIES,
    POINTER_TYPE_PLANT,
    RETRY_BACKOFF_BASE_SECONDS,
)
from .jwt_util import decode_jwt_payload
from .vision import filter_active_plants

_LOGGER = logging.getLogger(__name__)

# FarmBot stores camera calibration as loose "farmware env" key/value pairs
# rather than a dedicated resource. Key names verified against the
# CAMERA_CALIBRATION_* constants used by FarmBot's own plant-detection
# Farmware (github.com/FarmBot-Labs/plant-detection, Parameters.py), which
# is the reference implementation for this naming convention.
_CAMERA_CALIBRATION_PREFIX = "CAMERA_CALIBRATION_"
_CAMERA_CALIBRATION_REQUIRED_KEYS = (
    "coord_scale",
    "center_pixel_location_x",
    "center_pixel_location_y",
    "camera_z",
    "total_rotation_angle",
)
_CAMERA_CALIBRATION_OPTIONAL_KEYS = (
    "camera_offset_x",
    "camera_offset_y",
)


class FarmbotApiError(Exception):
    """Raised for an unrecoverable FarmBot API failure."""


class FarmbotAuthError(FarmbotApiError):
    """Raised when FarmBot rejects the current token (401/403)."""


class FarmbotResponseTooLargeError(FarmbotApiError):
    """Raised when a FarmBot response exceeds the configured size limit."""


class FarmbotUntrustedUrlError(FarmbotApiError):
    """Raised when an image URL fails scheme or redirect validation."""


def _redact_url(url: str) -> str:
    """Strip query strings (signed-URL tokens) before a URL is logged."""
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _is_safe_https_host(hostname: str) -> bool:
    """Reject issuer/redirect hosts that look like an SSRF target."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return True  # an ordinary DNS hostname, not a literal IP
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def resolve_api_base_url(token: str) -> str:
    """Return the FarmBot API base URL encoded in the token's issuer.

    Self-hosted FarmBot servers issue tokens whose ``iss`` claim points at
    their own host, so the base URL is not always ``my.farm.bot``. Only an
    ``https`` issuer with a non-loopback, non-private hostname is trusted;
    anything else falls back to the default FarmBot SaaS API rather than
    letting a malformed token redirect requests to an internal host.
    """
    payload = decode_jwt_payload(token) or {}
    issuer = payload.get("iss")
    if isinstance(issuer, str) and issuer:
        candidate = issuer if "://" in issuer else f"https://{issuer}"
        candidate = candidate.rstrip("/")
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.scheme == "https" and parsed.hostname and _is_safe_https_host(parsed.hostname):
            return f"{candidate}/api"
    return API_BASE_URL


class _RateLimitedLogger:
    """Logs at most one message per ``interval`` seconds per unique key."""

    def __init__(self, interval: float = AUTH_FAILURE_LOG_INTERVAL_SECONDS):
        self._interval = interval
        self._last: dict[str, float] = {}

    def warning(self, key: str, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._last.get(key, 0.0) >= self._interval:
            self._last[key] = now
            _LOGGER.warning(msg, *args)


class FarmbotApiClient:
    """Thin, centralised async wrapper around the FarmBot REST API."""

    def __init__(
        self,
        hass,
        token: str,
        device_id: str,
        *,
        reauth_callback: Any = None,
    ) -> None:
        self._hass = hass
        self.token = str(token).strip()
        self.device_id = str(device_id).strip()
        self._reauth_callback = reauth_callback
        self._base_url = resolve_api_base_url(self.token)
        self._rate_limited_log = _RateLimitedLogger()

    def update_token(self, token: str) -> None:
        """Update credentials after a token refresh or reauth."""
        self.token = str(token).strip()
        self._base_url = resolve_api_base_url(self.token)

    # -------------------- low-level request handling --------------------

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp_client.async_get_clientsession(self._hass)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def _handle_auth_failure(self, path: str) -> None:
        self._rate_limited_log.warning(
            "auth", "FarmBot API rejected credentials calling %s", path
        )
        if self._reauth_callback is not None:
            self._reauth_callback()

    async def _safe_error_detail(self, resp: aiohttp.ClientResponse) -> str:
        """Return a short, non-sensitive detail string for an error response.

        Never returns the raw response body: FarmBot error payloads can
        echo back request data, and in principle a proxy/CDN could inject
        content that should not be logged verbatim.
        """
        try:
            payload = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError):
            return "no additional details"
        if isinstance(payload, dict):
            message = payload.get("error") or payload.get("message")
            if isinstance(message, str) and message:
                return message[:200]
        return "no additional details"

    async def _read_json(self, resp: aiohttp.ClientResponse, path: str) -> Any:
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_JSON_RESPONSE_BYTES:
            raise FarmbotResponseTooLargeError(f"FarmBot response too large for {path}")
        data = bytearray()
        async for chunk in resp.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > MAX_JSON_RESPONSE_BYTES:
                raise FarmbotResponseTooLargeError(f"FarmBot response too large for {path}")
        if not data:
            return None
        try:
            return json.loads(bytes(data))
        except ValueError as err:
            raise FarmbotApiError(f"FarmBot returned invalid JSON for {path}") from err

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        """Perform one FarmBot API call with bounded retries.

        Retries are only attempted for idempotent (GET/HEAD) requests, and
        only for network errors or 5xx responses. 4xx responses (bad
        requests, validation errors, not-found) are never retried, and
        401/403 short-circuit into a single reauth trigger.
        """
        if idempotent is None:
            idempotent = method in ("GET", "HEAD")
        url = f"{self._base_url}{path}"
        attempts = MAX_RETRIES if idempotent else 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                session = self._session()
                async with session.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
                ) as resp:
                    if resp.status in (401, 403):
                        self._handle_auth_failure(path)
                        raise FarmbotAuthError(
                            f"FarmBot rejected credentials ({resp.status}) for {path}"
                        )
                    if resp.status >= 500:
                        detail = await self._safe_error_detail(resp)
                        last_error = FarmbotApiError(
                            f"FarmBot server error {resp.status} on {path}: {detail}"
                        )
                        if attempt < attempts:
                            await asyncio.sleep(RETRY_BACKOFF_BASE_SECONDS * attempt)
                            continue
                        raise last_error
                    if resp.status >= 400:
                        detail = await self._safe_error_detail(resp)
                        raise FarmbotApiError(
                            f"FarmBot rejected request ({resp.status}) on {path}: {detail}"
                        )
                    return await self._read_json(resp, path)
            except FarmbotApiError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                last_error = FarmbotApiError(f"Network error calling FarmBot ({path}): {err}")
                if attempt < attempts:
                    await asyncio.sleep(RETRY_BACKOFF_BASE_SECONDS * attempt)
                    continue
                raise last_error from err

        assert last_error is not None  # loop always returns or raises above
        raise last_error

    @staticmethod
    def _with_query(path: str, params: dict[str, str]) -> str:
        if not params:
            return path
        return f"{path}?{urllib.parse.urlencode(params)}"

    # -------------------- points / plants --------------------

    async def async_get_points(self, *, pointer_type: str | None = None) -> list[dict]:
        """Return raw FarmBot points, optionally filtered by pointer_type."""
        params = {"pointer_type": pointer_type} if pointer_type else {}
        data = await self._request_json("GET", self._with_query("/points", params))
        return data if isinstance(data, list) else []

    async def async_get_firmware_config(self) -> dict:
        """Return motion configuration used to derive conservative axis bounds."""
        data = await self._request_json("GET", "/firmware_config")
        return data if isinstance(data, dict) else {}

    async def async_get_active_plants(self) -> list[dict]:
        """Return Plant points that are planted/sprouted/active (not archived)."""
        points = await self.async_get_points(pointer_type=POINTER_TYPE_PLANT)
        return filter_active_plants(points)

    async def async_get_point(self, point_id: int) -> dict | None:
        """Return a single point by ID, or None if it does not exist."""
        try:
            data = await self._request_json("GET", f"/points/{int(point_id)}")
        except FarmbotApiError:
            return None
        return data if isinstance(data, dict) else None

    async def async_patch_plant_radius(self, point_id: int, radius_mm: float) -> dict:
        data = await self._request_json(
            "PATCH",
            f"/points/{int(point_id)}",
            json_body={"radius": radius_mm},
            idempotent=False,
        )
        return data if isinstance(data, dict) else {}

    async def async_patch_plant_center(self, point_id: int, x: float, y: float) -> dict:
        data = await self._request_json(
            "PATCH",
            f"/points/{int(point_id)}",
            json_body={"x": x, "y": y},
            idempotent=False,
        )
        return data if isinstance(data, dict) else {}

    async def async_patch_soil_height(self, point_id: int, z: float) -> dict:
        """Patch only the Z coordinate of an existing soil-height point."""
        data = await self._request_json(
            "PATCH",
            f"/points/{int(point_id)}",
            json_body={"z": float(z)},
            idempotent=False,
        )
        return data if isinstance(data, dict) else {}

    async def async_patch_soil_point(
        self, point_id: int, *, x: float, y: float, z: float
    ) -> dict:
        """Relocate an existing soil point and update its measured height."""
        data = await self._request_json(
            "PATCH",
            f"/points/{int(point_id)}",
            json_body={"x": x, "y": y, "z": z},
            idempotent=False,
        )
        return data if isinstance(data, dict) else {}

    async def async_create_weed(
        self, *, name: str, x: float, y: float, z: float, radius: float
    ) -> dict:
        data = await self._request_json(
            "POST",
            "/points",
            json_body={
                "pointer_type": "Weed",
                "name": name,
                "x": x,
                "y": y,
                "z": z,
                "radius": radius,
            },
            idempotent=False,
        )
        return data if isinstance(data, dict) else {}

    async def async_patch_weed_radius(self, point_id: int, radius_mm: float) -> dict:
        data = await self._request_json(
            "PATCH",
            f"/points/{int(point_id)}",
            json_body={"radius": radius_mm},
            idempotent=False,
        )
        return data if isinstance(data, dict) else {}

    async def async_remove_weed(self, point_id: int) -> dict:
        data = await self._request_json(
            "DELETE",
            f"/points/{int(point_id)}",
            idempotent=False,
        )
        return data if isinstance(data, dict) else {}

    async def async_archive_plant(self, point_id: int) -> dict:
        """Reversibly mark a plant as removed without deleting the point.

        FarmBot's point API accepts plant-stage updates.  Keeping the point
        (rather than issuing DELETE) preserves its history and allows a user
        to restore it later if the vision decision was incorrect.
        """
        data = await self._request_json(
            "PATCH",
            f"/points/{int(point_id)}",
            json_body={"plant_stage": "removed"},
            idempotent=False,
        )
        return data if isinstance(data, dict) else {}

    async def async_assign_curve_to_plant(self, point_id: int, curve_id: int | None) -> dict:
        """Assign (or, with curve_id=None, clear) a plant's spread curve."""
        data = await self._request_json(
            "PATCH",
            f"/points/{int(point_id)}",
            json_body={"spread_curve_id": None if curve_id is None else int(curve_id)},
            idempotent=False,
        )
        return data if isinstance(data, dict) else {}

    # -------------------- images --------------------

    async def async_get_images(self) -> list[dict]:
        data = await self._request_json("GET", "/images")
        return data if isinstance(data, list) else []

    async def async_get_image(self, image_id: int) -> dict | None:
        try:
            data = await self._request_json("GET", f"/images/{int(image_id)}")
        except FarmbotApiError:
            return None
        return data if isinstance(data, dict) else None

    async def async_download_image(self, attachment_url: str) -> tuple[bytes, str]:
        """Download raw image bytes for an already-resolved attachment URL.

        FarmBot attachment URLs are pre-signed cloud-storage links; the
        FarmBot bearer token is deliberately *not* sent here -- there is
        nothing on that host to authenticate to, and sending it would leak
        the token to a third-party storage provider.
        """
        parsed = urllib.parse.urlsplit(attachment_url)
        if parsed.scheme != "https":
            raise FarmbotUntrustedUrlError("Image URL must use https")

        try:
            session = self._session()
            async with session.get(
                attachment_url,
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
                max_redirects=2,
                allow_redirects=True,
            ) as resp:
                if str(resp.url.scheme) != "https":
                    raise FarmbotUntrustedUrlError("Image redirected to a non-https URL")
                if resp.status != 200:
                    raise FarmbotApiError(f"Image download failed with status {resp.status}")

                content_type = resp.headers.get("Content-Type", "")
                if not content_type.startswith("image/"):
                    raise FarmbotApiError(f"Unexpected content type for image: {content_type}")

                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_IMAGE_DOWNLOAD_BYTES:
                    raise FarmbotResponseTooLargeError("Image exceeds maximum download size")

                data = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_DOWNLOAD_BYTES:
                        raise FarmbotResponseTooLargeError("Image exceeds maximum download size")

                _LOGGER.debug(
                    "Downloaded image from %s (%d bytes)", _redact_url(attachment_url), len(data)
                )
                return bytes(data), content_type
        except FarmbotApiError:
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            raise FarmbotApiError(f"Network error downloading image: {err}") from err

    # -------------------- curves --------------------

    async def async_get_curves(self) -> list[dict]:
        data = await self._request_json("GET", "/curves")
        return data if isinstance(data, list) else []

    async def async_get_curve(self, curve_id: int) -> dict | None:
        try:
            data = await self._request_json("GET", f"/curves/{int(curve_id)}")
        except FarmbotApiError:
            return None
        return data if isinstance(data, dict) else None

    async def async_create_curve(self, *, name: str, type_: str, data: dict) -> dict:
        body = {"name": name, "type": type_, "data": data}
        result = await self._request_json("POST", "/curves", json_body=body, idempotent=False)
        if not isinstance(result, dict):
            raise FarmbotApiError("Unexpected FarmBot response creating curve")
        return result

    async def async_patch_curve(
        self, curve_id: int, *, name: str | None = None, data: dict | None = None
    ) -> dict:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if data is not None:
            body["data"] = data
        result = await self._request_json(
            "PATCH", f"/curves/{int(curve_id)}", json_body=body, idempotent=False
        )
        return result if isinstance(result, dict) else {}

    # -------------------- camera calibration --------------------

    async def async_get_camera_calibration(self) -> dict:
        """Best-effort read of FarmBot's camera calibration.

        FarmBot stores calibration as loose ``farmware_envs`` key/value
        strings rather than a dedicated, versioned resource, so parsing is
        deliberately defensive: any missing or non-numeric core value means
        calibration is reported unavailable instead of guessed at.
        """
        try:
            envs = await self._request_json("GET", "/farmware_envs")
        except FarmbotApiError:
            return {"available": False}
        if not isinstance(envs, list):
            return {"available": False}

        raw: dict[str, Any] = {}
        for item in envs:
            if not isinstance(item, dict):
                continue
            key = item.get("key") or ""
            if key.startswith(_CAMERA_CALIBRATION_PREFIX):
                raw[key[len(_CAMERA_CALIBRATION_PREFIX):]] = item.get("value")

        values: dict[str, float] = {}
        for name in _CAMERA_CALIBRATION_REQUIRED_KEYS + _CAMERA_CALIBRATION_OPTIONAL_KEYS:
            if name not in raw:
                continue
            try:
                values[name] = float(raw[name])
            except (TypeError, ValueError):
                continue

        if not all(name in values for name in _CAMERA_CALIBRATION_REQUIRED_KEYS):
            return {"available": False}

        return {"available": True, **values}
