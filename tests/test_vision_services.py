"""Integration-style tests for the FarmBot Vision bridge service handlers.

Registers the real services via _async_register_services and calls them
through FakeHass.services.async_call, with FarmbotManager.api swapped for
a FakeVisionApi double so no network access occurs. MQTT is never touched
(connect_mqtt/disconnect_mqtt are not called by these tests).
"""
import asyncio
import base64
import logging
import uuid
from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.farmbot import (
    DOMAIN,
    SERVICE_APPLY_VISION_RADIUS,
    SERVICE_EXECUTE_SEQUENCE,
    SERVICE_GET_VISION_IMAGE,
    SERVICE_GET_VISION_INVENTORY,
    SERVICE_LIST_VISION_BOTS,
    SERVICE_MOVE_TO,
    SERVICE_REPORT_VISION_STATUS,
    SERVICE_REQUEST_VISION_ANALYSIS,
    SERVICE_UPSERT_VISION_SPREAD_CURVE,
    _async_register_services,
    _async_remove_services_if_last_entry,
    _vision_response_service,
)
from custom_components.farmbot.const import EVENT_VISION_REQUEST
from custom_components.farmbot.manager import FarmbotManager

from .fake_api import FakeVisionApi
from .helpers import FakeHass
from .test_image_utils import _make_jpeg_bytes


def _run(coro):
    return asyncio.run(coro)


def _make_bot(hass, entry_id="entry-1", device_id="42", options=None):
    entry = ConfigEntry(
        entry_id=entry_id, unique_id=device_id, domain="farmbot",
        data={"token": "tok", "device_id": device_id, "mqtt_host": "mqtt.example.com"},
        options=options or {},
    )
    manager = FarmbotManager(hass, "tok", device_id, "mqtt.example.com", entry=entry)
    manager.api = FakeVisionApi(reauth_callback=manager._trigger_reauth_from_async)
    hass.data.setdefault(DOMAIN, {})[entry_id] = manager
    return manager, entry


async def _call(hass, service, data):
    return await hass.services.async_call(DOMAIN, service, data)


# --------------------------- service registration / removal ---------------------------

def test_registers_all_services_with_one_entry():
    hass = FakeHass()
    _async_register_services(hass)
    for service in (
        SERVICE_EXECUTE_SEQUENCE, SERVICE_MOVE_TO, SERVICE_LIST_VISION_BOTS,
        SERVICE_GET_VISION_INVENTORY, SERVICE_GET_VISION_IMAGE, SERVICE_APPLY_VISION_RADIUS,
        SERVICE_UPSERT_VISION_SPREAD_CURVE, SERVICE_REPORT_VISION_STATUS,
        SERVICE_REQUEST_VISION_ANALYSIS,
    ):
        assert hass.services.has_service(DOMAIN, service)


def test_registers_services_once_with_multiple_entries():
    hass = FakeHass()
    _async_register_services(hass)
    first = dict(hass.services._services)
    _async_register_services(hass)  # simulates a second config entry loading
    assert hass.services._services == first


def test_removes_all_services_after_final_entry_unloads():
    hass = FakeHass()
    _make_bot(hass)
    _async_register_services(hass)
    del hass.data[DOMAIN]["entry-1"]
    _async_remove_services_if_last_entry(hass)
    for service in (
        SERVICE_LIST_VISION_BOTS, SERVICE_GET_VISION_INVENTORY, SERVICE_GET_VISION_IMAGE,
        SERVICE_APPLY_VISION_RADIUS, SERVICE_UPSERT_VISION_SPREAD_CURVE,
        SERVICE_REPORT_VISION_STATUS, SERVICE_REQUEST_VISION_ANALYSIS,
    ):
        assert not hass.services.has_service(DOMAIN, service)


def test_services_remain_while_a_second_entry_is_still_loaded():
    hass = FakeHass()
    _make_bot(hass, entry_id="entry-1")
    _make_bot(hass, entry_id="entry-2", device_id="99")
    _async_register_services(hass)
    del hass.data[DOMAIN]["entry-1"]
    _async_remove_services_if_last_entry(hass)
    assert hass.services.has_service(DOMAIN, SERVICE_GET_VISION_INVENTORY)


