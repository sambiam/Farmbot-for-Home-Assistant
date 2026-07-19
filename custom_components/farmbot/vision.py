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


# -------------------- device identity --------------------

def normalize_device_id(value: Any) -> str:
    """Reduce a FarmBot device identifier to its canonical numeric form.

    FarmBot exposes a device's identity in two interchangeable ways: the
    REST API returns records carrying a bare numeric ``device_id`` (e.g.
    ``3379``), while the JWT ``bot`` claim -- which the integration stores as
    ``manager.device_id`` -- uses the ``device_<id>`` username form (e.g.
    ``"device_3379"``). Comparing the two forms verbatim rejects every
    legitimately-owned record, so ownership checks must first normalize both
    sides to the same form.
    """
    text = str(value).strip()
    if text.startswith("device_"):
        text = text[len("device_"):]
    return text


def same_device(a: Any, b: Any) -> bool:
    """True when two FarmBot device identifiers denote the same device.

    Accepts either identifier in numeric (``3379``) or ``device_<id>``
    (``"device_3379"``) form; see :func:`normalize_device_id`. This is the
    single source of truth for "does this record belong to this FarmBot?",
    shared by every ownership check so they can never disagree.
    """
    return normalize_device_id(a) == normalize_device_id(b)


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


# -------------------- calibration --------------------

# Contract version emitted to the FarmBot Vision app.
VISION_CONTRACT_VERSION = "farmbot-vision-v2"


