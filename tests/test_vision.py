"""Unit tests for custom_components/farmbot/vision.py.

These are plain data-in/data-out tests -- no HA, no aiohttp, no mocking.
"""
from datetime import datetime, timedelta, timezone

from custom_components.farmbot import vision

# --------------------------- plant filtering ---------------------------

PLANT_ACTIVE = {"id": 1, "pointer_type": "Plant", "plant_stage": "planted", "discarded_at": None}
PLANT_SPROUTED = {"id": 2, "pointer_type": "Plant", "plant_stage": "sprouted"}
PLANT_HARVESTED = {"id": 3, "pointer_type": "Plant", "plant_stage": "harvested"}
PLANT_REMOVED = {"id": 4, "pointer_type": "Plant", "plant_stage": "removed"}
PLANT_DISCARDED = {
    "id": 5, "pointer_type": "Plant", "plant_stage": "planted", "discarded_at": "2026-01-01"
}
WEED_POINT = {"id": 6, "pointer_type": "Weed", "plant_stage": "planted"}
GENERIC_POINT = {"id": 7, "pointer_type": "GenericPointer"}


def test_filter_active_plants_keeps_planted_sprouted_active():
    plants = [PLANT_ACTIVE, PLANT_SPROUTED]
    assert vision.filter_active_plants(plants) == plants


def test_filter_active_plants_excludes_harvested_removed_discarded():
    result = vision.filter_active_plants(
        [PLANT_ACTIVE, PLANT_HARVESTED, PLANT_REMOVED, PLANT_DISCARDED]
    )
    assert result == [PLANT_ACTIVE]


def test_filter_active_plants_excludes_non_plant_pointer_types():
    result = vision.filter_active_plants([PLANT_ACTIVE, WEED_POINT, GENERIC_POINT])
    assert result == [PLANT_ACTIVE]


def test_project_plant_only_includes_documented_fields():
    point = dict(PLANT_ACTIVE, secret_field="leaked?", name="Tomato 1")
    projected = vision.project_plant(point)
    assert set(projected) == set(vision.PLANT_FIELDS)
    assert "secret_field" not in projected
    assert projected["name"] == "Tomato 1"


def test_project_weed_only_includes_vision_map_fields():
    point = dict(
        WEED_POINT,
        name=None,
        x=120,
        y=340,
        z=0,
        radius=18,
        secret_field="leaked?",
    )
    projected = vision.project_weed(point)
    assert set(projected) == set(vision.WEED_FIELDS)
    assert "secret_field" not in projected
    assert projected["radius"] == 18


# --------------------------- image lookback filtering ---------------------------

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _image(image_id, hours_ago, processed=True):
    created = NOW - timedelta(hours=hours_ago)
    return {
        "id": image_id,
        "created_at": created.isoformat(),
        "attachment_processed_at": created.isoformat() if processed else None,
        "meta": {"x": 1, "y": 2, "z": 3},
    }


def test_filter_recent_processed_images_respects_lookback_window():
    images = [_image(1, hours_ago=1), _image(2, hours_ago=100)]
    result = vision.filter_recent_processed_images(images, now=NOW, lookback_hours=72)
    assert [i["id"] for i in result] == [1]


def test_filter_recent_processed_images_excludes_unprocessed():
    images = [_image(1, hours_ago=1, processed=True), _image(2, hours_ago=1, processed=False)]
    result = vision.filter_recent_processed_images(images, now=NOW, lookback_hours=72)
    assert [i["id"] for i in result] == [1]


def test_project_image_never_includes_attachment_url():
    image = _image(1, hours_ago=1)
    image["attachment_url"] = "https://signed.example.com/secret?token=abc"
    projected = vision.project_image(image)
    assert "attachment_url" not in projected
    assert "url" not in projected


# --------------------------- unit conversion ---------------------------

def test_radius_to_diameter_conversion():
    assert vision.radius_mm_to_diameter_mm(100) == 200
    assert vision.diameter_mm_to_radius_mm(200) == 100


def test_radius_and_diameter_are_not_interchangeable_for_the_same_plant():
    # A 60mm-radius plant is a 120mm-diameter plant -- never the same number.
    radius = 60
    diameter = vision.radius_mm_to_diameter_mm(radius)
    assert diameter != radius
    assert vision.diameter_mm_to_radius_mm(diameter) == radius


# --------------------------- radius validation ---------------------------

def _point(**overrides):
    base = {
        "id": 10, "device_id": "42", "pointer_type": "Plant",
        "discarded_at": None, "radius": 120.0,
    }
    base.update(overrides)
    return base


def test_validate_radius_change_accepts_valid_increase():
    result = vision.validate_radius_change(
        point=_point(),
        device_id="42",
        expected_current_radius_mm=120.0,
        recommended_radius_mm=150.0,
    )
    assert result.ok


def test_validate_radius_change_rejects_missing_plant():
    result = vision.validate_radius_change(
        point=None, device_id="42", expected_current_radius_mm=120.0,
        recommended_radius_mm=150.0,
    )
    assert not result.ok
    assert result.reason == "plant_not_found"


