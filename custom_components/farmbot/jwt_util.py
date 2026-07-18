"""JWT payload decoding shared by the manager and the FarmBot REST client.

FarmBot's JWTs are not re-verified here (signature verification would
require fetching FarmBot's public key, which the integration does not
do). The payload is only read for claims of a token Home Assistant
already obtained and trusts via the email/password login or the
token-refresh endpoint -- never for a token supplied by anything else.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT's payload segment without verifying its signature."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        padding = 4 - (len(payload_b64) % 4)
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception as err:  # noqa: BLE001 - many unrelated decode failure modes
        _LOGGER.debug("Failed to decode JWT payload: %s", err)
        return None