# --------------------------- list_vision_bots ---------------------------

def test_list_vision_bots_without_credentials():
    hass = FakeHass()
    _make_bot(hass, entry_id="entry-1", device_id="42")
    _make_bot(hass, entry_id="entry-2", device_id="99")
    _async_register_services(hass)

    result = _run(_call(hass, SERVICE_LIST_VISION_BOTS, {}))

    assert {b["config_entry_id"] for b in result["bots"]} == {"entry-1", "entry-2"}
    dumped = str(result)
    assert "tok" not in dumped
    assert "password" not in dumped.lower()
    assert "@" not in dumped


# --------------------------- get_vision_inventory ---------------------------

def test_get_vision_inventory_filters_plants_images_and_curves():
    hass = FakeHass()
    manager, _entry = _make_bot(hass)
    now = dt_util.utcnow()
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=200)).isoformat()

    manager.api = FakeVisionApi(
        points=[
            {
                "id": 1, "device_id": "42", "pointer_type": "Plant", "plant_stage": "planted",
                "discarded_at": None, "radius": 100, "spread_curve_id": 5, "name": "Tomato",
                "x": 1, "y": 2, "z": 0, "openfarm_slug": "tomato", "planted_at": recent,
            },
            {"id": 2, "device_id": "42", "pointer_type": "Plant", "plant_stage": "harvested"},
        ],
        images=[
            {
                "id": 10, "device_id": "42", "created_at": recent,
                "attachment_processed_at": recent, "meta": {"x": 1, "y": 2, "z": 0},
                "attachment_url": "https://x/1.jpg",
            },
            {
                "id": 11, "device_id": "42", "created_at": old, "attachment_processed_at": old,
                "meta": {}, "attachment_url": "https://x/2.jpg",
            },
            {
                "id": 12, "device_id": "42", "created_at": recent,
                "attachment_processed_at": None, "meta": {}, "attachment_url": "https://x/3.jpg",
            },
        ],
        curves=[
            {"id": 5, "name": "User Curve", "type": "spread", "data": {"0": 10}},
            {"id": 6, "name": "[FarmBot Vision] Basil", "type": "spread", "data": {"0": 5}},
            {"id": 7, "name": "Unrelated", "type": "spread", "data": {}},
        ],
    )
    _async_register_services(hass)

    result = _run(_call(hass, SERVICE_GET_VISION_INVENTORY, {"config_entry_id": "entry-1"}))

    assert [p["id"] for p in result["plants"]] == [1]
    assert [i["id"] for i in result["images"]] == [10]
    assert {c["id"] for c in result["curves"]} == {5, 6}
    assert "attachment_url" not in str(result["images"])
    assert result["camera_calibration"] == {"available": False}


def test_get_vision_inventory_include_all_curves():
    hass = FakeHass()
    manager, _entry = _make_bot(hass)
    manager.api = FakeVisionApi(
        curves=[{"id": 1, "name": "Unrelated", "type": "spread", "data": {}}]
    )
    _async_register_services(hass)

    result = _run(_call(
        hass, SERVICE_GET_VISION_INVENTORY,
        {"config_entry_id": "entry-1", "include_all_curves": True},
    ))
    assert [c["id"] for c in result["curves"]] == [1]


def test_get_vision_inventory_normalizes_camera_calibration():
    hass = FakeHass()
    manager, _entry = _make_bot(hass)
    manager.api = FakeVisionApi(
        calibration={
            "available": True,
            "coord_scale": 0.8130081,
            "center_pixel_location_x": 1296,
            "center_pixel_location_y": 972,
            "camera_z": 300.0,
            "total_rotation_angle": 0.0,
            "camera_offset_x": 0.0,
            "camera_offset_y": 0.0,
        }
    )
    _async_register_services(hass)

    result = _run(_call(hass, SERVICE_GET_VISION_INVENTORY, {"config_entry_id": "entry-1"}))
    cal = result["camera_calibration"]
    assert cal["available"] is True
    assert cal["basis"] == "oriented_native_image"
    assert cal["reference_width"] == 2592
    assert cal["reference_height"] == 1944
    assert cal["pixels_per_mm_x"] == pytest.approx(1.23, abs=1e-3)
    # Raw FarmBot field names must NOT leak through (the app would misread them).
    assert "coord_scale" not in cal
    assert "center_pixel_location_x" not in cal


