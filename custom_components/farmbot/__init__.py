"""The FarmBot integration, including the FarmBot Vision bridge services."""
import base64
import functools
import logging
import uuid
from datetime import timedelta
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import image_utils, vision
from .api import FarmbotApiError, FarmbotAuthError
from .config_flow import FarmbotConfigFlow
from .const import (
    DEFAULT_IMAGE_LOOKBACK_HOURS,
    DEFAULT_IMAGE_MAX_HEIGHT,
    DEFAULT_IMAGE_MAX_WIDTH,
    DOMAIN,
    EVENT_VISION_REQUEST,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_LOOKBACK_HOURS,
    OPTION_ALLOW_AUTOMATIC_RADIUS_INCREASES,
    OPTION_ALLOW_VISION_CURVE_WRITES,
    OPTION_MAXIMUM_PLANT_RADIUS_MM,
    OPTION_MINIMUM_AUTOMATIC_CONFIDENCE,
    SERVICE_APPLY_VISION_RADIUS,
    SERVICE_EXECUTE_SEQUENCE,
    SERVICE_GET_VISION_IMAGE,
    SERVICE_GET_VISION_INVENTORY,
    SERVICE_LIST_VISION_BOTS,
    SERVICE_MOVE_TO,
    SERVICE_REPORT_VISION_STATUS,
    SERVICE_REQUEST_VISION_ANALYSIS,
    SERVICE_UPSERT_VISION_SPREAD_CURVE,
    TOKEN_REFRESH_INTERVAL,
    VISION_ANALYSIS_MODES,
    VISION_CURVE_TYPE,
    VISION_STATUS_VALUES,
)
from .manager import FarmbotManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "sensor", "button", "binary_sensor", "select"]

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

# --------------------------------------------------------------------------
# Service schemas - existing services (unchanged)
# --------------------------------------------------------------------------

SERVICE_EXECUTE_SEQUENCE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("sequence_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)

SERVICE_MOVE_TO_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("config_entry_id"): cv.string,
            vol.Optional("x"): vol.Coerce(float),
            vol.Optional("y"): vol.Coerce(float),
            vol.Optional("z"): vol.Coerce(float),
            vol.Optional("speed", default=100): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=100)
            ),
        }
    ),
    cv.has_at_least_one_key("x", "y", "z"),
)


# --------------------------------------------------------------------------
# Service schemas - FarmBot Vision bridge
# --------------------------------------------------------------------------