def map_camera_calibration(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Map FarmBot farmware calibration values to the app's reference shape.

    FarmBot stores ``coord_scale`` as millimetres-per-pixel at the native
    camera resolution. The app expects ``pixels_per_mm`` at a stated reference
    resolution, so we invert the scale and derive the reference dimensions from
    ``center_pixel_location_*`` (half-frame). Any missing or non-positive core
    value yields ``{"available": False}`` rather than a guessed calibration.
    """
    if not isinstance(raw, dict) or not raw.get("available"):
        return {"available": False}
    coord_scale = raw.get("coord_scale")
    cx = raw.get("center_pixel_location_x")
    cy = raw.get("center_pixel_location_y")
    if not (
        _is_finite_positive_number(coord_scale)
        and _is_finite_positive_number(cx)
        and _is_finite_positive_number(cy)
    ):
        return {"available": False}
    pixels_per_mm = 1.0 / coord_scale
    return {
        "available": True,
        "pixels_per_mm_x": pixels_per_mm,
        "pixels_per_mm_y": pixels_per_mm,
        "rotation_degrees": float(raw.get("total_rotation_angle") or 0.0),
        "offset_x_mm": float(raw.get("camera_offset_x") or 0.0),
        "offset_y_mm": float(raw.get("camera_offset_y") or 0.0),
        "reference_width": int(round(cx * 2)),
        "reference_height": int(round(cy * 2)),
        "basis": "native_frame",
    }


def build_processed_calibration(
    reference: dict[str, Any] | None, *, width: int, height: int
) -> dict[str, Any] | None:
    """Scale a reference calibration to an exact processed image.

    Returns a ``processed_image`` basis calibration the app can use directly,
    or ``None`` when the reference is unavailable/incomplete. This is the same
    documented transform the app would otherwise apply; doing it here lets the
    app prefer a calibration already tied to the returned pixels.
    """
    if not isinstance(reference, dict) or not reference.get("available"):
        return None
    ref_w = reference.get("reference_width")
    ref_h = reference.get("reference_height")
    ppm_x = reference.get("pixels_per_mm_x")
    ppm_y = reference.get("pixels_per_mm_y")
    if not (
        _is_finite_positive_number(ref_w)
        and _is_finite_positive_number(ref_h)
        and _is_finite_positive_number(ppm_x)
        and _is_finite_positive_number(ppm_y)
    ):
        return None
    return {
        "available": True,
        "pixels_per_mm_x": ppm_x * width / ref_w,
        "pixels_per_mm_y": ppm_y * height / ref_h,
        "rotation_degrees": float(reference.get("rotation_degrees") or 0.0),
        "offset_x_mm": float(reference.get("offset_x_mm") or 0.0),
        "offset_y_mm": float(reference.get("offset_y_mm") or 0.0),
        "basis": "processed_image",
        "width": width,
        "height": height,
    }


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


# -------------------- camera calibration --------------------
#
# FarmBot stores camera calibration as loose "farmware env" key/value pairs
# (the CAMERA_CALIBRATION_* keys). Their meaning and units were verified
# against FarmBot's own plant-detection Farmware, the reference
# implementation for this naming convention
# (github.com/FarmBot-Labs/plant-detection, plant_detection/P2C.py):
#
#   coord_scale                -- millimetres per pixel (mm/px). In P2C the
#                                 pixel->coordinate conversion multiplies a
#                                 pixel offset by coord_scale to get mm, and it
#                                 is derived as known_mm_separation / pixel_
#                                 separation. Pixels-per-mm is therefore its
#                                 reciprocal, 1 / coord_scale. FarmBot uses a
#                                 single isotropic scale, so x and y share it.
#   center_pixel_location_x/y  -- the pixel coordinates of the image centre, in
#                                 the native (EXIF-oriented) capture resolution,
#                                 computed by FarmBot as int(dimension / 2). The
#                                 native reference dimensions are therefore
#                                 2 * center_pixel_location (see note below).
#   total_rotation_angle       -- degrees; FarmBot applies it as a whole-image
#                                 rotation to align the camera with the bed.
#                                 Passed through unchanged; sign follows
#                                 FarmBot's stored convention.
#   camera_offset_x/y          -- millimetre offset from the bot (UTM) position
#                                 to the camera centre, in FarmBot coordinates.
#   camera_z                   -- the Z height (mm) at which calibration was
#                                 captured; retained for reference only.
#
# The normalized ``basis`` is ``"oriented_native_image"``: FarmBot camera
# frames carry no EXIF orientation, so the oriented native image is the raw
# capture, and that is the coordinate system these values describe.

_CAMERA_CALIBRATION_MIN_REFERENCE_DIMENSION = 2  # center*2 must yield a real image
CAMERA_CALIBRATION_BASIS = "oriented_native_image"
PROCESSED_CALIBRATION_BASIS = "processed_image"


def normalize_camera_calibration(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Convert raw FarmBot CAMERA_CALIBRATION_* values into normalized fields.

    Returns the structure the FarmBot Vision companion app expects, with
    unambiguous units (pixels-per-mm, millimetres, degrees) and explicit
    native reference dimensions. Returns ``{"available": False}`` whenever the
    raw values are missing, non-finite, or cannot be converted safely --
    missing values are never manufactured, and raw fields are never surfaced
    under names the app would misinterpret. This preserves the companion
    app's manual-calibration fallback: an unavailable result tells it to fall
    back rather than trust a guess.
    """
    if not isinstance(raw, dict) or not raw.get("available"):
        return {"available": False}

    coord_scale = raw.get("coord_scale")
    center_x = raw.get("center_pixel_location_x")
    center_y = raw.get("center_pixel_location_y")

    # coord_scale is mm/px; pixels-per-mm is its reciprocal and must be finite
    # and strictly positive.
    if not _is_finite_positive_number(coord_scale):
        return {"available": False}
    pixels_per_mm = 1.0 / coord_scale
    if not math.isfinite(pixels_per_mm) or pixels_per_mm <= 0:
        return {"available": False}

    # The native reference dimensions are derived from the calibration image
    # centre (FarmBot stores centre = int(dimension / 2)). Without a usable
    # centre we cannot describe which coordinate system this calibration
    # belongs to, so we report it unavailable rather than guess.
    if not _is_finite_positive_number(center_x) or not _is_finite_positive_number(center_y):
        return {"available": False}
    reference_width = int(round(center_x * 2))
    reference_height = int(round(center_y * 2))
    if (
        reference_width < _CAMERA_CALIBRATION_MIN_REFERENCE_DIMENSION
        or reference_height < _CAMERA_CALIBRATION_MIN_REFERENCE_DIMENSION
    ):
        return {"available": False}

    rotation = raw.get("total_rotation_angle", 0.0)
    if not isinstance(rotation, (int, float)) or isinstance(rotation, bool) or not math.isfinite(
        rotation
    ):
        return {"available": False}

    # Offsets are optional in FarmBot's stored env and genuinely default to 0
    # mm (camera mounted at the UTM). A present-but-non-finite offset is an
    # error, not a benign default, so it makes the whole calibration
    # unavailable rather than being silently zeroed.
    offset_x = _optional_finite_number(raw.get("camera_offset_x"))
    offset_y = _optional_finite_number(raw.get("camera_offset_y"))
    if offset_x is None or offset_y is None:
        return {"available": False}

    return {
        "available": True,
        "pixels_per_mm_x": pixels_per_mm,
        "pixels_per_mm_y": pixels_per_mm,
        "rotation_degrees": float(rotation),
        "offset_x_mm": offset_x,
        "offset_y_mm": offset_y,
        "reference_width": reference_width,
        "reference_height": reference_height,
        "basis": CAMERA_CALIBRATION_BASIS,
    }


def compute_processed_calibration(
    normalized: dict[str, Any] | None,
    *,
    oriented_width: int,
    oriented_height: int,
    processed_width: int,
    processed_height: int,
) -> dict[str, Any]:
    """Rescale normalized native calibration to the returned processed image.

    ``pixels_per_mm`` scales linearly with resolution::

        processed_pixels_per_mm_x = reference_pixels_per_mm_x
                                    * processed_width / reference_width

    and likewise for y. Rotation and millimetre offsets are resolution
    independent and carry through unchanged.

    Returns ``{"available": False, "basis": "processed_image"}`` unless the
    source orientation is understood (the oriented native dimensions match the
    calibration's reference dimensions), the scaling is valid, and every
    resulting value is finite and positive -- so the companion app never has
    to guess which image coordinate system a calibration belongs to.
    """
    unavailable = {"available": False, "basis": PROCESSED_CALIBRATION_BASIS}

    if not isinstance(normalized, dict) or not normalized.get("available"):
        return unavailable

    reference_width = normalized.get("reference_width")
    reference_height = normalized.get("reference_height")
    ref_ppm_x = normalized.get("pixels_per_mm_x")
    ref_ppm_y = normalized.get("pixels_per_mm_y")

    if not _is_positive_int(reference_width) or not _is_positive_int(reference_height):
        return unavailable
    if not _is_positive_int(oriented_width) or not _is_positive_int(oriented_height):
        return unavailable
    if not _is_positive_int(processed_width) or not _is_positive_int(processed_height):
        return unavailable
    if not _is_finite_positive_number(ref_ppm_x) or not _is_finite_positive_number(ref_ppm_y):
        return unavailable

    # The oriented native image must be the exact coordinate system the
    # calibration was captured in; otherwise the reference pixels-per-mm does
    # not apply to this frame and we must not rescale it.
    if oriented_width != reference_width or oriented_height != reference_height:
        return unavailable

    processed_ppm_x = ref_ppm_x * processed_width / reference_width
    processed_ppm_y = ref_ppm_y * processed_height / reference_height
    if not (math.isfinite(processed_ppm_x) and processed_ppm_x > 0):
        return unavailable
    if not (math.isfinite(processed_ppm_y) and processed_ppm_y > 0):
        return unavailable

    return {
        "available": True,
        "pixels_per_mm_x": processed_ppm_x,
        "pixels_per_mm_y": processed_ppm_y,
        "rotation_degrees": float(normalized.get("rotation_degrees", 0.0)),
        "offset_x_mm": float(normalized.get("offset_x_mm", 0.0)),
        "offset_y_mm": float(normalized.get("offset_y_mm", 0.0)),
        "basis": PROCESSED_CALIBRATION_BASIS,
        "width": processed_width,
        "height": processed_height,
    }


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


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _optional_finite_number(value: Any) -> float | None:
    """Return ``value`` as a float when absent (->0.0) or finite; else ``None``.

    ``None``/missing is a benign default of 0.0; a present but non-finite or
    non-numeric value is an error signalled by returning ``None``.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


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
    if not same_device(point.get("device_id"), device_id):
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
    if not same_device(plant.get("device_id"), device_id):
        return ValidationResult(False, "wrong_device")
    if plant.get("pointer_type") != POINTER_TYPE_PLANT:
        return ValidationResult(False, "not_a_plant")
    if plant.get("discarded_at"):
        return ValidationResult(False, "plant_archived")
    return ValidationResult(True, None)