# --------------------------- get_vision_image ---------------------------

def _image_record(image_id, **overrides):
    base = {
        "id": image_id, "device_id": "42", "attachment_processed_at": "2026-07-17T00:00:00Z",
        "attachment_url": "https://x/img.jpg", "created_at": "2026-07-17T00:00:00Z",
        "meta": {"x": 1, "y": 2, "z": 3},
    }
    base.update(overrides)
    return base


def test_get_vision_image_rejects_wrong_device():
    hass = FakeHass()
    manager, _ = _make_bot(hass, device_id="42")
    manager.api.images[5] = _image_record(5, device_id="99")
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        _run(_call(hass, SERVICE_GET_VISION_IMAGE, {"config_entry_id": "entry-1", "image_id": 5}))


def test_get_vision_image_rejects_unprocessed():
    hass = FakeHass()
    manager, _ = _make_bot(hass)
    manager.api.images[5] = _image_record(5, attachment_processed_at=None)
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        _run(_call(hass, SERVICE_GET_VISION_IMAGE, {"config_entry_id": "entry-1", "image_id": 5}))


def test_get_vision_image_rejects_missing_image():
    hass = FakeHass()
    _make_bot(hass)
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        _run(_call(hass, SERVICE_GET_VISION_IMAGE, {"config_entry_id": "entry-1", "image_id": 999}))


def test_get_vision_image_returns_resized_base64_jpeg_without_leaking_secrets(caplog):
    import hashlib

    hass = FakeHass()
    manager, _ = _make_bot(hass)
    manager.api.images[5] = _image_record(5)
    manager.api.download_bytes = _make_jpeg_bytes(size=(1200, 800))
    _async_register_services(hass)

    caplog.set_level(logging.DEBUG)
    result = _run(_call(
        hass, SERVICE_GET_VISION_IMAGE, {"config_entry_id": "entry-1", "image_id": 5}
    ))

    assert result["content_type"] == "image/jpeg"
    assert result["width"] <= 640
    assert result["height"] <= 480
    decoded = base64.b64decode(result["image_base64"])
    assert decoded[:2] == b"\xff\xd8"  # JPEG magic bytes
    assert result["meta"] == {"x": 1, "y": 2, "z": 3, "created_at": "2026-07-17T00:00:00Z"}

    # Backward-compatible fields still present.
    for field in ("image_id", "content_type", "sha256", "width", "height", "image_base64", "meta"):
        assert field in result

    # New scaling metadata present and correct.
    assert result["source_width"] == 1200
    assert result["source_height"] == 800
    assert result["oriented_width"] == 1200
    assert result["oriented_height"] == 800
    assert result["resize_scale_x"] == pytest.approx(result["width"] / 1200)
    assert result["resize_scale_y"] == pytest.approx(result["height"] / 800)

    # Checksum contract: sha256 is over the returned JPEG bytes, not the source.
    assert result["sha256"] == hashlib.sha256(decoded).hexdigest()
    assert result["source_sha256"] == hashlib.sha256(manager.api.download_bytes).hexdigest()
    assert result["sha256"] != result["source_sha256"]

    # No calibration configured on the fake -> processed calibration unavailable.
    assert result["processed_calibration"] == {
        "available": False, "basis": "processed_image"
    }

    for record in caplog.records:
        message = record.getMessage()
        assert result["image_base64"] not in message
        assert manager.token not in message
        assert "img.jpg" not in message  # signed/attachment URL path never logged


