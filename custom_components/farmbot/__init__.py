"""The FarmBot integration, including the FarmBot Vision bridge services."""
import asyncio
import base64
import functools
import logging
import math
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
    MAX_SOIL_BASELINE_MM,
    MAX_SOIL_Z_OFFSET_MM,
    MIN_SOIL_BASELINE_MM,
    SERVICE_APPLY_VISION_PLANT_CENTER,
    SERVICE_APPLY_VISION_RADIUS,
    SERVICE_APPLY_VISION_REMOVAL,
    SERVICE_APPLY_VISION_SOIL_HEIGHT,
    SERVICE_CREATE_VISION_WEED,
    SERVICE_EXECUTE_SEQUENCE,
    SERVICE_GET_VISION_IMAGE,
    SERVICE_GET_VISION_INVENTORY,
    SERVICE_GET_VISION_SOIL_CAPTURE,
    SERVICE_GET_VISION_SOIL_POINTS,
    SERVICE_LIST_VISION_BOTS,
    SERVICE_MOVE_TO,
    SERVICE_REMOVE_VISION_WEED,
    SERVICE_REPORT_VISION_STATUS,
    SERVICE_REQUEST_VISION_ANALYSIS,
    SERVICE_START_VISION_SOIL_CAPTURE,
    SERVICE_UPDATE_VISION_WEED_RADIUS,
    SERVICE_UPSERT_VISION_SPREAD_CURVE,
    TOKEN_REFRESH_INTERVAL,
    VISION_ANALYSIS_MODES,
    VISION_CURVE_TYPE,
    VISION_IMAGE_POLL_INTERVAL_SECONDS,
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

SERVICE_GET_VISION_SOIL_POINTS_SCHEMA = vol.Schema(
    {vol.Required("config_entry_id"): cv.string}
)

SERVICE_START_VISION_SOIL_CAPTURE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("point_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional("capture_z", default=0): vol.Coerce(float),
        vol.Optional("baseline_mm", default=15): vol.All(
            vol.Coerce(float),
            vol.Range(min=MIN_SOIL_BASELINE_MM, max=MAX_SOIL_BASELINE_MM),
        ),
        vol.Optional("z_offsets_mm", default=lambda: [0.0]): vol.All(
            [vol.All(vol.Coerce(float), vol.Range(min=0, max=MAX_SOIL_Z_OFFSET_MM))],
            vol.Length(min=1, max=3),
        ),
    }
)

SERVICE_GET_VISION_SOIL_CAPTURE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("capture_id"): _cv_uuid,
    }
)

SERVICE_APPLY_VISION_SOIL_HEIGHT_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("point_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required("measurement_id"): _cv_uuid,
        vol.Required("expected_x"): vol.Coerce(float),
        vol.Required("expected_y"): vol.Coerce(float),
        vol.Required("expected_z"): vol.Coerce(float),
        vol.Required("recommended_z_mm"): vol.Coerce(float),
        vol.Required("confidence"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional("apply", default=False): cv.boolean,
        vol.Optional("human_approved", default=False): cv.boolean,
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
        vol.Optional("human_approved", default=False): cv.boolean,
    }
)

SERVICE_APPLY_VISION_REMOVAL_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("plant_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required("measurement_id"): _cv_uuid,
        vol.Required("expected_current_radius_mm"): vol.Coerce(float),
        vol.Required("confidence"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional("apply", default=False): cv.boolean,
        vol.Optional("human_approved", default=False): cv.boolean,
    }
)

SERVICE_APPLY_VISION_PLANT_CENTER_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("plant_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required("measurement_id"): _cv_uuid,
        vol.Required("expected_x"): vol.Coerce(float),
        vol.Required("expected_y"): vol.Coerce(float),
        vol.Required("recommended_x"): vol.Coerce(float),
        vol.Required("recommended_y"): vol.Coerce(float),
        vol.Optional("apply", default=False): cv.boolean,
        vol.Optional("human_approved", default=False): cv.boolean,
    }
)

SERVICE_CREATE_VISION_WEED_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("detection_id"): _cv_uuid,
        vol.Required("x"): vol.Coerce(float),
        vol.Required("y"): vol.Coerce(float),
        vol.Optional("z", default=0): vol.Coerce(float),
        vol.Required("radius"): vol.All(vol.Coerce(float), vol.Range(min=1, max=250)),
        vol.Optional("name", default="Vision detected weed"): cv.string,
        vol.Required("confidence"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional("apply", default=False): cv.boolean,
        vol.Optional("human_approved", default=False): cv.boolean,
    }
)