def test_validate_radius_change_rejects_wrong_device():
    result = vision.validate_radius_change(
        point=_point(device_id="99"), device_id="42", expected_current_radius_mm=120.0,
        recommended_radius_mm=150.0,
    )
    assert not result.ok
    assert result.reason == "wrong_device"


def test_validate_radius_change_rejects_non_plant_pointer():
    result = vision.validate_radius_change(
        point=_point(pointer_type="Weed"), device_id="42", expected_current_radius_mm=120.0,
        recommended_radius_mm=150.0,
    )
    assert not result.ok
    assert result.reason == "not_a_plant"


def test_validate_radius_change_rejects_archived_plant():
    result = vision.validate_radius_change(
        point=_point(discarded_at="2026-01-01"), device_id="42",
        expected_current_radius_mm=120.0, recommended_radius_mm=150.0,
    )
    assert not result.ok
    assert result.reason == "plant_archived"


def test_validate_radius_change_detects_stale_current_radius():
    result = vision.validate_radius_change(
        point=_point(radius=200.0), device_id="42", expected_current_radius_mm=120.0,
        recommended_radius_mm=150.0,
    )
    assert not result.ok
    assert result.reason == "stale_radius"


def test_validate_radius_change_tolerates_small_rounding_difference():
    result = vision.validate_radius_change(
        point=_point(radius=120.3), device_id="42", expected_current_radius_mm=120.0,
        recommended_radius_mm=150.0,
    )
    assert result.ok


def test_validate_radius_change_allows_shrink():
    # Automatic-shrink permission and the maximum-radius cap are the FarmBot
    # Vision app's responsibility (its own settings already govern every
    # write it proposes); the integration only re-verifies plant identity
    # and freshness against FarmBot's live data.
    result = vision.validate_radius_change(
        point=_point(radius=120.0), device_id="42", expected_current_radius_mm=120.0,
        recommended_radius_mm=90.0,
    )
    assert result.ok


def test_validate_radius_change_rejects_non_positive_and_nan_values():
    for bad in (0, -5, float("nan")):
        result = vision.validate_radius_change(
            point=_point(), device_id="42", expected_current_radius_mm=120.0,
            recommended_radius_mm=bad,
        )
        assert not result.ok
        assert result.reason == "invalid_recommended_radius_mm"


def test_validate_removal_accepts_owned_current_plant():
    result = vision.validate_removal(
        point=_point(), device_id="42", expected_current_radius_mm=120.0
    )
    assert result.ok


def test_validate_removal_rejects_stale_or_removed_plant():
    stale = vision.validate_removal(
        point=_point(radius=200.0), device_id="42", expected_current_radius_mm=120.0
    )
    removed = vision.validate_removal(
        point=_point(plant_stage="removed"), device_id="42", expected_current_radius_mm=120.0
    )
    assert stale.reason == "stale_radius"
    assert removed.reason == "plant_archived"


def test_radius_and_removal_accept_zero_current_radius_but_reject_inactive_stages():
    zero = _point(radius=0.0)
    radius = vision.validate_radius_change(
        point=zero,
        device_id="42",
        expected_current_radius_mm=0.0,
        recommended_radius_mm=10.0,
    )
    removal = vision.validate_removal(
        point=zero, device_id="42", expected_current_radius_mm=0.0
    )
    harvested = vision.validate_radius_change(
        point=_point(plant_stage="harvested"),
        device_id="42",
        expected_current_radius_mm=120.0,
        recommended_radius_mm=150.0,
    )

    assert radius.ok
    assert removal.ok
    assert harvested.reason == "plant_archived"


# --------------------------- curve validation ---------------------------

def test_validate_curve_name_requires_vision_prefix():
    assert vision.validate_curve_name("[FarmBot Vision] Tomato").ok
    assert not vision.validate_curve_name("Tomato").ok


def test_validate_curve_data_accepts_monotonic_increasing():
    assert vision.validate_curve_data({"0": 10, "10": 40, "30": 120}).ok


def test_validate_curve_data_rejects_non_monotonic_values():
    result = vision.validate_curve_data({"0": 10, "10": 40, "30": 20})
    assert not result.ok
    assert result.reason == "values_not_monotonic"


def test_validate_curve_data_rejects_duplicate_day_after_int_conversion():
    # "05" and "5" are distinct dict keys but the same day once converted --
    # data is sorted by day before validation, so this is the only way two
    # entries can land on the same "day" and fail the increasing-day check.
    result = vision.validate_curve_data({"05": 10, "5": 40})
    assert not result.ok
    assert result.reason == "days_not_increasing"


def test_validate_curve_data_rejects_decreasing_values_regardless_of_key_order():
    result = vision.validate_curve_data({"20": 10, "10": 40})
    assert not result.ok
    assert result.reason == "values_not_monotonic"


def test_validate_curve_data_rejects_too_many_control_points():
    data = {str(day): day for day in range(0, 22, 2)}  # 11 points
    result = vision.validate_curve_data(data)
    assert not result.ok
    assert result.reason == "too_many_control_points"