def test_get_vision_image_includes_processed_calibration_when_available():
    hass = FakeHass()
    manager, _ = _make_bot(hass)
    manager.api.images[5] = _image_record(5)
    manager.api.download_bytes = _make_jpeg_bytes(size=(2592, 1944))
    manager.api.calibration = {
        "available": True,
        "coord_scale": 0.8130081,          # -> ~1.23 px/mm
        "center_pixel_location_x": 1296,   # -> reference_width 2592
        "center_pixel_location_y": 972,    # -> reference_height 1944
        "camera_z": 300.0,
        "total_rotation_angle": 0.0,
        "camera_offset_x": 0.0,
        "camera_offset_y": 0.0,
    }
    _async_register_services(hass)

    result = _run(_call(
        hass, SERVICE_GET_VISION_IMAGE,
        {"config_entry_id": "entry-1", "image_id": 5, "max_width": 960, "max_height": 720},
    ))

    assert result["width"] == 960
    assert result["height"] == 720
    cal = result["processed_calibration"]
    assert cal["available"] is True
    assert cal["basis"] == "processed_image"
    assert cal["width"] == 960
    assert cal["height"] == 720
    assert cal["pixels_per_mm_x"] == pytest.approx(0.455, abs=1e-3)
    assert cal["pixels_per_mm_y"] == pytest.approx(0.455, abs=1e-3)


@pytest.mark.parametrize("box", [(640, 480), (960, 720), (1280, 960)])
def test_get_vision_image_supports_configurable_analysis_resolutions(box):
    hass = FakeHass()
    manager, _ = _make_bot(hass)
    manager.api.images[5] = _image_record(5)
    manager.api.download_bytes = _make_jpeg_bytes(size=(2592, 1944))
    _async_register_services(hass)

    result = _run(_call(
        hass, SERVICE_GET_VISION_IMAGE,
        {"config_entry_id": "entry-1", "image_id": 5,
         "max_width": box[0], "max_height": box[1]},
    ))
    assert (result["width"], result["height"]) == box


def test_get_vision_image_rejects_decode_failure():
    hass = FakeHass()
    manager, _ = _make_bot(hass)
    manager.api.images[5] = _image_record(5)
    manager.api.download_bytes = b"not an image"
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        _run(_call(hass, SERVICE_GET_VISION_IMAGE, {"config_entry_id": "entry-1", "image_id": 5}))


# --------------- inventory <-> image ownership agreement (regression) ---------------

def test_inventory_and_image_agree_on_ownership_round_trip():
    """Every image get_vision_inventory lists MUST pass get_vision_image's
    ownership check for the same config entry.

    Reproduces the production identity mismatch: the FarmBot REST API returns
    images with a bare numeric ``device_id`` (e.g. 42), while the config
    entry's ``manager.device_id`` is the JWT ``device_<id>`` username form
    (``"device_42"``). Before the fix the ownership check compared these two
    forms verbatim and rejected every legitimately-owned image.
    """
    hass = FakeHass()
    manager, _entry = _make_bot(hass, device_id="device_42")
    now = dt_util.utcnow()
    recent = (now - timedelta(hours=1)).isoformat()
    manager.api = FakeVisionApi(
        images=[
            {
                "id": image_id, "device_id": 42, "created_at": recent,
                "attachment_processed_at": recent, "meta": {"x": 1, "y": 2, "z": 3},
                "attachment_url": "https://x/img.jpg",
            }
            for image_id in (3043473, 3043472, 3043471, 3043164)
        ],
    )
    manager.api.download_bytes = _make_jpeg_bytes(size=(200, 150))
    _async_register_services(hass)

    inventory = _run(_call(hass, SERVICE_GET_VISION_INVENTORY, {"config_entry_id": "entry-1"}))
    listed_ids = [i["id"] for i in inventory["images"]]
    assert listed_ids == [3043473, 3043472, 3043471, 3043164]

    for image_id in listed_ids:
        result = _run(_call(
            hass, SERVICE_GET_VISION_IMAGE,
            {"config_entry_id": "entry-1", "image_id": image_id},
        ))
        assert result["image_id"] == image_id
        assert result["content_type"] == "image/jpeg"


def test_get_vision_image_rejects_foreign_image_despite_prefixed_device_id():
    """The fix must not loosen ownership: an image belonging to a different
    FarmBot is still rejected, even when the owning bot uses the
    ``device_<id>`` username form.
    """
    hass = FakeHass()
    manager, _ = _make_bot(hass, device_id="device_42")
    manager.api.images[5] = _image_record(5, device_id=99)
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError) as excinfo:
        _run(_call(hass, SERVICE_GET_VISION_IMAGE, {"config_entry_id": "entry-1", "image_id": 5}))
    assert excinfo.value.translation_key == "vision_image_wrong_device"


