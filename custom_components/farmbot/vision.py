"""Pure validation, filtering and projection helpers for the FarmBot Vision bridge.

Deliberately free of aiohttp/Home Assistant imports so it can be unit
tested as plain data-in/data-out functions. Every function here treats its
input as untrusted, whether it originated from the FarmBot API or from the
local FarmBot Vision companion app: confidence, plant IDs, units and
"current" values are never assumed correct just because they were supplied
by the caller. This is the integration's independent safety layer.

Unit note: a FarmBot point's ``radius`` is a **radius** in millimetres. A
spread curve's data values are plant **diameters** in millimetres. These
are never the same number for the same plant, and this module never
conflates them -- see ``radius_mm_to_diameter_mm`` / ``diameter_mm_to_radius_mm``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .const import (
    ACTIVE_PLANT_STAGES,
    CURVE_NAME_PREFIX,
    MAX_CURVE_CONTROL_POINTS,
    POINTER_TYPE_PLANT,
    RADIUS_TOLERANCE_MM,
    VISION_CURVE_TYPE,
)

PLANT_FIELDS = (
    "id",
    "name",
    "openfarm_slug",
    "x",
    "y",
    "z",
    "radius",
    "plant_stage",
    "planted_at",
    "spread_curve_id",
)
CURVE_FIELDS = ("id", "name", "type", "data")


# -------------------- plants --------------------

def is_active_plant(point: dict[str, Any]) -> bool:
    """True if `point` is a non-archived Plant in an active growth stage."""
    if point.get("pointer_type") != POINTER_TYPE_PLANT:
        return False
    if point.get("discarded_at"):
        return False
    return point.get("plant_stage") in ACTIVE_PLANT_STAGES


def filter_active_plants(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only planted/sprouted/active Plant points, never harvested/removed/discarded."""
    return [p for p in points if is_active_plant(p)]


def project_plant(point: dict[str, Any]) -> dict[str, Any]:
    """Trim a FarmBot point down to the fields the Vision app needs."""
    return {field: point.get(field) for field in PLANT_FIELDS}


# -------------------- images --------------------

def is_image_ready(image: dict[str, Any]) -> bool:
    """True once FarmBot has finished processing the image attachment."""
    return bool(image.get("attachment_processed_at"))


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def project_image(image: dict[str, Any]) -> dict[str, Any]:
    """Trim a FarmBot image record to metadata only -- never an attachment URL."""
    meta = image.get("meta") or {}
    return {
        "id": image.get("id"),
        "created_at": image.get("created_at"),
        "x": meta.get("x"),
        "y": meta.get("y"),
        "z": meta.get("z"),
    }


def filter_recent_processed_images(
    images: list[dict[str, Any]], *, now: datetime, lookback_hours: float
) -> list[dict[str, Any]]:
    """Return only fully-processed images created within the lookback window."""
    cutoff = now - timedelta(hours=lookback_hours)
    result = []
    for image in images:
        if not is_image_ready(image):
            continue
        created_at = _parse_timestamp(image.get("created_at"))
        if created_at is None or created_at < cutoff:
            continue
        result.append(image)
    return result


# -------------------- curves --------------------

def is_vision_owned_curve(curve: dict[str, Any]) -> bool:
    """True for a spread curve created/managed by this integration."""
    name = curve.get("name") or ""
    return name.startswith(CURVE_NAME_PREFIX) and curve.get("type") == VISION_CURVE_TYPE


def project_curve(curve: dict[str, Any]) -> dict[str, Any]:
    return {field: curve.get(field) for field in CURVE_FIELDS}


def select_relevant_curves(
    plants: list[dict[str, Any]], curves: list[dict[str, Any]], *, include_all: bool = False
) -> list[dict[str, Any]]:
    """Return curves assigned to `plants` plus all FarmBot Vision-owned curves."""
    if include_all:
        return [project_curve(c) for c in curves]
    wanted_ids = {p.get("spread_curve_id") for p in plants if p.get("spread_curve_id") is not None}
    return [
        project_curve(c)
        for c in curves
        if c.get("id") in wanted_ids or is_vision_owned_curve(c)
    ]


# -------------------- unit conversion --------------------

def radius_mm_to_diameter_mm(radius_mm: float) -> float:
    """Convert a FarmBot point radius (mm) to a spread-curve diameter (mm)."""
    return radius_mm * 2.0


def diameter_mm_to_radius_mm(diameter_mm: float) -> float:
    """Convert a spread-curve diameter (mm) to a FarmBot point radius (mm)."""
    return diameter_mm / 2.0


# -------------------- radius validation --------------------

