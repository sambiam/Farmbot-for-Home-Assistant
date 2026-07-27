"""A fake FarmbotApiClient double for testing the Vision service handlers."""
from custom_components.farmbot import vision
from custom_components.farmbot.api import FarmbotApiError, FarmbotAuthError


class FakeVisionApi:
    """Records calls and returns/raises pre-programmed results.

    Assigned onto ``manager.api`` in place of a real FarmbotApiClient so
    service-handler tests exercise real manager/vision.py logic without any
    network access.
    """

    def __init__(
        self, *, points=None, images=None, curves=None, calibration=None,
        firmware_config=None, reauth_callback=None
    ):
        self.points = {p["id"]: dict(p) for p in (points or [])}
        self.images = {i["id"]: dict(i) for i in (images or [])}
        self.curves = {c["id"]: dict(c) for c in (curves or [])}
        self.calibration = calibration if calibration is not None else {"available": False}
        self.firmware_config = firmware_config or {
            "movement_axis_nr_steps_x": 600000,
            "movement_axis_nr_steps_y": 300000,
            "movement_axis_nr_steps_z": 120000,
            "movement_step_per_mm_x": 100,
            "movement_step_per_mm_y": 100,
            "movement_step_per_mm_z": 100,
            "movement_home_up_z": 1,
        }
        self.calls: list[str] = []
        self.download_bytes = b""
        self.download_content_type = "image/jpeg"
        self.auth_error_on: set[str] = set()
        self.api_error_on: set[str] = set()
        self.fail_assign_once_for: set[int] = set()
        self.next_curve_id = 1000
        self.next_point_id = max(self.points, default=0) + 1
        # Mirrors FarmbotApiClient: a 401/403 invokes the reauth callback
        # before the exception propagates, so tests can assert reauth
        # dedup logic the same way they would against the real client.
        self.reauth_callback = reauth_callback

    def _record(self, name):
        self.calls.append(name)
        if name in self.auth_error_on:
            if self.reauth_callback is not None:
                self.reauth_callback()
            raise FarmbotAuthError(f"simulated auth failure in {name}")
        if name in self.api_error_on:
            raise FarmbotApiError(f"simulated API failure in {name}")

    async def async_get_active_plants(self):
        self._record("async_get_active_plants")
        return vision.filter_active_plants(list(self.points.values()))

    async def async_get_images(self):
        self._record("async_get_images")
        return list(self.images.values())

    async def async_get_points(self, *, pointer_type=None):
        self._record("async_get_points")
        points = list(self.points.values())
        return [
            point for point in points
            if pointer_type is None or point.get("pointer_type") == pointer_type
        ]

    async def async_get_firmware_config(self):
        self._record("async_get_firmware_config")
        return self.firmware_config

    async def async_get_curves(self):
        self._record("async_get_curves")
        return list(self.curves.values())

    async def async_get_camera_calibration(self):
        self._record("async_get_camera_calibration")
        return self.calibration

    async def async_get_image(self, image_id):
        self._record("async_get_image")
        return self.images.get(image_id)

    async def async_delete_image(self, image_id):
        self._record("async_delete_image")
        self.images.pop(image_id, None)
        return {}

    async def async_get_point(self, point_id):
        self._record("async_get_point")
        return self.points.get(point_id)

    async def async_download_image(self, attachment_url):
        self._record("async_download_image")
        return self.download_bytes, self.download_content_type

    async def async_get_curve(self, curve_id):
        self._record("async_get_curve")
        return self.curves.get(curve_id)

    async def async_patch_plant_radius(self, point_id, radius_mm):
        self._record("async_patch_plant_radius")
        if point_id in self.points:
            self.points[point_id]["radius"] = radius_mm
        return self.points.get(point_id, {})

    async def async_archive_plant(self, point_id):
        self._record("async_archive_plant")
        if point_id in self.points:
            self.points[point_id]["plant_stage"] = "removed"
        return self.points.get(point_id, {})

    async def async_patch_plant_center(self, point_id, x, y):
        self._record("async_patch_plant_center")
        if point_id in self.points:
            self.points[point_id].update({"x": x, "y": y})
        return self.points.get(point_id, {})

    async def async_patch_soil_height(self, point_id, z):
        self._record("async_patch_soil_height")
        if point_id in self.points:
            self.points[point_id]["z"] = z
        return self.points.get(point_id, {})

    async def async_patch_soil_point(self, point_id, *, x, y, z):
        self._record("async_patch_soil_point")
        if point_id in self.points:
            self.points[point_id].update({"x": x, "y": y, "z": z})
        return self.points.get(point_id, {})

    async def async_create_weed(self, *, name, x, y, z, radius):
        self._record("async_create_weed")
        point = {
            "id": self.next_point_id,
            "pointer_type": "Weed",
            "name": name,
            "x": x,
            "y": y,
            "z": z,
            "radius": radius,
        }
        self.next_point_id += 1
        self.points[point["id"]] = point
        return point

    async def async_patch_weed_radius(self, point_id, radius_mm):
        self._record("async_patch_weed_radius")
        if point_id in self.points:
            self.points[point_id]["radius"] = radius_mm
        return self.points.get(point_id, {})

    async def async_remove_weed(self, point_id):
        self._record("async_remove_weed")
        return self.points.pop(point_id, {})

    async def async_create_curve(self, *, name, type_, data):
        self._record("async_create_curve")
        curve_id = self.next_curve_id
        self.next_curve_id += 1
        curve = {"id": curve_id, "name": name, "type": type_, "data": data}
        self.curves[curve_id] = curve
        return curve

    async def async_patch_curve(self, curve_id, *, name=None, data=None):
        self._record("async_patch_curve")
        curve = self.curves.setdefault(curve_id, {"id": curve_id})
        if name is not None:
            curve["name"] = name
        if data is not None:
            curve["data"] = data
        return curve

    async def async_assign_curve_to_plant(self, point_id, curve_id):
        self.calls.append("async_assign_curve_to_plant")
        if point_id in self.fail_assign_once_for:
            self.fail_assign_once_for.discard(point_id)
            raise FarmbotApiError("simulated assignment failure")
        if point_id in self.points:
            self.points[point_id]["spread_curve_id"] = curve_id
        return self.points.get(point_id, {})