def test_get_vision_image_wrong_device_surfaces_as_translated_400_not_500():
    """A foreign image is a permanent rejection: it must raise the
    400-mapped ServiceValidationError carrying the translated message, never a
    bare HomeAssistantError (which Home Assistant serves as a 500 the caller
    would mistake for a transient failure).
    """
    hass = FakeHass()
    manager, _ = _make_bot(hass, device_id="42")
    manager.api.images[5] = _image_record(5, device_id="99")
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError) as excinfo:
        _run(_call(hass, SERVICE_GET_VISION_IMAGE, {"config_entry_id": "entry-1", "image_id": 5}))
    err = excinfo.value
    assert isinstance(err, ServiceValidationError)  # -> HTTP 400
    assert err.translation_domain == DOMAIN
    assert err.translation_key == "vision_image_wrong_device"


def test_vision_response_service_passes_validation_error_through():
    """The response-service guard must not swallow or reclassify a
    ServiceValidationError -- it stays a 400 with its translated message.
    """
    async def handler(call):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="vision_image_wrong_device"
        )

    wrapped = _vision_response_service(handler)
    with pytest.raises(ServiceValidationError) as excinfo:
        _run(wrapped(ServiceCall(domain=DOMAIN, service=SERVICE_GET_VISION_IMAGE, data={})))
    assert excinfo.value.translation_key == "vision_image_wrong_device"


def test_vision_response_service_wraps_unexpected_error_as_structured_500():
    """An unexpected (non-HA) exception must not escape the response path as
    aiohttp's opaque 500 page; it becomes a translated HomeAssistantError so
    the caller sees a structured server error.
    """
    async def handler(call):
        raise ValueError("kaboom")

    wrapped = _vision_response_service(handler)
    with pytest.raises(HomeAssistantError) as excinfo:
        _run(wrapped(ServiceCall(domain=DOMAIN, service=SERVICE_GET_VISION_IMAGE, data={})))
    err = excinfo.value
    assert not isinstance(err, ServiceValidationError)  # server error, not validation
    assert err.translation_key == "vision_unexpected_error"


# --------------------------- apply_vision_radius ---------------------------

def _plant_record(plant_id, **overrides):
    base = {
        "id": plant_id, "device_id": "42", "pointer_type": "Plant",
        "discarded_at": None, "radius": 120.0,
    }
    base.update(overrides)
    return base


def test_apply_vision_radius_dry_run_validated():
    hass = FakeHass()
    manager, _ = _make_bot(hass)
    manager.api.points[7] = _plant_record(7)
    _async_register_services(hass)

    result = _run(_call(hass, SERVICE_APPLY_VISION_RADIUS, {
        "config_entry_id": "entry-1", "plant_id": 7, "measurement_id": str(uuid.uuid4()),
        "expected_current_radius_mm": 120.0, "recommended_radius_mm": 150.0, "confidence": 0.9,
        "apply": False,
    }))
    assert result["status"] == "validated"
    assert "async_patch_plant_radius" not in manager.api.calls


def test_apply_vision_radius_applies_when_enabled():
    hass = FakeHass()
    manager, _ = _make_bot(hass, options={"allow_automatic_radius_increases": True})
    manager.api.points[7] = _plant_record(7)
    _async_register_services(hass)

    result = _run(_call(hass, SERVICE_APPLY_VISION_RADIUS, {
        "config_entry_id": "entry-1", "plant_id": 7, "measurement_id": str(uuid.uuid4()),
        "expected_current_radius_mm": 120.0, "recommended_radius_mm": 150.0, "confidence": 0.9,
        "apply": True,
    }))
    assert result["status"] == "applied"
    assert result["old_radius_mm"] == 120.0
    assert result["new_radius_mm"] == 150.0
    assert manager.api.points[7]["radius"] == 150.0


def test_apply_vision_radius_rejected_when_automatic_writes_disabled():
    hass = FakeHass()
    manager, _ = _make_bot(hass)  # allow_automatic_radius_increases defaults False
    manager.api.points[7] = _plant_record(7)
    _async_register_services(hass)

    result = _run(_call(hass, SERVICE_APPLY_VISION_RADIUS, {
        "config_entry_id": "entry-1", "plant_id": 7, "measurement_id": str(uuid.uuid4()),
        "expected_current_radius_mm": 120.0, "recommended_radius_mm": 150.0, "confidence": 0.9,
        "apply": True,
    }))
    assert result["status"] == "rejected"
    assert manager.api.points[7]["radius"] == 120.0


