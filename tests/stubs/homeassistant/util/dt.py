"""Minimal stand-in for homeassistant.util.dt."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str) -> datetime | None:
    """Best-effort ISO-8601 parse; returns None instead of raising."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