def test_validate_curve_data_rejects_negative_values():
    result = vision.validate_curve_data({"0": -5})
    assert not result.ok
    assert result.reason == "negative_diameter"


def test_validate_curve_upsert_rejects_modifying_user_created_curve():
    user_curve = {"id": 5, "name": "My Tomatoes", "type": "spread", "data": {"0": 10}}
    result = vision.validate_curve_upsert(
        curve_id=5, name="[FarmBot Vision] Tomato", data={"0": 10, "20": 40},
        existing_curve=user_curve,
    )
    assert not result.ok
    assert result.reason == "curve_not_vision_owned"


def test_validate_curve_upsert_allows_updating_vision_owned_curve():
    vision_curve = {
        "id": 5, "name": "[FarmBot Vision] Tomato", "type": "spread", "data": {"0": 10},
    }
    result = vision.validate_curve_upsert(
        curve_id=5, name="[FarmBot Vision] Tomato", data={"0": 10, "20": 40},
        existing_curve=vision_curve,
    )
    assert result.ok


def test_validate_curve_upsert_rejects_missing_curve_id():
    result = vision.validate_curve_upsert(
        curve_id=999, name="[FarmBot Vision] Tomato", data={"0": 10}, existing_curve=None
    )
    assert not result.ok
    assert result.reason == "curve_not_found"


def test_is_vision_owned_curve_requires_prefix_and_spread_type():
    assert vision.is_vision_owned_curve(
        {"name": "[FarmBot Vision] Tomato", "type": "spread"}
    )
    assert not vision.is_vision_owned_curve({"name": "[FarmBot Vision] Tomato", "type": "water"})
    assert not vision.is_vision_owned_curve({"name": "Tomato", "type": "spread"})


def test_select_relevant_curves_includes_assigned_and_vision_owned_only():
    plants = [{"id": 1, "spread_curve_id": 5}]
    curves = [
        {"id": 5, "name": "User Curve", "type": "spread", "data": {}},
        {"id": 6, "name": "[FarmBot Vision] Basil", "type": "spread", "data": {}},
        {"id": 7, "name": "Unrelated", "type": "spread", "data": {}},
    ]
    result = vision.select_relevant_curves(plants, curves)
    assert {c["id"] for c in result} == {5, 6}


def test_select_relevant_curves_include_all_returns_everything():
    plants = []
    curves = [{"id": 1, "name": "A", "type": "spread", "data": {}}]
    result = vision.select_relevant_curves(plants, curves, include_all=True)
    assert len(result) == 1


# --------------------------- plant assignment validation ---------------------------

def test_validate_plant_assignment_rejects_wrong_bot():
    result = vision.validate_plant_assignment(_point(device_id="99"), device_id="42")
    assert not result.ok
    assert result.reason == "wrong_device"


def test_validate_plant_assignment_accepts_matching_plant():
    result = vision.validate_plant_assignment(_point(), device_id="42")
    assert result.ok


# --------------------------- calibration mapping (contract v2) ---------------------------

_RAW_CALIBRATION = {
    "available": True,
    "coord_scale": 2.0,  # mm per pixel at native resolution
    "center_pixel_location_x": 1296,  # -> reference width 2592
    "center_pixel_location_y": 972,  # -> reference height 1944
    "total_rotation_angle": 3.0,
    "camera_offset_x": 5.0,
    "camera_offset_y": -4.0,
}


def test_map_camera_calibration_inverts_scale_and_derives_reference():
    mapped = vision.map_camera_calibration(_RAW_CALIBRATION)
    assert mapped["available"] is True
    assert mapped["pixels_per_mm_x"] == 0.5  # 1 / coord_scale
    assert mapped["pixels_per_mm_y"] == 0.5
    assert mapped["reference_width"] == 2592
    assert mapped["reference_height"] == 1944
    assert mapped["rotation_degrees"] == 3.0
    assert mapped["basis"] == "native_frame"


def test_map_camera_calibration_unavailable_when_missing_core_values():
    assert vision.map_camera_calibration({"available": True})["available"] is False
    assert vision.map_camera_calibration({"available": False})["available"] is False
    assert vision.map_camera_calibration(None)["available"] is False
    bad = dict(_RAW_CALIBRATION, coord_scale=0)
    assert vision.map_camera_calibration(bad)["available"] is False


def test_build_processed_calibration_scales_to_processed_pixels():
    reference = vision.map_camera_calibration(_RAW_CALIBRATION)
    processed = vision.build_processed_calibration(reference, width=960, height=720)
    assert processed["basis"] == "processed_image"
    assert processed["width"] == 960 and processed["height"] == 720
    # 0.5 px/mm * 960 / 2592
    assert abs(processed["pixels_per_mm_x"] - 0.5 * 960 / 2592) < 1e-9


def test_build_processed_calibration_none_when_reference_unavailable():
    assert vision.build_processed_calibration({"available": False}, width=960, height=720) is None
    assert vision.build_processed_calibration(None, width=960, height=720) is None