def test_apply_vision_radius_rejects_low_confidence_when_applying():
    hass = FakeHass()
    manager, _ = _make_bot(hass, options={
        "allow_automatic_radius_increases": True,
        "minimum_automatic_confidence": 0.9,
    })
    manager.api.points[7] = _plant_record(7)
    _async_register_services(hass)

    result = _run(_call(hass, SERVICE_APPLY_VISION_RADIUS, {
        "config_entry_id": "entry-1", "plant_id": 7, "measurement_id": str(uuid.uuid4()),
        "expected_current_radius_mm": 120.0, "recommended_radius_mm": 150.0, "confidence": 0.5,
        "apply": True,
    }))
    assert result["status"] == "rejected"
    assert "confidence" in result["message"].lower()
    assert manager.api.points[7]["radius"] == 120.0


def test_apply_vision_radius_stale_radius_is_conflict():
    hass = FakeHass()
    manager, _ = _make_bot(hass, options={"allow_automatic_radius_increases": True})
    manager.api.points[7] = _plant_record(7, radius=200.0)
    _async_register_services(hass)

    result = _run(_call(hass, SERVICE_APPLY_VISION_RADIUS, {
        "config_entry_id": "entry-1", "plant_id": 7, "measurement_id": str(uuid.uuid4()),
        "expected_current_radius_mm": 120.0, "recommended_radius_mm": 150.0, "confidence": 0.9,
        "apply": True,
    }))
    assert result["status"] == "conflict"


def test_apply_vision_radius_rejects_shrink():
    hass = FakeHass()
    manager, _ = _make_bot(hass, options={"allow_automatic_radius_increases": True})
    manager.api.points[7] = _plant_record(7)
    _async_register_services(hass)

    result = _run(_call(hass, SERVICE_APPLY_VISION_RADIUS, {
        "config_entry_id": "entry-1", "plant_id": 7, "measurement_id": str(uuid.uuid4()),
        "expected_current_radius_mm": 120.0, "recommended_radius_mm": 90.0, "confidence": 0.9,
        "apply": True,
    }))
    assert result["status"] == "rejected"
    assert "shrink" in result["message"].lower()
    assert manager.api.points[7]["radius"] == 120.0


# --------------------------- upsert_vision_spread_curve ---------------------------

def test_upsert_curve_disabled_by_default():
    hass = FakeHass()
    _make_bot(hass)
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        _run(_call(hass, SERVICE_UPSERT_VISION_SPREAD_CURVE, {
            "config_entry_id": "entry-1", "crop_slug": "tomato",
            "name": "[FarmBot Vision] Tomato", "data": {"0": 10, "20": 40}, "apply": False,
        }))


def test_upsert_curve_rejects_monotonic_violation_when_enabled():
    hass = FakeHass()
    _make_bot(hass, options={"allow_vision_curve_writes": True})
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        _run(_call(hass, SERVICE_UPSERT_VISION_SPREAD_CURVE, {
            "config_entry_id": "entry-1", "crop_slug": "tomato",
            "name": "[FarmBot Vision] Tomato", "data": {"0": 40, "20": 10}, "apply": False,
        }))


def test_upsert_curve_rejects_modifying_user_created_curve():
    hass = FakeHass()
    manager, _ = _make_bot(hass, options={"allow_vision_curve_writes": True})
    manager.api.curves[5] = {"id": 5, "name": "My Tomatoes", "type": "spread", "data": {"0": 10}}
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        _run(_call(hass, SERVICE_UPSERT_VISION_SPREAD_CURVE, {
            "config_entry_id": "entry-1", "crop_slug": "tomato", "curve_id": 5,
            "name": "[FarmBot Vision] Tomato", "data": {"0": 10, "20": 40}, "apply": True,
        }))