SERVICE_UPDATE_VISION_WEED_RADIUS_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("weed_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required("expected_current_radius_mm"): vol.Coerce(float),
        vol.Required("recommended_radius_mm"): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=250)
        ),
        vol.Required("confidence"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional("apply", default=False): cv.boolean,
        vol.Optional("human_approved", default=False): cv.boolean,
    }
)

SERVICE_REMOVE_VISION_WEED_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("weed_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Required("confidence"): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Optional("apply", default=False): cv.boolean,
        vol.Optional("human_approved", default=False): cv.boolean,
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
        vol.Optional("human_approved", default=False): cv.boolean,
    }
)

SERVICE_REPORT_VISION_STATUS_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("available"): cv.boolean,
        vol.Required("status"): vol.In(VISION_STATUS_VALUES),
        # job_id and last_completed_at are nullable per the farmbot-vision-v2
        # contract: the companion app sends job_id=null on every idle
        # heartbeat and last_completed_at=null while a job is running. cv.string
        # rejects None, so accepting a bare string here would 400 every real
        # report (including the very first one) and leave the Vision entities
        # stuck at their defaults forever. Accept None explicitly.
        vol.Optional("job_id"): vol.Any(None, cv.string),
        vol.Optional("last_completed_at"): vol.Any(None, cv.string),
        vol.Optional("plants_analysed"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("recommendations"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("automatically_applied"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("uncertain"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("message"): vol.All(cv.string, vol.Length(max=240)),
        vol.Optional("app_version"): vol.Any(None, cv.string),
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


def _vision_response_service(handler):
    """Guard a response-returning Vision service so its failures stay structured.

    Home Assistant maps an escaping ``HomeAssistantError`` (and its
    ``ServiceValidationError`` subclass) to a JSON error carrying this
    integration's translated message -- a 400 for a permanent validation
    rejection, a 500 for a transient server error. Any *other* exception,
    however, escapes the response path and is served as aiohttp's opaque
    "Server got itself in trouble" 500 page, which a caller cannot tell apart
    from a transient failure. Convert those stragglers into a translated
    ``HomeAssistantError`` so every failure of a response service surfaces as a
    structured status the FarmBot Vision app can act on, while validation
    rejections keep flowing through untouched as their proper 400.
    """
    @functools.wraps(handler)
    async def wrapper(call: ServiceCall):
        try:
            return await handler(call)
        except HomeAssistantError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception(
                "Unexpected error in FarmBot Vision service %s", handler.__name__
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="vision_unexpected_error",
            ) from err

    return wrapper


_RADIUS_REJECTION_MESSAGES = {
    "plant_not_found": "Plant not found",
    "wrong_device": "Plant does not belong to this FarmBot",
    "not_a_plant": "Point is not a Plant",
    "plant_archived": "Plant is archived or removed",
    "invalid_expected_current_radius_mm": (
        "expected_current_radius_mm is not a valid positive number"
    ),
    "invalid_recommended_radius_mm": "recommended_radius_mm is not a valid positive number",
    "current_radius_unknown": "FarmBot has no numeric radius recorded for this plant",
    "stale_radius": "Current FarmBot radius does not match expected_current_radius_mm",
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
        # Resolve ownership from the same identity get_vision_inventory selects
        # by: the FarmBot device behind this config entry. The image's REST
        # ``device_id`` is a bare number while ``manager.device_id`` is the JWT
        # ``device_<id>`` username form, so compare them via the shared,
        # form-agnostic helper rather than as raw strings -- otherwise every
        # legitimately-owned image is falsely rejected.
        if not vision.same_device(image.get("device_id"), manager.device_id):
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
        response = {
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
        return response

    async def get_vision_soil_points(call: ServiceCall) -> dict:
        """Return recognized soil points and conservative motion state."""
        manager = _get_manager(hass, call.data["config_entry_id"])
        points = await _safe_api_call(
            manager, manager.api.async_get_points(), context="fetch soil-height points"
        )
        firmware = await _safe_api_call(
            manager,
            manager.api.async_get_firmware_config(),
            context="fetch motion configuration",
        )
        eligible = []
        for point in points:
            if not manager.is_soil_height_point(point):
                continue
            if point.get("device_id") is not None and not vision.same_device(
                point.get("device_id"), manager.device_id
            ):
                continue
            try:
                coordinates = [float(point[axis]) for axis in ("x", "y", "z")]
                if not all(math.isfinite(value) for value in coordinates):
                    continue
                eligible.append(
                    {
                        "id": int(point["id"]),
                        "name": str(point.get("name") or "Soil Height"),
                        "x": coordinates[0],
                        "y": coordinates[1],
                        "z": coordinates[2],
                        "updated_at": point.get("updated_at"),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return {
            "device_id": manager.device_id,
            "generated_at": dt_util.utcnow().isoformat(),
            "points": sorted(eligible, key=lambda item: (item["x"], item["y"], item["id"])),
            "motion": manager.soil_motion_state(firmware),
        }

    async def start_vision_soil_capture(call: ServiceCall) -> dict:
        manager = _get_manager(hass, call.data["config_entry_id"])
        point = await _safe_api_call(
            manager,
            manager.api.async_get_point(call.data["point_id"]),
            context="fetch soil point for capture",
        )
        if not manager.is_soil_height_point(point):
            return {"status": "rejected", "message": "Eligible soil-height point not found"}
        if point.get("device_id") is not None and not vision.same_device(
            point.get("device_id"), manager.device_id
        ):
            return {"status": "rejected", "message": "Soil point belongs to another FarmBot"}
        firmware = await _safe_api_call(
            manager,
            manager.api.async_get_firmware_config(),
            context="fetch motion configuration",
        )
        values = [
            call.data["capture_z"],
            call.data["baseline_mm"],
            *call.data["z_offsets_mm"],
            point.get("x"),
            point.get("y"),
            point.get("z"),
        ]
        try:
            finite = all(math.isfinite(float(value)) for value in values)
        except (TypeError, ValueError):
            finite = False
        if not finite:
            return {"status": "rejected", "message": "Capture values must be finite"}
        z_offsets = [float(value) for value in call.data["z_offsets_mm"]]
        if z_offsets != sorted(set(z_offsets)):
            return {
                "status": "rejected",
                "message": "Z offsets must be unique and in ascending order",
            }
        try:
            capture_id = manager.start_soil_capture(
                point=point,
                firmware_config=firmware,
                capture_z=float(call.data["capture_z"]),
                baseline_mm=float(call.data["baseline_mm"]),
                z_offsets_mm=z_offsets,
            )
        except ValueError as err:
            return {"status": "rejected", "message": str(err)[:240]}
        return {
            "status": "queued",
            "capture_id": capture_id,
            "message": "Soil capture queued",
        }

    async def get_vision_soil_capture(call: ServiceCall) -> dict:
        manager = _get_manager(hass, call.data["config_entry_id"])
        capture = manager.soil_capture(call.data["capture_id"])
        if capture is None:
            return {"status": "failed", "message": "Soil capture was not found", "frames": []}
        return capture

    async def apply_vision_soil_height(call: ServiceCall) -> dict:
        """Patch only Z after identity, ownership, staleness and approval checks."""
        manager = _get_manager(hass, call.data["config_entry_id"])
        point = await _safe_api_call(
            manager,
            manager.api.async_get_point(call.data["point_id"]),
            context="fetch soil point for height update",
        )
        if not manager.is_soil_height_point(point):
            return {"status": "rejected", "message": "Eligible soil-height point not found"}
        if point.get("device_id") is not None and not vision.same_device(
            point.get("device_id"), manager.device_id
        ):
            return {"status": "rejected", "message": "Soil point belongs to another FarmBot"}
        requested = [
            call.data["expected_x"],
            call.data["expected_y"],
            call.data["expected_z"],
            call.data["recommended_z_mm"],
        ]
        if not all(math.isfinite(float(value)) for value in requested):
            return {"status": "rejected", "message": "Soil coordinates must be finite"}
        try:
            actual_coordinates = {
                axis: float(point[axis]) for axis in ("x", "y", "z")
            }
        except (KeyError, TypeError, ValueError):
            return {"status": "rejected", "message": "FarmBot soil coordinates are invalid"}
        if not all(math.isfinite(value) for value in actual_coordinates.values()):
            return {"status": "rejected", "message": "FarmBot soil coordinates are invalid"}
        if any(
            abs(actual_coordinates[axis] - float(call.data[f"expected_{axis}"])) > 0.5
            for axis in ("x", "y", "z")
        ):
            return {"status": "conflict", "message": "Soil point coordinates changed"}
        firmware = await _safe_api_call(
            manager,
            manager.api.async_get_firmware_config(),
            context="fetch motion configuration",
        )
        z_bounds = manager.soil_motion_state(firmware)["axis_bounds"]["z"]
        recommended = float(call.data["recommended_z_mm"])
        if z_bounds is None or not z_bounds[0] <= recommended <= z_bounds[1]:
            return {"status": "rejected", "message": "Recommended soil Z is outside FarmBot bounds"}
        if not call.data["apply"]:
            return {"status": "validated", "message": "Validated; no write performed"}
        if not call.data["human_approved"]:
            return {"status": "rejected", "message": "Human approval is required"}
        await _safe_api_call(
            manager,
            manager.api.async_patch_soil_height(call.data["point_id"], recommended),
            context="update soil height",
        )
        updated = await _safe_api_call(
            manager,
            manager.api.async_get_point(call.data["point_id"]),
            context="verify soil height update",
        )
        if not manager.is_soil_height_point(updated):
            return {"status": "conflict", "message": "FarmBot soil point changed during update"}
        try:
            actual = float(updated["z"])
        except (KeyError, TypeError, ValueError):
            return {"status": "conflict", "message": "FarmBot returned an invalid soil height"}
        if not math.isfinite(actual) or abs(actual - recommended) > 0.5:
            return {"status": "conflict", "message": "FarmBot did not persist the soil height"}
        return {
            "status": "applied",
            "point_id": call.data["point_id"],
            "old_z_mm": float(point["z"]),
            "z_mm": actual,
            "message": "Soil height updated",
        }

    async def apply_vision_radius(call: ServiceCall) -> dict:
        manager = _get_manager(hass, call.data["config_entry_id"])
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

        await _safe_api_call(
            manager,
            manager.api.async_patch_plant_radius(plant_id, recommended),
            context="update plant radius",
        )
        updated_point = None
        new_radius = None
        for attempt in range(3):
            updated_point = await _safe_api_call(
                manager, manager.api.async_get_point(plant_id), context="verify plant radius"
            )
            new_radius = (
                updated_point.get("radius") if isinstance(updated_point, dict) else None
            )
            if isinstance(new_radius, (int, float)) and abs(new_radius - recommended) <= 0.5:
                break
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
        if not isinstance(new_radius, (int, float)) or abs(new_radius - recommended) > 0.5:
            _LOGGER.error(
                "FarmBot accepted the radius PATCH for plant %s but verification returned %r",
                plant_id,
                new_radius,
            )
            return {
                "status": "conflict",
                "plant_id": plant_id,
                "measurement_id": measurement_id,
                "old_radius_mm": actual_radius,
                "new_radius_mm": new_radius,
                "message": "FarmBot did not persist the requested radius",
            }
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

    async def apply_vision_removal(call: ServiceCall) -> dict:
        """Validate then reversibly archive a vision-confirmed missing plant."""
        manager = _get_manager(hass, call.data["config_entry_id"])
        plant_id = call.data["plant_id"]
        measurement_id = call.data["measurement_id"]
        expected = call.data["expected_current_radius_mm"]
        apply = call.data["apply"]

        point = await _safe_api_call(
            manager, manager.api.async_get_point(plant_id), context="fetch plant"
        )
        result = vision.validate_removal(
            point=point,
            device_id=manager.device_id,
            expected_current_radius_mm=expected,
        )
        actual_radius = point.get("radius") if isinstance(point, dict) else None
        if not result.ok:
            status = "conflict" if result.reason == "stale_radius" else "rejected"
            message = _RADIUS_REJECTION_MESSAGES.get(result.reason, result.reason)
            return {
                "status": status,
                "plant_id": plant_id,
                "measurement_id": measurement_id,
                "old_radius_mm": actual_radius,
                "message": message,
            }

        if not apply:
            return {
                "status": "validated",
                "plant_id": plant_id,
                "measurement_id": measurement_id,
                "old_radius_mm": actual_radius,
                "message": "Validated; dry run, no write performed",
            }

        await _safe_api_call(
            manager, manager.api.async_archive_plant(plant_id), context="archive plant"
        )
        _LOGGER.info("FarmBot Vision archived plant %s after removal confirmation", plant_id)
        return {
            "status": "applied",
            "plant_id": plant_id,
            "measurement_id": measurement_id,
            "old_radius_mm": actual_radius,
            "message": "Plant marked removed",
        }

    async def apply_vision_plant_center(call: ServiceCall) -> dict:
        """Move a plant only after verifying its coordinates have not changed."""
        manager = _get_manager(hass, call.data["config_entry_id"])
        point = await _safe_api_call(
            manager,
            manager.api.async_get_point(call.data["plant_id"]),
            context="fetch plant for centre update",
        )
        if not isinstance(point, dict) or point.get("pointer_type") != "Plant":
            return {"status": "rejected", "message": "Plant was not found"}
        if (
            abs(float(point.get("x", 0)) - call.data["expected_x"]) > 0.5
            or abs(float(point.get("y", 0)) - call.data["expected_y"]) > 0.5
        ):
            return {"status": "conflict", "message": "Plant coordinates changed"}
        if not call.data["apply"]:
            return {"status": "validated", "message": "Validated; no write performed"}
        if not call.data["human_approved"]:
            return {"status": "rejected", "message": "Plant centre moves require human approval"}
        updated = await _safe_api_call(
            manager,
            manager.api.async_patch_plant_center(
                call.data["plant_id"],
                call.data["recommended_x"],
                call.data["recommended_y"],
            ),
            context="move plant centre",
        )
        return {
            "status": "applied",
            "plant_id": call.data["plant_id"],
            "measurement_id": call.data["measurement_id"],
            "x": updated.get("x", call.data["recommended_x"]),
            "y": updated.get("y", call.data["recommended_y"]),
            "message": "Plant centre moved",
        }

    async def create_vision_weed(call: ServiceCall) -> dict:
        """Create a FarmBot Weed point from a calibrated vision detection."""
        manager = _get_manager(hass, call.data["config_entry_id"])
        if not call.data["apply"]:
            return {"status": "validated", "message": "Validated; no write performed"}
        created = await _safe_api_call(
            manager,
            manager.api.async_create_weed(
                name=call.data["name"],
                x=call.data["x"],
                y=call.data["y"],
                z=call.data["z"],
                radius=call.data["radius"],
            ),
            context="create vision weed",
        )
        return {
            "status": "applied",
            "weed_id": created.get("id"),
            "detection_id": call.data["detection_id"],
            "message": "Weed created",
        }

    async def update_vision_weed_radius(call: ServiceCall) -> dict:
        """Increase a known Weed point radius after an identity-safe lookup."""
        manager = _get_manager(hass, call.data["config_entry_id"])
        point = await _safe_api_call(
            manager,
            manager.api.async_get_point(call.data["weed_id"]),
            context="fetch weed for radius update",
        )
        if not isinstance(point, dict) or point.get("pointer_type") != "Weed":
            return {"status": "rejected", "message": "Weed was not found"}
        actual = float(point.get("radius", 0))
        if abs(actual - call.data["expected_current_radius_mm"]) > 0.5:
            return {"status": "conflict", "message": "Weed radius changed"}
        recommended = max(actual, float(call.data["recommended_radius_mm"]))
        if not call.data["apply"]:
            return {"status": "validated", "message": "Validated; no write performed"}
        updated = await _safe_api_call(
            manager,
            manager.api.async_patch_weed_radius(call.data["weed_id"], recommended),
            context="update weed radius",
        )
        return {
            "status": "applied",
            "weed_id": call.data["weed_id"],
            "old_radius_mm": actual,
            "radius_mm": updated.get("radius", recommended),
            "message": "Weed radius updated",
        }

    async def remove_vision_weed(call: ServiceCall) -> dict:
        """Remove a known Weed point after the app confirms disappearance."""
        manager = _get_manager(hass, call.data["config_entry_id"])
        point = await _safe_api_call(
            manager,
            manager.api.async_get_point(call.data["weed_id"]),
            context="fetch weed for removal",
        )
        if not isinstance(point, dict) or point.get("pointer_type") != "Weed":
            return {"status": "rejected", "message": "Weed was not found"}
        if not call.data["apply"]:
            return {"status": "validated", "message": "Validated; no write performed"}
        await _safe_api_call(
            manager,
            manager.api.async_remove_weed(call.data["weed_id"]),
            context="remove weed",
        )
        return {
            "status": "applied",
            "weed_id": call.data["weed_id"],
            "message": "Weed marked removed",
        }

    async def upsert_vision_spread_curve(call: ServiceCall) -> dict:
        manager = _get_manager(hass, call.data["config_entry_id"])
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
        verified_curve = await _safe_api_call(
            manager,
            manager.api.async_get_curve(new_curve_id),
            context="verify curve data",
        )
        expected_curve_data = {str(day): int(value) for day, value in data.items()}
        actual_curve_data = (
            {
                str(day): int(value)
                for day, value in (verified_curve.get("data") or {}).items()
            }
            if isinstance(verified_curve, dict)
            else {}
        )
        if actual_curve_data != expected_curve_data:
            raise HomeAssistantError("FarmBot did not persist the requested spread curve data")

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
                verified_plant = await _safe_api_call(
                    manager,
                    manager.api.async_get_point(plant_id),
                    context="verify curve assignment",
                )
                if (
                    not isinstance(verified_plant, dict)
                    or verified_plant.get("spread_curve_id") != new_curve_id
                ):
                    raise HomeAssistantError(
                        f"FarmBot did not persist curve assignment for plant {plant_id}"
                    )
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
        _vision_response_service(get_vision_image),
        schema=SERVICE_GET_VISION_IMAGE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_VISION_SOIL_POINTS,
        _vision_response_service(get_vision_soil_points),
        schema=SERVICE_GET_VISION_SOIL_POINTS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_VISION_SOIL_CAPTURE,
        _vision_response_service(start_vision_soil_capture),
        schema=SERVICE_START_VISION_SOIL_CAPTURE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_VISION_SOIL_CAPTURE,
        _vision_response_service(get_vision_soil_capture),
        schema=SERVICE_GET_VISION_SOIL_CAPTURE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_VISION_SOIL_HEIGHT,
        _vision_response_service(apply_vision_soil_height),
        schema=SERVICE_APPLY_VISION_SOIL_HEIGHT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
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
        SERVICE_APPLY_VISION_REMOVAL,
        apply_vision_removal,
        schema=SERVICE_APPLY_VISION_REMOVAL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_VISION_PLANT_CENTER,
        apply_vision_plant_center,
        schema=SERVICE_APPLY_VISION_PLANT_CENTER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_VISION_WEED,
        create_vision_weed,
        schema=SERVICE_CREATE_VISION_WEED_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_VISION_WEED_RADIUS,
        update_vision_weed_radius,
        schema=SERVICE_UPDATE_VISION_WEED_RADIUS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REMOVE_VISION_WEED,
        remove_vision_weed,
        schema=SERVICE_REMOVE_VISION_WEED_SCHEMA,
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
        SERVICE_GET_VISION_SOIL_POINTS,
        SERVICE_START_VISION_SOIL_CAPTURE,
        SERVICE_GET_VISION_SOIL_CAPTURE,
        SERVICE_APPLY_VISION_SOIL_HEIGHT,
        SERVICE_APPLY_VISION_RADIUS,
        SERVICE_APPLY_VISION_REMOVAL,
        SERVICE_APPLY_VISION_PLANT_CENTER,
        SERVICE_CREATE_VISION_WEED,
        SERVICE_UPDATE_VISION_WEED_RADIUS,
        SERVICE_REMOVE_VISION_WEED,
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

    # Establish the processed-image baseline, then turn each newly completed
    # FarmBot photo into a targeted companion-app request.
    await manager.async_poll_new_vision_images()

    async def _poll_new_vision_images(now):
        await manager.async_poll_new_vision_images()

    image_poll_interval = timedelta(seconds=VISION_IMAGE_POLL_INTERVAL_SECONDS)
    entry.async_on_unload(
        async_track_time_interval(hass, _poll_new_vision_images, image_poll_interval)
    )
    _LOGGER.info("FarmBot Vision image monitor started (interval: %s)", image_poll_interval)

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
