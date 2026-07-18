"""Unit tests for camera-calibration normalization and rescaling (vision.py).

Verifies the CAMERA_CALIBRATION_* interpretation documented in vision.py
against the FarmBot plant-detection reference (coord_scale is mm/pixel, so
pixels-per-mm is its reciprocal; native reference dimensions are derived from
center_pixel_location * 2).
"""
import math

import pytest

from custom_components.farmbot import vision


def _raw(**overrides):
    base = {
        "available": True,
        "coord_scale": 0.8130081,        # mm/px -> 1/0.8130081 ~= 1.23 px/mm
        "center_pixel_location_x": 1296,  # -> reference_width  2592
        "center_pixel_location_y": 972,   # -> reference_height 1944
        "camera_z": 300.0,
        "total_rotation_angle": 0.0,
        "camera_offset_x": 0.0,
        "camera_offset_y": 0.0,
    }
    base.update(overrides)
    return base


# --------------------------- normalize_camera_calibration ---------------------------

def test_normalize_produces_expected_structure():
    result = vision.normalize_camera_calibration(_raw())
    assert result["available"] is True
    assert result["pixels_per_mm_x"] == pytest.approx(1.23, abs=1e-3)
    assert result["pixels_per_mm_y"] == pytest.approx(1.23, abs=1e-3)
    assert result["rotation_degrees"] == 0.0
    assert result["offset_x_mm"] == 0.0
    assert result["offset_y_mm"] == 0.0
    assert result["reference_width"] == 2592
    assert result["reference_height"] == 1944
    assert result["basis"] == "oriented_native_image"


def test_normalize_units_are_reciprocal_of_coord_scale():
    result = vision.normalize_camera_calibration(_raw(coord_scale=2.0))
    assert result["pixels_per_mm_x"] == pytest.approx(0.5)
    assert result["pixels_per_mm_y"] == pytest.approx(0.5)


def test_normalize_passes_through_rotation_and_offsets():
    result = vision.normalize_camera_calibration(
        _raw(total_rotation_angle=12.5, camera_offset_x=-40.0, camera_offset_y=15.0)
    )
    assert result["rotation_degrees"] == 12.5
    assert result["offset_x_mm"] == -40.0
    assert result["offset_y_mm"] == 15.0


def test_normalize_defaults_absent_offsets_to_zero():
    raw = _raw()
    del raw["camera_offset_x"]
    del raw["camera_offset_y"]
    result = vision.normalize_camera_calibration(raw)
    assert result["available"] is True
    assert result["offset_x_mm"] == 0.0
    assert result["offset_y_mm"] == 0.0


def test_normalize_unavailable_input_stays_unavailable():
    assert vision.normalize_camera_calibration({"available": False}) == {"available": False}
    assert vision.normalize_camera_calibration(None) == {"available": False}
    assert vision.normalize_camera_calibration({}) == {"available": False}


@pytest.mark.parametrize("bad_scale", [0.0, -1.0, float("inf"), float("nan")])
def test_normalize_rejects_bad_coord_scale(bad_scale):
    assert vision.normalize_camera_calibration(_raw(coord_scale=bad_scale)) == {
        "available": False
    }


def test_normalize_ambiguous_without_center_pixel_is_unavailable():
    """Cannot derive native reference dimensions -> report unavailable, not a guess."""
    raw = _raw()
    del raw["center_pixel_location_x"]
    assert vision.normalize_camera_calibration(raw) == {"available": False}


def test_normalize_rejects_non_finite_offset():
    assert vision.normalize_camera_calibration(_raw(camera_offset_x=float("inf"))) == {
        "available": False
    }


def test_normalize_rejects_non_finite_rotation():
    assert vision.normalize_camera_calibration(_raw(total_rotation_angle=float("nan"))) == {
        "available": False
    }


# --------------------------- compute_processed_calibration ---------------------------

def test_processed_calibration_rescales_pixels_per_mm():
    normalized = vision.normalize_camera_calibration(_raw())
    processed = vision.compute_processed_calibration(
        normalized,
        oriented_width=2592,
        oriented_height=1944,
        processed_width=960,
        processed_height=720,
    )
    assert processed["available"] is True
    assert processed["basis"] == "processed_image"
    assert processed["width"] == 960
    assert processed["height"] == 720
    # 1.23 * 960/2592 ~= 0.455
    assert processed["pixels_per_mm_x"] == pytest.approx(0.455, abs=1e-3)
    assert processed["pixels_per_mm_y"] == pytest.approx(0.455, abs=1e-3)
    assert processed["rotation_degrees"] == 0.0
    assert processed["offset_x_mm"] == 0.0
    assert processed["offset_y_mm"] == 0.0


def test_processed_calibration_matches_resize_scale_relationship():
    normalized = vision.normalize_camera_calibration(_raw())
    processed = vision.compute_processed_calibration(
        normalized,
        oriented_width=2592,
        oriented_height=1944,
        processed_width=1280,
        processed_height=960,
    )
    scale = 1280 / 2592
    assert processed["pixels_per_mm_x"] == pytest.approx(
        normalized["pixels_per_mm_x"] * scale
    )


def test_processed_calibration_unavailable_when_calibration_unavailable():
    assert vision.compute_processed_calibration(
        {"available": False},
        oriented_width=2592,
        oriented_height=1944,
        processed_width=960,
        processed_height=720,
    ) == {"available": False, "basis": "processed_image"}


def test_processed_calibration_unavailable_when_oriented_mismatches_reference():
    """Oriented native frame differs from calibration basis -> not rescalable."""
    normalized = vision.normalize_camera_calibration(_raw())
    result = vision.compute_processed_calibration(
        normalized,
        oriented_width=1920,   # not the 2592 reference
        oriented_height=1080,
        processed_width=960,
        processed_height=540,
    )
    assert result == {"available": False, "basis": "processed_image"}


def test_processed_calibration_rejects_non_positive_processed_dimensions():
    normalized = vision.normalize_camera_calibration(_raw())
    result = vision.compute_processed_calibration(
        normalized,
        oriented_width=2592,
        oriented_height=1944,
        processed_width=0,
        processed_height=720,
    )
    assert result == {"available": False, "basis": "processed_image"}


def test_processed_calibration_values_are_finite_and_positive():
    normalized = vision.normalize_camera_calibration(_raw())
    processed = vision.compute_processed_calibration(
        normalized,
        oriented_width=2592,
        oriented_height=1944,
        processed_width=640,
        processed_height=480,
    )
    assert math.isfinite(processed["pixels_per_mm_x"]) and processed["pixels_per_mm_x"] > 0
    assert math.isfinite(processed["pixels_per_mm_y"]) and processed["pixels_per_mm_y"] > 0