def test_upsert_curve_rejects_assignment_to_wrong_bot_plant():
    hass = FakeHass()
    manager, _ = _make_bot(hass, device_id="42", options={"allow_vision_curve_writes": True})
    manager.api.points[9] = _plant_record(9, device_id="99")
    _async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        _run(_call(hass, SERVICE_UPSERT_VISION_SPREAD_CURVE, {
            "config_entry_id": "entry-1", "crop_slug": "tomato",
            "name": "[FarmBot Vision] Tomato", "data": {"0": 10, "20": 40},
            "assign_to_plant_ids": [9], "apply": False,
        }))


def test_upsert_curve_creates_and_assigns_when_valid():
    hass = FakeHass()
    manager, _ = _make_bot(hass, device_id="42", options={"allow_vision_curve_writes": True})
    manager.api.points[9] = _plant_record(9, spread_curve_id=None)
    _async_register_services(hass)

    result = _run(_call(hass, SERVICE_UPSERT_VISION_SPREAD_CURVE, {
        "config_entry_id": "entry-1", "crop_slug": "tomato",
        "name": "[FarmBot Vision] Tomato", "data": {"0": 10, "20": 40},
        "assign_to_plant_ids": [9], "apply": True,
    }))
    assert result["status"] == "applied"
    assert result["assignments"] == [{"plant_id": 9, "status": "assigned"}]
    assert manager.api.points[9]["spread_curve_id"] == result["curve_id"]


def test_upsert_curve_rolls_back_partial_assignment_failure():
    hass = FakeHass()
    manager, _ = _make_bot(hass, device_id="42", options={"allow_vision_curve_writes": True})
    manager.api.points[9] = _plant_record(9, spread_curve_id=1)
    manager.api.points[10] = _plant_record(10, spread_curve_id=2)
    manager.api.fail_assign_once_for = {10}
    _async_register_services(hass)

    with pytest.raises(Exception):
        _run(_call(hass, SERVICE_UPSERT_VISION_SPREAD_CURVE, {
            "config_entry_id": "entry-1", "crop_slug": "tomato",
            "name": "[FarmBot Vision] Tomato", "data": {"0": 10, "20": 40},
            "assign_to_plant_ids": [9, 10], "apply": True,
        }))
    assert manager.api.points[9]["spread_curve_id"] == 1


# --------------------------- report_vision_status ---------------------------

def test_report_vision_status_updates_manager_entities_state():
    hass = FakeHass()
    manager, _ = _make_bot(hass)
    _async_register_services(hass)

    _run(_call(hass, SERVICE_REPORT_VISION_STATUS, {
        "config_entry_id": "entry-1", "available": True, "status": "running",
        "job_id": "job-9", "plants_analysed": 3,
    }))

    assert manager.vision_status == "running"
    assert manager.vision_job_id == "job-9"
    assert manager.vision_plants_analysed == 3
    assert manager.vision_is_available() is True


# --------------------------- request_vision_analysis ---------------------------

def test_request_vision_analysis_fires_event():
    hass = FakeHass()
    _make_bot(hass)
    _async_register_services(hass)

    _run(_call(hass, SERVICE_REQUEST_VISION_ANALYSIS, {
        "config_entry_id": "entry-1", "plant_ids": [1, 2], "mode": "recommend",
    }))

    assert len(hass.bus.fired) == 1
    event_type, event_data = hass.bus.fired[0]
    assert event_type == EVENT_VISION_REQUEST
    assert event_data["device_id"] == "42"
    assert event_data["plant_ids"] == [1, 2]
    assert event_data["mode"] == "recommend"


# --------------------------- authentication failure handling ---------------------------

def test_get_vision_inventory_auth_failure_triggers_single_reauth():
    hass = FakeHass()
    manager, entry = _make_bot(hass)
    reauth_calls = []
    entry.async_start_reauth = lambda hass: reauth_calls.append(1)
    manager.api.auth_error_on = {"async_get_active_plants"}
    _async_register_services(hass)

    with pytest.raises(ServiceValidationError) as excinfo:
        _run(_call(hass, SERVICE_GET_VISION_INVENTORY, {"config_entry_id": "entry-1"}))
    assert "tok" not in str(excinfo.value)
    assert reauth_calls == [1]

    with pytest.raises(ServiceValidationError):
        _run(_call(hass, SERVICE_GET_VISION_INVENTORY, {"config_entry_id": "entry-1"}))
    assert reauth_calls == [1]  # not triggered a second time