def _cv_uuid(value: Any) -> str:
    """Validate that `value` is a UUID string; return it normalised."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as err:
        raise vol.Invalid("must be a UUID") from err


def _cv_curve_data(value: Any) -> dict:
    """Validate the shape of a curve `data` mapping without judging its values.

    Deep validation (monotonicity, control-point count, diameter sanity) is
    the job of vision.validate_curve_data - this only rejects inputs that
    aren't even a plain day->int mapping.
    """
    if not isinstance(value, dict):
        raise vol.Invalid("data must be a mapping of day -> diameter_mm")
    result = {}
    for day, diameter in value.items():
        try:
            result[str(int(day))] = int(diameter)
        except (TypeError, ValueError) as err:
            raise vol.Invalid("data keys and values must be integers") from err
    return result


SERVICE_LIST_VISION_BOTS_SCHEMA = vol.Schema({})

SERVICE_GET_VISION_INVENTORY_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Optional("image_lookback_hours", default=DEFAULT_IMAGE_LOOKBACK_HOURS): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=MAX_IMAGE_LOOKBACK_HOURS)
        ),
        vol.Optional("include_all_curves", default=False): cv.boolean,
    }
)

SERVICE_GET_VISION_IMAGE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("image_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional("max_width", default=DEFAULT_IMAGE_MAX_WIDTH): vol.All(
            vol.Coerce(int), vol.Range(min=32, max=MAX_IMAGE_DIMENSION)
        ),
        vol.Optional("max_height", default=DEFAULT_IMAGE_MAX_HEIGHT): vol.All(
            vol.Coerce(int), vol.Range(min=32, max=MAX_IMAGE_DIMENSION)
        ),
    }
)

SERVICE_APPLY_VISION_RADIUS_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("plant_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required("measurement_id"): _cv_uuid,
        vol.Required("expected_current_radius_mm"): vol.Coerce(float),
        vol.Required("recommended_radius_mm"): vol.Coerce(float),
        vol.Required("confidence"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional("apply", default=False): cv.boolean,
    }
)

SERVICE_UPSERT_VISION_SPREAD_CURVE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("crop_slug"): cv.string,
        vol.Optional("curve_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required("name"): cv.string,
        vol.Required("data"): _cv_curve_data,
        vol.Optional("assign_to_plant_ids", default=list): [
            vol.All(vol.Coerce(int), vol.Range(min=1))
        ],
        vol.Optional("apply", default=False): cv.boolean,
    }
)

SERVICE_REPORT_VISION_STATUS_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("available"): cv.boolean,
        vol.Required("status"): vol.In(VISION_STATUS_VALUES),
        vol.Optional("job_id"): cv.string,
        vol.Optional("last_completed_at"): cv.string,
        vol.Optional("plants_analysed"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("recommendations"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("automatically_applied"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("uncertain"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("message"): cv.string,
        vol.Optional("app_version"): cv.string,
    }
)

SERVICE_REQUEST_VISION_ANALYSIS_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Optional("plant_ids", default=list): [vol.All(vol.Coerce(int), vol.Range(min=1))],
        vol.Optional("mode", default="recommend"): vol.In(VISION_ANALYSIS_MODES),
    }
)


def _get_manager(hass: HomeAssistant, config_entry_id: str) -> FarmbotManager:
    """Look up the FarmbotManager for a loaded config entry, or raise."""
    manager = hass.data.get(DOMAIN, {}).get(config_entry_id)
    if manager is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="config_entry_not_loaded",
            translation_placeholders={"config_entry_id": config_entry_id},
        )
    return manager


async def _safe_api_call(manager: FarmbotManager, coro, *, context: str):
    """Await a FarmbotApiClient call, converting failures to HA exceptions.

    Auth failures (401/403) have already triggered reauth exactly once
    inside FarmbotApiClient by the time this raises; the FarmBot response
    body is never included in the exception shown to the caller.
    """
    try:
        return await coro
    except FarmbotAuthError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="farmbot_auth_error"
        ) from err
    except FarmbotApiError as err:
        _LOGGER.error(
            "FarmBot Vision %s failed for bot %s: %s", context, manager.device_id, err
        )
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="farmbot_api_error"
        ) from err


_RADIUS_REJECTION_MESSAGES = {
    "plant_not_found": "Plant not found",
    "wrong_device": "Plant does not belong to this FarmBot",
    "not_a_plant": "Point is not a Plant",
    "plant_archived": "Plant is archived or removed",
    "invalid_expected_current_radius_mm": (
        "expected_current_radius_mm is not a valid positive number"
    ),
    "invalid_recommended_radius_mm": "recommended_radius_mm is not a valid positive number",
    "radius_exceeds_maximum": "Recommended radius exceeds the configured maximum",
    "current_radius_unknown": "FarmBot has no numeric radius recorded for this plant",
    "stale_radius": "Current FarmBot radius does not match expected_current_radius_mm",
    "shrink_not_allowed": "Automatic radius shrink is not supported",
}


def _async_register_services(hass: HomeAssistant) -> None:
    """Register FarmBot services once, shared across all config entries."""
    if hass.services.has_service(DOMAIN, SERVICE_EXECUTE_SEQUENCE):
        return

    # -------------------- existing services (unchanged behaviour) --------------------

    def execute_sequence(call: ServiceCall) -> None:
        manager = _get_manager(hass, call.data["config_entry_id"])
        manager.execute_sequence(call.data["sequence_id"])

    def move_to(call: ServiceCall) -> None:
        manager = _get_manager(hass, call.data["config_entry_id"])
        manager.move_to(
            x=call.data.get("x"),
            y=call.data.get("y"),
            z=call.data.get("z"),
            speed=call.data["speed"],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_EXECUTE_SEQUENCE,
        execute_sequence,
        schema=SERVICE_EXECUTE_SEQUENCE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_MOVE_TO, move_to, schema=SERVICE_MOVE_TO_SCHEMA
    )

    # -------------------- FarmBot Vision bridge services --------------------

    async def list_vision_bots(call: ServiceCall) -> dict:
        bots = [
            {
                "config_entry_id": entry_id,
                "device_id": manager.device_id,
                "name": manager.device_name,
            }
            for entry_id, manager in hass.data.get(DOMAIN, {}).items()
        ]
        return {"bots": bots}

    async def get_vision_inventory(call: ServiceCall) -> dict:
        manager = _get_manager(hass, call.data["config_entry_id"])
        lookback_hours = call.data["image_lookback_hours"]
        include_all_curves = call.data["include_all_curves"]

        plants = await _safe_api_call(
            manager, manager.api.async_get_active_plants(), context="fetch plants"
        )
        images = await _safe_api_call(
            manager, manager.api.async_get_images(), context="fetch images"
        )
        curves = await _safe_api_call(
            manager, manager.api.async_get_curves(), context="fetch curves"
        )
        raw_calibration = await _safe_api_call(
            manager, manager.api.async_get_camera_calibration(), context="fetch camera calibration"
        )
        calibration = vision.normalize_camera_calibration(raw_calibration)

        now = dt_util.utcnow()
        recent_images = vision.filter_recent_processed_images(
            images, now=now, lookback_hours=lookback_hours
        )

        return {
            "device_id": manager.device_id,
            "generated_at": now.isoformat(),
            "plants": [vision.project_plant(p) for p in plants],
            "images": [vision.project_image(i) for i in recent_images],
            "curves": vision.select_relevant_curves(
                plants, curves, include_all=include_all_curves
            ),
            "camera_calibration": calibration,
        }

    async def get_vision_image(call: ServiceCall) -> dict:
        manager = _get_manager(hass, call.data["config_entry_id"])
        image_id = call.data["image_id"]
        max_width = call.data["max_width"]
        max_height = call.data["max_height"]

        image = await _safe_api_call(
            manager, manager.api.async_get_image(image_id), context="fetch image metadata"
        )
        if image is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vision_image_not_found",
                translation_placeholders={"image_id": str(image_id)},
            )
        if str(image.get("device_id")) != str(manager.device_id):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="vision_image_wrong_device"
            )
        if not vision.is_image_ready(image):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="vision_image_not_processed"
            )

        attachment_url = image.get("attachment_url")
        if not attachment_url:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="vision_image_no_attachment"
            )

        raw_bytes, _content_type = await _safe_api_call(
            manager,
            manager.api.async_download_image(attachment_url),
            context="download image",
        )

        try:
            processed = await hass.async_add_executor_job(
                functools.partial(
                    image_utils.process_image,
                    raw_bytes,
                    max_width=max_width,
                    max_height=max_height,
                )
            )
        except image_utils.ImageDecodeError as err:
            _LOGGER.error("FarmBot Vision image %s failed to decode: %s", image_id, err)
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="vision_image_decode_failed"
            ) from err

        # Rescale FarmBot's native camera calibration onto the exact image we
        # are returning, so the companion app never has to guess which
        # coordinate system the calibration belongs to. Unavailable/ambiguous
        # calibration yields {"available": False, ...} rather than a guess.
        raw_calibration = await _safe_api_call(
            manager,
            manager.api.async_get_camera_calibration(),
            context="fetch camera calibration",
        )
        normalized_calibration = vision.normalize_camera_calibration(raw_calibration)
        processed_calibration = vision.compute_processed_calibration(
            normalized_calibration,
            oriented_width=processed.oriented_width,
            oriented_height=processed.oriented_height,
            processed_width=processed.width,
            processed_height=processed.height,
        )

        meta = image.get("meta") or {}
        _LOGGER.debug(
            "FarmBot Vision image %s processed %dx%d -> %dx%d "
            "(%d bytes JPEG; base64 and signed URL not logged)",
            image_id, processed.oriented_width, processed.oriented_height,
            processed.width, processed.height, len(processed.jpeg_bytes),
        )
        return {
            "image_id": image_id,
            "content_type": "image/jpeg",
            # sha256 is over the returned JPEG bytes; source_sha256 is over the
            # original download and never replaces it.
            "sha256": processed.sha256,
            "source_sha256": processed.source_sha256,
            "source_width": processed.source_width,
            "source_height": processed.source_height,
            "oriented_width": processed.oriented_width,
            "oriented_height": processed.oriented_height,
            "width": processed.width,
            "height": processed.height,
            "resize_scale_x": processed.resize_scale_x,
            "resize_scale_y": processed.resize_scale_y,
            "image_base64": base64.b64encode(processed.jpeg_bytes).decode("ascii"),
            "processed_calibration": processed_calibration,
            "meta": {
                "x": meta.get("x"),
                "y": meta.get("y"),
                "z": meta.get("z"),
                "created_at": image.get("created_at"),
            },
        }

    async def apply_vision_radius(call: ServiceCall) -> dict:
        manager = _get_manager(hass, call.data["config_entry_id"])
        options = manager.vision_options()
        plant_id = call.data["plant_id"]
        measurement_id = call.data["measurement_id"]
        expected = call.data["expected_current_radius_mm"]
        recommended = call.data["recommended_radius_mm"]
        confidence = call.data["confidence"]
        apply = call.data["apply"]

        _LOGGER.info(
            "FarmBot Vision radius proposal: bot=%s plant=%s measurement=%s "
            "expected_mm=%.1f recommended_mm=%.1f confidence=%.2f apply=%s",
            manager.device_id, plant_id, measurement_id, expected, recommended, confidence, apply,
        )

        point = await _safe_api_call(
            manager, manager.api.async_get_point(plant_id), context="fetch plant"
        )

        result = vision.validate_radius_change(
            point=point,
            device_id=manager.device_id,
            expected_current_radius_mm=expected,
            recommended_radius_mm=recommended,
            allow_automatic_shrink=False,  # shrinking is never implemented in this release
            maximum_plant_radius_mm=options[OPTION_MAXIMUM_PLANT_RADIUS_MM],
        )
        actual_radius = point.get("radius") if isinstance(point, dict) else None

        if not result.ok:
            status = "conflict" if result.reason == "stale_radius" else "rejected"
            message = _RADIUS_REJECTION_MESSAGES.get(result.reason, result.reason)
            _LOGGER.warning(
                "FarmBot Vision radius proposal for plant %s: %s (%s)",
                plant_id, status, message,
            )
            return {
                "status": status,
                "plant_id": plant_id,
                "measurement_id": measurement_id,
                "old_radius_mm": actual_radius,
                "new_radius_mm": recommended,
                "message": message,
            }

        if not apply:
            return {
                "status": "validated",
                "plant_id": plant_id,
                "measurement_id": measurement_id,
                "old_radius_mm": actual_radius,
                "new_radius_mm": recommended,
                "message": "Validated; dry run, no write performed",
            }

        if not options[OPTION_ALLOW_AUTOMATIC_RADIUS_INCREASES]:
            return {
                "status": "rejected",
                "plant_id": plant_id,
                "measurement_id": measurement_id,
                "old_radius_mm": actual_radius,
                "new_radius_mm": recommended,
                "message": "Automatic radius application is disabled in integration options",
            }

        if confidence < options[OPTION_MINIMUM_AUTOMATIC_CONFIDENCE]:
            return {
                "status": "rejected",
                "plant_id": plant_id,
                "measurement_id": measurement_id,
                "old_radius_mm": actual_radius,
                "new_radius_mm": recommended,
                "message": "Confidence is below the configured minimum for automatic changes",
            }

        await _safe_api_call(
            manager,
            manager.api.async_patch_plant_radius(plant_id, recommended),
            context="update plant radius",
        )
        updated_point = await _safe_api_call(
            manager, manager.api.async_get_point(plant_id), context="re-fetch plant"
        )
        new_radius = (
            updated_point.get("radius") if isinstance(updated_point, dict) else recommended
        )
        _LOGGER.info(
            "FarmBot Vision applied radius change for plant %s: %s mm -> %s mm",
            plant_id, actual_radius, new_radius,
        )
        return {
            "status": "applied",
            "plant_id": plant_id,
            "measurement_id": measurement_id,
            "old_radius_mm": actual_radius,
            "new_radius_mm": new_radius,
            "message": "Radius updated",
        }

    async def upsert_vision_spread_curve(call: ServiceCall) -> dict:
        manager = _get_manager(hass, call.data["config_entry_id"])
        options = manager.vision_options()
        if not options[OPTION_ALLOW_VISION_CURVE_WRITES]:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="vision_curve_writes_disabled"
            )

        crop_slug = call.data["crop_slug"]
        curve_id = call.data.get("curve_id")
        name = call.data["name"]
        data = call.data["data"]
        assign_to_plant_ids = call.data["assign_to_plant_ids"]
        apply = call.data["apply"]

        existing_curve = None
        if curve_id is not None:
            existing_curve = await _safe_api_call(
                manager, manager.api.async_get_curve(curve_id), context="fetch curve"
            )

        curve_result = vision.validate_curve_upsert(
            curve_id=curve_id, name=name, data=data, existing_curve=existing_curve
        )
        if not curve_result.ok:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="vision_curve_invalid",
                translation_placeholders={"reason": curve_result.reason},
            )

        assignment_targets: list[tuple[int, dict]] = []
        for plant_id in assign_to_plant_ids:
            plant = await _safe_api_call(
                manager,
                manager.api.async_get_point(plant_id),
                context="fetch plant for assignment",
            )
            plant_result = vision.validate_plant_assignment(plant, device_id=manager.device_id)
            if not plant_result.ok:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="vision_curve_assignment_invalid",
                    translation_placeholders={
                        "plant_id": str(plant_id), "reason": plant_result.reason
                    },
                )
            assignment_targets.append((plant_id, plant))

        if not apply:
            return {
                "status": "validated",
                "curve_id": curve_id,
                "crop_slug": crop_slug,
                "name": name,
                "assignments": [
                    {"plant_id": pid, "status": "validated"} for pid, _ in assignment_targets
                ],
                "message": "Validated; dry run, no write performed",
            }

        if curve_id is None:
            curve = await _safe_api_call(
                manager,
                manager.api.async_create_curve(name=name, type_=VISION_CURVE_TYPE, data=data),
                context="create curve",
            )
        else:
            curve = await _safe_api_call(
                manager,
                manager.api.async_patch_curve(curve_id, name=name, data=data),
                context="update curve",
            )
        new_curve_id = curve.get("id", curve_id)

        assignments = []
        applied: list[tuple[int, Any]] = []
        try:
            for plant_id, plant in assignment_targets:
                previous_curve_id = plant.get("spread_curve_id")
                await _safe_api_call(
                    manager,
                    manager.api.async_assign_curve_to_plant(plant_id, new_curve_id),
                    context="assign curve to plant",
                )
                applied.append((plant_id, previous_curve_id))
                assignments.append({"plant_id": plant_id, "status": "assigned"})
        except HomeAssistantError:
            rollback_failed = []
            for plant_id, previous_curve_id in applied:
                try:
                    await manager.api.async_assign_curve_to_plant(plant_id, previous_curve_id)
                except FarmbotApiError:
                    rollback_failed.append(plant_id)
            _LOGGER.error(
                "FarmBot Vision curve assignment failed partway through for bot %s; "
                "rolled back %d/%d plant(s) (rollback failed for %s)",
                manager.device_id, len(applied) - len(rollback_failed), len(applied),
                rollback_failed,
            )
            raise

        return {
            "status": "applied",
            "curve_id": new_curve_id,
            "crop_slug": crop_slug,
            "name": name,
            "assignments": assignments,
            "message": "Curve created" if curve_id is None else "Curve updated",
        }

    async def report_vision_status(call: ServiceCall) -> None:
        manager = _get_manager(hass, call.data["config_entry_id"])
        manager.update_vision_status(
            available=call.data["available"],
            status=call.data["status"],
            job_id=call.data.get("job_id"),
            last_completed_at=call.data.get("last_completed_at"),
            plants_analysed=call.data.get("plants_analysed"),
            recommendations=call.data.get("recommendations"),
            automatically_applied=call.data.get("automatically_applied"),
            uncertain=call.data.get("uncertain"),
            message=call.data.get("message"),
            app_version=call.data.get("app_version"),
        )

    async def request_vision_analysis(call: ServiceCall) -> None:
        manager = _get_manager(hass, call.data["config_entry_id"])
        hass.bus.async_fire(
            EVENT_VISION_REQUEST,
            {
                "config_entry_id": call.data["config_entry_id"],
                "device_id": manager.device_id,
                "plant_ids": call.data["plant_ids"],
                "mode": call.data["mode"],
            },
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_VISION_BOTS,
        list_vision_bots,
        schema=SERVICE_LIST_VISION_BOTS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_VISION_INVENTORY,
        get_vision_inventory,
        schema=SERVICE_GET_VISION_INVENTORY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_VISION_IMAGE,
        get_vision_image,
        schema=SERVICE_GET_VISION_IMAGE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_VISION_RADIUS,
        apply_vision_radius,
        schema=SERVICE_APPLY_VISION_RADIUS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPSERT_VISION_SPREAD_CURVE,
        upsert_vision_spread_curve,
        schema=SERVICE_UPSERT_VISION_SPREAD_CURVE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REPORT_VISION_STATUS,
        report_vision_status,
        schema=SERVICE_REPORT_VISION_STATUS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REQUEST_VISION_ANALYSIS,
        request_vision_analysis,
        schema=SERVICE_REQUEST_VISION_ANALYSIS_SCHEMA,
    )


def _async_remove_services_if_last_entry(hass: HomeAssistant) -> None:
    """Remove FarmBot services once no config entries remain loaded."""
    if hass.data.get(DOMAIN):
        return
    for service in (
        SERVICE_EXECUTE_SEQUENCE,
        SERVICE_MOVE_TO,
        SERVICE_LIST_VISION_BOTS,
        SERVICE_GET_VISION_INVENTORY,
        SERVICE_GET_VISION_IMAGE,
        SERVICE_APPLY_VISION_RADIUS,
        SERVICE_UPSERT_VISION_SPREAD_CURVE,
        SERVICE_REPORT_VISION_STATUS,
        SERVICE_REQUEST_VISION_ANALYSIS,
    ):
        hass.services.async_remove(DOMAIN, service)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FarmBot from a config entry."""
    token     = entry.data["token"]
    device_id = entry.data["device_id"]
    mqtt_host = entry.data["mqtt_host"]

    manager = FarmbotManager(hass, token, device_id, mqtt_host, entry=entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager

    # Check and refresh token immediately on startup
    _LOGGER.info("Checking token expiry on startup")
    await manager.async_check_and_refresh_token()

    # Connect to MQTT without blocking the event loop
    await manager.connect_mqtt()

    # Schedule periodic token refresh check
    async def _periodic_token_check(now):
        """Periodic callback to check and refresh token."""
        _LOGGER.debug("Periodic token refresh check")
        await manager.async_check_and_refresh_token()

    refresh_interval = timedelta(seconds=TOKEN_REFRESH_INTERVAL)
    entry.async_on_unload(
        async_track_time_interval(hass, _periodic_token_check, refresh_interval)
    )
    _LOGGER.info("Token refresh scheduler started (interval: %s)", refresh_interval)

    _async_register_services(hass)

    # Forward each platform to its respective setup file
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old FarmBot config entry to the current version.

    Version 1 entries (created before unique IDs were introduced) may be
    missing ``unique_id``. Assign ``str(device_id)`` as the unique ID so
    duplicate-FarmBot detection and reauth identity checks work for them.
    """
    if entry.version > FarmbotConfigFlow.VERSION:
        _LOGGER.error(
            "FarmBot config entry %s has version %s, newer than supported "
            "version %s; refusing to migrate",
            entry.entry_id, entry.version, FarmbotConfigFlow.VERSION,
        )
        return False

    if entry.version == 1:
        if entry.unique_id is None:
            device_id = entry.data.get("device_id")
            if device_id is None:
                _LOGGER.error(
                    "Cannot migrate FarmBot config entry %s: no unique_id and no "
                    "device_id to identify the FarmBot",
                    entry.entry_id,
                )
                return False

            new_unique_id = str(device_id)
            for other in hass.config_entries.async_entries(DOMAIN):
                if other.entry_id != entry.entry_id and other.unique_id == new_unique_id:
                    _LOGGER.error(
                        "Cannot migrate FarmBot config entry %s: bot id %s is already "
                        "claimed by config entry %s",
                        entry.entry_id, new_unique_id, other.entry_id,
                    )
                    return False

            hass.config_entries.async_update_entry(
                entry, unique_id=new_unique_id, version=2
            )
            _LOGGER.info(
                "Migrated FarmBot config entry %s to version 2 (assigned unique_id)",
                entry.entry_id,
            )
        else:
            hass.config_entries.async_update_entry(entry, version=2)
            _LOGGER.info(
                "Migrated FarmBot config entry %s to version 2", entry.entry_id
            )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload all platforms first so they can be re-setup on reload
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        _LOGGER.warning("Failed to unload one or more FarmBot platforms")
        return False

    manager = hass.data[DOMAIN].pop(entry.entry_id, None)
    if manager:
        await manager.disconnect_mqtt()
        await manager.async_close()

    _async_remove_services_if_last_entry(hass)
    return True