@dataclass
class ValidationResult:
    ok: bool
    reason: str | None = None


def _is_finite_positive_number(value: Any) -> bool:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if math.isnan(value) or math.isinf(value):
        return False
    return value > 0


def validate_radius_change(
    *,
    point: dict[str, Any] | None,
    device_id: Any,
    expected_current_radius_mm: Any,
    recommended_radius_mm: Any,
    allow_automatic_shrink: bool,
    maximum_plant_radius_mm: float,
) -> ValidationResult:
    """Independently re-validate a proposed plant-radius change.

    Never trusts the caller's claimed current radius, plant identity or
    units: the point is re-checked fresh against what FarmBot itself
    reports for it.
    """
    if point is None:
        return ValidationResult(False, "plant_not_found")
    if str(point.get("device_id")) != str(device_id):
        return ValidationResult(False, "wrong_device")
    if point.get("pointer_type") != POINTER_TYPE_PLANT:
        return ValidationResult(False, "not_a_plant")
    if point.get("discarded_at"):
        return ValidationResult(False, "plant_archived")

    if not _is_finite_positive_number(expected_current_radius_mm):
        return ValidationResult(False, "invalid_expected_current_radius_mm")
    if not _is_finite_positive_number(recommended_radius_mm):
        return ValidationResult(False, "invalid_recommended_radius_mm")

    if recommended_radius_mm > maximum_plant_radius_mm:
        return ValidationResult(False, "radius_exceeds_maximum")

    actual_radius = point.get("radius")
    if not isinstance(actual_radius, (int, float)) or isinstance(actual_radius, bool):
        return ValidationResult(False, "current_radius_unknown")

    if abs(actual_radius - expected_current_radius_mm) > RADIUS_TOLERANCE_MM:
        return ValidationResult(False, "stale_radius")

    if recommended_radius_mm < actual_radius and not allow_automatic_shrink:
        return ValidationResult(False, "shrink_not_allowed")

    return ValidationResult(True, None)


# -------------------- spread-curve validation --------------------

def validate_curve_name(name: Any) -> ValidationResult:
    if not isinstance(name, str) or not name.startswith(CURVE_NAME_PREFIX):
        return ValidationResult(False, "name_missing_vision_prefix")
    return ValidationResult(True, None)


def validate_curve_data(data: Any) -> ValidationResult:
    """Validate a proposed spread-curve ``data`` mapping (day string -> diameter mm)."""
    if not isinstance(data, dict) or not data:
        return ValidationResult(False, "curve_data_empty")
    if len(data) > MAX_CURVE_CONTROL_POINTS:
        return ValidationResult(False, "too_many_control_points")

    try:
        points = sorted((int(day), data[day]) for day in data)
    except (TypeError, ValueError):
        return ValidationResult(False, "invalid_day_key")

    last_day: int | None = None
    last_value: int | None = None
    for day, value in points:
        if day < 0:
            return ValidationResult(False, "negative_day")
        if last_day is not None and day <= last_day:
            return ValidationResult(False, "days_not_increasing")
        if not isinstance(value, int) or isinstance(value, bool):
            return ValidationResult(False, "value_not_integer")
        if value < 0:
            return ValidationResult(False, "negative_diameter")
        if last_value is not None and value < last_value:
            return ValidationResult(False, "values_not_monotonic")
        last_day, last_value = day, value

    return ValidationResult(True, None)


def validate_curve_upsert(
    *,
    curve_id: Any,
    name: Any,
    data: Any,
    existing_curve: dict[str, Any] | None,
) -> ValidationResult:
    """Validate a full upsert request: name, data, and ownership of `curve_id`."""
    name_result = validate_curve_name(name)
    if not name_result.ok:
        return name_result

    data_result = validate_curve_data(data)
    if not data_result.ok:
        return data_result

    if curve_id is not None:
        if existing_curve is None:
            return ValidationResult(False, "curve_not_found")
        if not is_vision_owned_curve(existing_curve):
            return ValidationResult(False, "curve_not_vision_owned")

    return ValidationResult(True, None)


def validate_plant_assignment(plant: dict[str, Any] | None, *, device_id: Any) -> ValidationResult:
    """Validate that a plant is eligible to be assigned a FarmBot Vision curve."""
    if plant is None:
        return ValidationResult(False, "plant_not_found")
    if str(plant.get("device_id")) != str(device_id):
        return ValidationResult(False, "wrong_device")
    if plant.get("pointer_type") != POINTER_TYPE_PLANT:
        return ValidationResult(False, "not_a_plant")
    if plant.get("discarded_at"):
        return ValidationResult(False, "plant_archived")
    return ValidationResult(True, None)
