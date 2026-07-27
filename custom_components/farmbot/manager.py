import asyncio
import json
import logging
import math
import ssl
import time
import uuid
from datetime import timedelta
from typing import Any, Optional, Tuple

import paho.mqtt.client as mqtt
import requests
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from . import vision
from .api import FarmbotApiClient, FarmbotApiError
from .const import (
    API_BASE_URL,
    DEFAULT_VISION_ENABLED,
    DEFAULT_VISION_HEARTBEAT_TIMEOUT_MINUTES,
    EVENT_VISION_REQUEST,
    GRID_REPAIR_COORDINATE_TOLERANCE_MM,
    GRID_REPAIR_IMAGE_TIMEOUT_SECONDS,
    GRID_REPAIR_MAX_PHOTO_ATTEMPTS,
    MQTT_PORT,
    OPTION_VISION_ENABLED,
    OPTION_VISION_HEARTBEAT_TIMEOUT_MINUTES,
    SIGNAL_SEQUENCE_SELECTED,
    SIGNAL_STATE,
    SIGNAL_VISION_STATE,
    SOIL_CAPTURE_SETTLE_MILLISECONDS,
    SOIL_IMAGE_TIMEOUT_SECONDS,
    SOIL_RPC_TIMEOUT_SECONDS,
    TOKEN_REFRESH_WINDOW,
    TOPIC_COMMAND,
    TOPIC_FROM_DEVICE,
    TOPIC_LOGS,
    TOPIC_STATUS,
)
from .jwt_util import decode_jwt_payload

_LOGGER = logging.getLogger(__name__)


def _mask(s: str, keep_start: int = 4, keep_end: int = 4) -> str:
    if not s:
        return ""
    if len(s) <= keep_start + keep_end:
        return "*" * len(s)
    return f"{s[:keep_start]}…{s[-keep_end:]}"


def _normalize_username(device_id: str) -> str:
    """Ensure username is in 'device_<id>' format required by FarmBot."""
    device_id = str(device_id).strip()
    if not device_id:
        return ""
    return device_id if device_id.startswith("device_") else f"device_{device_id}"


def _split_host_port(raw_host: str, default_port: int) -> Tuple[str, int]:
    """Strip schemes like mqtts:// or amqps:// and split out ':port' if present."""
    host = (raw_host or "").strip()
    for scheme in (
        "mqtts://",
        "mqtt://",
        "amqps://",
        "amqp://",
        "ssl://",
        "tcp://",
        "wss://",
        "ws://",
    ):
        if host.lower().startswith(scheme):
            host = host[len(scheme) :]
            break
    port = default_port
    if ":" in host:
        h, p = host.rsplit(":", 1)
        if p.isdigit():
            host, port = h, int(p)
    return host, port


class FarmbotManager:
    """Central manager for FarmBot integration over MQTT."""

    def __init__(self, hass, token: str, device_id: str, mqtt_host: str, entry=None):
        self.hass = hass
        self.token = str(token).strip()  # encoded JWT
        self.device_id = str(device_id).strip()  # 'device_<id>' or numeric
        self.mqtt_host_raw = str(mqtt_host).strip()  # must come from token.unencoded.mqtt
        self.status: dict = {}
        self.device_name = f"FarmBot {self.device_id}"
        self.entry_id: Optional[str] = (
            getattr(entry, "entry_id", None) if entry is not None else None
        )
        self._mqtt: Optional[mqtt.Client] = None
        self._mqtt_connected = False
        self._entry = entry  # ConfigEntry reference for updates and reauth
        self._auth_failed = False  # Track auth failure to prevent spam
        self._last_bad_auth_log_time = 0  # Rate-limit bad-auth logging
        self.selected_sequence: Optional[dict] = None  # {'id': int, 'name': str} or None
        # Do not connect here; async_setup_entry will await connect_mqtt()

        # -------------------- FarmBot Vision bridge runtime state --------------------
        self.api = FarmbotApiClient(
            hass, self.token, self.device_id, reauth_callback=self._trigger_reauth_from_async
        )
        self.vision_last_heartbeat: Optional[Any] = None
        self.vision_app_version: Optional[str] = None
        self.vision_app_reported_available: Optional[bool] = None
        self.vision_status: str = "unavailable"
        self.vision_job_id: Optional[str] = None
        self.vision_message: Optional[str] = None
        self.vision_last_completed_at: Optional[Any] = None
        self.vision_plants_analysed: int = 0
        self.vision_recommendations: int = 0
        self.vision_automatically_applied: int = 0
        self.vision_uncertain: int = 0
        self._last_vision_report_snapshot: Optional[tuple] = None
        self._known_ready_vision_image_ids: Optional[set[int]] = None
        self._vision_image_monitor_started_at = dt_util.utcnow()
        self._pending_rpcs: dict[str, asyncio.Future] = {}
        self._soil_capture_lock = asyncio.Lock()
        self.soil_captures: dict[str, dict[str, Any]] = {}
        self._soil_capture_tasks: set[asyncio.Task] = set()
        self._claimed_soil_image_ids: set[int] = set()
        self.grid_repairs: dict[str, dict[str, Any]] = {}
        self._grid_repair_tasks: set[asyncio.Task] = set()

    # -------------------- Token Refresh --------------------
    def _should_refresh_token(self) -> bool:
        """Check if token needs refresh based on expiry."""
        payload = decode_jwt_payload(self.token)
        if not payload:
            _LOGGER.warning("Unable to decode token payload for expiry check")
            return False

        exp = payload.get("exp")
        if not exp:
            _LOGGER.warning("Token missing 'exp' field")
            return False

        now = int(time.time())
        time_until_expiry = exp - now

        if time_until_expiry <= 0:
            _LOGGER.warning("Token has expired (exp=%s, now=%s)", exp, now)
            return True

        if time_until_expiry < TOKEN_REFRESH_WINDOW:
            _LOGGER.info(
                "Token expires in %s seconds (< %s window), will refresh",
                time_until_expiry,
                TOKEN_REFRESH_WINDOW,
            )
            return True

        _LOGGER.debug("Token still valid for %s seconds", time_until_expiry)
        return False

    async def async_refresh_token(self) -> bool:
        """Refresh the JWT token by calling FarmBot API. Returns True on success."""
        _LOGGER.info("Attempting to refresh FarmBot token")

        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            url = f"{API_BASE_URL}/tokens"
            headers = {"Authorization": f"Bearer {self.token}"}

            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token_obj = data.get("token", {})

                    new_encoded = token_obj.get("encoded")
                    new_unencoded = token_obj.get("unencoded", {})

                    if not new_encoded or not new_unencoded:
                        _LOGGER.error("Token refresh response missing fields: %s", token_obj)
                        return False

                    # Extract new credentials
                    new_device_id = new_unencoded.get("bot", self.device_id)
                    new_mqtt_host = new_unencoded.get("mqtt", self.mqtt_host_raw)

                    # Update config entry
                    if self._entry:
                        new_data = {
                            **self._entry.data,
                            "token": new_encoded,
                            "device_id": new_device_id,
                            "mqtt_host": new_mqtt_host,
                        }
                        self.hass.config_entries.async_update_entry(self._entry, data=new_data)
                        _LOGGER.info("Config entry updated with new token")

                    # Update in-memory credentials
                    self.token = new_encoded
                    self.device_id = new_device_id
                    self.mqtt_host_raw = new_mqtt_host
                    self.api.update_token(new_encoded)

                    # Reconnect MQTT with new credentials
                    _LOGGER.info("Reconnecting MQTT with refreshed token")
                    await self.disconnect_mqtt()
                    await self.connect_mqtt()

                    # Reset auth failure flag on success
                    self._auth_failed = False

                    return True

                elif resp.status in (401, 403):
                    _LOGGER.error(
                        "Token refresh failed with auth error %s - triggering reauth",
                        resp.status,
                    )
                    return False
                else:
                    body = await resp.text()
                    _LOGGER.error("Token refresh failed [%s]: %s", resp.status, body)
                    return False

        except Exception as e:
            _LOGGER.exception("Exception during token refresh: %s", e)
            return False

    async def async_check_and_refresh_token(self) -> bool:
        """Check token expiry and refresh if needed. Returns True if token is valid."""
        if not self._should_refresh_token():
            return True  # Token still valid, no refresh needed

        success = await self.async_refresh_token()

        if not success and self._entry and not self._auth_failed:
            # Trigger reauth flow
            _LOGGER.warning("Token refresh failed, triggering reauth flow")
            self._auth_failed = True
            self._entry.async_start_reauth(self.hass)

        return success

    # -------------------- Connection (run in executor) --------------------
    def _connect_mqtt_blocking(self):
        """(Blocking) Initialize MQTT client with proper TLS and credentials."""
        username = _normalize_username(self.device_id)
        host, port = _split_host_port(self.mqtt_host_raw, MQTT_PORT)

        client_id = f"ha-{username}-{uuid.uuid4().hex[:8]}"
        self._mqtt = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )

        # TLS is required by FarmBot’s broker; load system CAs (blocking)
        # Use modern TLS; do not disable verification.
        self._mqtt.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self._mqtt.tls_insecure_set(False)

        # Auth: username = 'device_<id>', password = encoded token
        self._mqtt.username_pw_set(username=username, password=self.token)

        # Helpful backoff on reconnects
        self._mqtt.reconnect_delay_set(min_delay=1, max_delay=30)

        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message

        _LOGGER.info(
            "MQTT: connecting host=%s port=%s user=%s token=%s",
            host,
            port,
            username,
            _mask(self.token, 8, 8),
        )
        try:
            self._mqtt.connect(host, port)
        except Exception:
            _LOGGER.exception("MQTT connect() raised")
            raise

        self._mqtt.loop_start()
        _LOGGER.debug("MQTT loop started for %s", username)

    async def connect_mqtt(self):
        await self.hass.async_add_executor_job(self._connect_mqtt_blocking)

    def _disconnect_mqtt_blocking(self):
        if getattr(self, "_mqtt", None):
            _LOGGER.debug("Stopping MQTT loop")
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
            self._mqtt_connected = False
            _LOGGER.info("MQTT disconnected for %s", self.device_id)

    async def disconnect_mqtt(self):
        await self.hass.async_add_executor_job(self._disconnect_mqtt_blocking)

    # -------------------- MQTT callbacks --------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        # reason_code is a paho ReasonCode: compare by name/int, not by the old
        # MQTTv3.1.1 rc values (e.g. "bad auth" is numeric 4 pre-migration,
        # 134 as a ReasonCode) - see paho.mqtt.reasoncodes for the mapping.
        if reason_code == 0:
            self._mqtt_connected = True
            client.subscribe(TOPIC_STATUS.format(device_id=self.device_id))
            client.subscribe(TOPIC_FROM_DEVICE.format(device_id=self.device_id))
            client.subscribe(TOPIC_LOGS.format(device_id=self.device_id))
            _LOGGER.info("MQTT connected and subscribed for %s", self.device_id)
            # Reset auth failure flag on successful connection
            self._auth_failed = False
        elif reason_code == "Bad user name or password":
            self._mqtt_connected = False
            # Rate limit logging
            now = time.time()
            if now - self._last_bad_auth_log_time > 60:  # Log at most once per minute
                _LOGGER.error("MQTT connect failed: %s - token may be expired", reason_code)
                self._last_bad_auth_log_time = now

            # Trigger reauth if not already done
            if self._entry and not self._auth_failed:
                _LOGGER.warning("MQTT authentication failed, triggering reauth flow")
                self._auth_failed = True
                # Schedule reauth trigger in event loop
                self.hass.loop.call_soon_threadsafe(self._entry.async_start_reauth, self.hass)
        else:
            self._mqtt_connected = False
            _LOGGER.error(
                "MQTT connect failed: %s (reason code %s)", reason_code, reason_code.value
            )

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            _LOGGER.exception("Failed to parse MQTT payload on %s", msg.topic)
            return

        if msg.topic == TOPIC_STATUS.format(device_id=self.device_id):
            state = payload.get("body", payload) or {}
            self.status = state
            # Paho callback thread -> HA loop:
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send, self.hass, SIGNAL_STATE, self.status
            )
        elif msg.topic == TOPIC_FROM_DEVICE.format(device_id=self.device_id):
            self.hass.loop.call_soon_threadsafe(self._resolve_rpc_response, payload)
        else:
            _LOGGER.debug("Unhandled topic %s", msg.topic)

    # -------------------- Command helpers --------------------
    def _publish_rpc(self, rpc: dict):
        assert self._mqtt is not None, "MQTT client not connected"
        topic = TOPIC_COMMAND.format(device_id=self.device_id)
        _LOGGER.debug("Publishing RPC to %s: %s", topic, rpc)
        self._mqtt.publish(topic, json.dumps(rpc))

    def send_rpc_request(
        self, commands: list, priority: int = 600, label: str | None = None
    ) -> str:
        if label is None:
            label = f"ha-{uuid.uuid4()}"
        rpc = {
            "kind": "rpc_request",
            "args": {"label": label, "priority": priority},
            "body": commands,
        }
        self._publish_rpc(rpc)
        return label

    def _resolve_rpc_response(self, payload: dict[str, Any]) -> None:
        """Resolve an acknowledged RPC on the HA event-loop thread."""
        if payload.get("kind") not in {"rpc_ok", "rpc_error"}:
            return
        label = str((payload.get("args") or {}).get("label") or "")
        future = self._pending_rpcs.pop(label, None)
        if future is None or future.done():
            return
        if payload.get("kind") == "rpc_ok":
            future.set_result(payload)
            return
        explanations = [
            str(item.get("args", {}).get("message", ""))
            for item in payload.get("body", [])
            if isinstance(item, dict) and item.get("kind") == "explanation"
        ]
        message = "; ".join(filter(None, explanations))[:240] or "FarmBot rejected the command"
        future.set_exception(RuntimeError(message))

    async def async_rpc_request(
        self,
        commands: list[dict[str, Any]],
        *,
        timeout: float = SOIL_RPC_TIMEOUT_SECONDS,
        priority: int = 600,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Publish a CeleryScript request and await its matching acknowledgement."""
        if self._mqtt is None or not self._mqtt_connected:
            raise RuntimeError("FarmBot MQTT is not connected")
        label = label or f"ha-{uuid.uuid4()}"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_rpcs[label] = future
        try:
            self.send_rpc_request(commands, priority=priority, label=label)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_rpcs.pop(label, None)

    def send_write_pin(self, pin: int, value: int):
        cs = [
            {
                "kind": "write_pin",
                "args": {"pin_number": int(pin), "pin_value": int(value), "pin_mode": 0},
            }
        ]
        self.send_rpc_request(cs)

    def send_toggle_pin(self, pin: int):
        cs = [{"kind": "toggle_pin", "args": {"pin_number": int(pin)}}]
        self.send_rpc_request(cs)

    def fetch_sequences(self) -> list[dict]:
        """Return a list of sequences [{'id': int, 'name': str}, ...] from FarmBot API."""
        url = f"{API_BASE_URL}/sequences"
        headers = {"Authorization": f"Bearer {self.token}"}
        resp = requests.get(url, headers=headers, timeout=10)

        if resp.status_code in (401, 403):
            _LOGGER.warning(
                "FarmBot API returned %s - token may be expired, triggering reauth",
                resp.status_code,
            )
            if self._entry and not self._auth_failed:
                self._auth_failed = True
                # This may be called from an executor thread, so schedule in event loop
                self.hass.loop.call_soon_threadsafe(self._entry.async_start_reauth, self.hass)
            return []

        resp.raise_for_status()
        data = resp.json() or []
        items = []
        for s in data:
            sid = s.get("id")
            name = s.get("name") or s.get("label") or f"Sequence {sid}"
            if sid is not None:
                items.append({"id": int(sid), "name": str(name)})
        return items

    def execute_sequence(self, sequence_id: int):
        cs = [{"kind": "execute", "args": {"sequence_id": int(sequence_id)}}]
        self.send_rpc_request(cs)

    def set_selected_sequence(self, seq: Optional[dict]) -> None:
        """Record which sequence is currently selected and notify listeners.

        Lets the "launch selected sequence" button stay in sync with the
        sequence select entity without the two entities referencing each
        other directly.
        """
        self.selected_sequence = seq
        async_dispatcher_send(self.hass, SIGNAL_SEQUENCE_SELECTED, seq)

    def move_to(self, x=None, y=None, z=None, speed=100):
        args = {}
        if x is not None:
            args["x"] = float(x)
        if y is not None:
            args["y"] = float(y)
        if z is not None:
            args["z"] = float(z)
        args["speed"] = int(speed)
        cs = [{"kind": "move", "args": args}]
        self.send_rpc_request(cs)

    def get_pin_value(self, pin: int):
        pins = (self.status or {}).get("pins") or {}
        if isinstance(pins, dict):
            item = pins.get(str(pin)) or pins.get(int(pin))
            if isinstance(item, dict):
                return item.get("value")
            return item
        if isinstance(pins, list):
            for p in pins:
                if str(p.get("number")) == str(pin):
                    return p.get("value")
        return None

    # -------------------- Soil-height capture --------------------

    @staticmethod
    def is_soil_height_point(point: object) -> bool:
        """Recognize FarmBot soil points by metadata, never by display name alone."""
        if not isinstance(point, dict) or point.get("pointer_type") != "GenericPointer":
            return False
        if point.get("discarded_at"):
            return False
        meta = point.get("meta")
        if not isinstance(meta, dict):
            return False
        at_soil = meta.get("at_soil_level")
        return (
            meta.get("created_by") == "measure-soil-height"
            or at_soil is True
            or (isinstance(at_soil, str) and at_soil.lower() == "true")
        )

    @staticmethod
    def _axis_length(config: dict[str, Any], axis: str) -> float | None:
        steps = config.get(f"movement_axis_nr_steps_{axis}")
        per_mm = config.get(f"movement_step_per_mm_{axis}")
        try:
            steps_f, per_mm_f = float(steps), float(per_mm)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(steps_f) or not math.isfinite(per_mm_f) or per_mm_f <= 0:
            return None
        value = steps_f / per_mm_f
        return value if value > 0 else None

    def soil_motion_state(self, firmware_config: dict[str, Any]) -> dict[str, Any]:
        info = (self.status or {}).get("informational_settings") or {}
        position = (self.status or {}).get("location_data", {}).get("position") or {}
        home_up = firmware_config.get(
            "movement_home_up_z",
            (self.status or {}).get("mcu_params", {}).get("movement_home_up_z"),
        )
        z_direction = -1 if home_up in (1, True, "1") else 1
        lengths = {axis: self._axis_length(firmware_config, axis) for axis in ("x", "y", "z")}
        if lengths["z"] is not None and z_direction < 0:
            z_bounds = [-lengths["z"], 0.0]
        elif lengths["z"] is not None:
            z_bounds = [0.0, lengths["z"]]
        else:
            z_bounds = None
        return {
            "connected": self._mqtt is not None and self._mqtt_connected,
            "busy": bool(info.get("busy", False)) or self._soil_capture_lock.locked(),
            "locked": bool(info.get("locked", False)),
            "position": {axis: position.get(axis) for axis in ("x", "y", "z")},
            "z_direction": z_direction,
            "axis_bounds": {
                "x": [0.0, lengths["x"]] if lengths["x"] is not None else None,
                "y": [0.0, lengths["y"]] if lengths["y"] is not None else None,
                "z": z_bounds,
            },
        }

    @staticmethod
    def _soil_lateral_offsets(y: float, baseline: float, y_max: float) -> list[float]:
        """Choose a centered triplet, falling back to an in-bounds one-sided set."""
        centered = [-baseline, 0.0, baseline]
        if 0 <= y - baseline and y + baseline <= y_max:
            return centered
        if y + 2 * baseline <= y_max:
            return [0.0, baseline, 2 * baseline]
        if y - 2 * baseline >= 0:
            return [-2 * baseline, -baseline, 0.0]
        raise ValueError("not enough Y-axis travel for a stereo triplet")

    @staticmethod
    def _soil_capture_commands(
        *,
        x: float,
        y: float,
        capture_z: float,
        lateral_offsets: list[float],
        z_offsets: list[float],
        z_direction: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, float]]]:
        commands: list[dict[str, Any]] = []
        frames: list[dict[str, float]] = []
        for z_offset in z_offsets:
            z = capture_z + z_direction * z_offset
            for lateral in lateral_offsets:
                frame_y = y + lateral
                commands.extend(
                    [
                        {
                            "kind": "move",
                            "args": {
                                "x": x,
                                "y": frame_y,
                                "z": z,
                                "speed": 100,
                                "safe_z": True,
                            },
                        },
                        {
                            "kind": "wait",
                            "args": {"milliseconds": SOIL_CAPTURE_SETTLE_MILLISECONDS},
                        },
                        {"kind": "take_photo", "args": {}},
                    ]
                )
                frames.append(
                    {
                        "x": x,
                        "y": frame_y,
                        "z": z,
                        "lateral_offset_mm": lateral,
                        "z_offset_mm": z_offset,
                    }
                )
        return commands, frames

    def start_soil_capture(
        self,
        *,
        point: dict[str, Any],
        firmware_config: dict[str, Any],
        capture_z: float,
        baseline_mm: float,
        z_offsets_mm: list[float],
    ) -> str:
        """Create a bounded asynchronous capture session and return its ID."""
        state = self.soil_motion_state(firmware_config)
        if not state["connected"]:
            raise ValueError("FarmBot is not connected")
        if state["locked"]:
            raise ValueError("FarmBot is emergency-stopped")
        if (
            state["busy"]
            or any(not task.done() for task in self._soil_capture_tasks)
            or any(not task.done() for task in self._grid_repair_tasks)
        ):
            raise ValueError("FarmBot is busy")
        bounds = state["axis_bounds"]
        if any(bounds[axis] is None for axis in ("x", "y", "z")):
            raise ValueError("FarmBot axis bounds are unavailable")
        x, y = float(point["x"]), float(point["y"])
        if not bounds["x"][0] <= x <= bounds["x"][1]:
            raise ValueError("soil point X is outside FarmBot bounds")
        laterals = self._soil_lateral_offsets(y, baseline_mm, bounds["y"][1])
        z_direction = int(state["z_direction"])
        for offset in z_offsets_mm:
            z = capture_z + z_direction * offset
            if not bounds["z"][0] <= z <= bounds["z"][1]:
                raise ValueError("soil capture Z is outside FarmBot bounds")
        capture_id = str(uuid.uuid4())
        self.soil_captures[capture_id] = {
            "capture_id": capture_id,
            "status": "queued",
            "message": "Capture queued",
            "frames": [],
            "created_at": dt_util.utcnow().isoformat(),
            "expected_frames": [],
        }
        task = asyncio.create_task(
            self._run_soil_capture(
                capture_id=capture_id,
                point=point,
                capture_z=capture_z,
                lateral_offsets=laterals,
                z_offsets=z_offsets_mm,
                z_direction=z_direction,
                original_position=state["position"],
            ),
            name=f"farmbot-soil-{capture_id}",
        )
        self._soil_capture_tasks.add(task)
        task.add_done_callback(self._soil_capture_tasks.discard)
        return capture_id

    async def _run_soil_capture(
        self,
        *,
        capture_id: str,
        point: dict[str, Any],
        capture_z: float,
        lateral_offsets: list[float],
        z_offsets: list[float],
        z_direction: int,
        original_position: dict[str, Any],
    ) -> None:
        record = self.soil_captures[capture_id]
        async with self._soil_capture_lock:
            final_status = "failed"
            final_message = "Soil capture failed"
            try:
                before = {
                    int(item["id"])
                    for item in await self.api.async_get_images()
                    if isinstance(item, dict) and item.get("id") is not None
                }
                record["before_image_ids"] = sorted(before)
                started_at = dt_util.utcnow()
                commands, expected = self._soil_capture_commands(
                    x=float(point["x"]),
                    y=float(point["y"]),
                    capture_z=capture_z,
                    lateral_offsets=lateral_offsets,
                    z_offsets=z_offsets,
                    z_direction=z_direction,
                )
                record.update(
                    status="running",
                    message="FarmBot is moving and taking soil images",
                    expected_frames=expected,
                    started_at=started_at.isoformat(),
                )
                await self.async_rpc_request(commands)
                record.update(
                    status="waiting_images",
                    message="Waiting for FarmBot image processing",
                )
                record["frames"] = await self._wait_for_soil_images(
                    before=before,
                    expected=expected,
                    started_at=started_at,
                )
                final_status = "complete"
                final_message = f"Captured {len(record['frames'])} soil images"
                record.update(message="Restoring the FarmBot starting position")
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Soil capture %s failed: %s", capture_id, err)
                final_message = str(err)[:240] or "Soil capture failed"
                record.update(message="Capture failed; restoring the FarmBot position")
            finally:
                if all(original_position.get(axis) is not None for axis in ("x", "y", "z")):
                    try:
                        await self.async_rpc_request(
                            [
                                {
                                    "kind": "move",
                                    "args": {
                                        **{
                                            axis: float(original_position[axis])
                                            for axis in ("x", "y", "z")
                                        },
                                        "speed": 100,
                                        "safe_z": True,
                                    },
                                }
                            ],
                            timeout=60,
                        )
                    except Exception as err:  # pylint: disable=broad-except
                        _LOGGER.warning(
                            "Could not restore FarmBot position after soil capture %s: %s",
                            capture_id,
                            err,
                        )
                record.update(
                    status=final_status,
                    message=final_message,
                    completed_at=dt_util.utcnow().isoformat(),
                )

    @staticmethod
    def _image_meta(image: dict[str, Any]) -> dict[str, Any]:
        meta = image.get("meta")
        return meta if isinstance(meta, dict) else image

    @classmethod
    def _match_soil_frames(
        cls,
        images: list[dict[str, Any]],
        expected: list[dict[str, float]],
    ) -> list[dict[str, Any]]:
        remaining = list(images)
        matched: list[dict[str, Any]] = []
        for target in expected:
            candidates = []
            for image in remaining:
                meta = cls._image_meta(image)
                try:
                    distance = math.sqrt(
                        sum(
                            (float(meta[axis]) - float(target[axis])) ** 2
                            for axis in ("x", "y", "z")
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if distance <= 2.5:
                    candidates.append((distance, image))
            if not candidates:
                continue
            _, image = min(candidates, key=lambda item: item[0])
            remaining.remove(image)
            matched.append(
                {
                    "image_id": int(image["id"]),
                    **target,
                }
            )
        return matched

    async def _wait_for_soil_images(
        self,
        *,
        before: set[int],
        expected: list[dict[str, float]],
        started_at,
    ) -> list[dict[str, Any]]:
        deadline = asyncio.get_running_loop().time() + SOIL_IMAGE_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            images = await self.api.async_get_images()
            candidates = []
            for image in images:
                if not isinstance(image, dict) or not vision.is_image_ready(image):
                    continue
                try:
                    image_id = int(image["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if image_id in before or not vision.same_device(
                    image.get("device_id"), self.device_id
                ):
                    continue
                created = dt_util.parse_datetime(str(image.get("created_at") or ""))
                if created is not None:
                    try:
                        if created < started_at:
                            continue
                    except TypeError:
                        continue
                candidates.append(image)
            matched = self._match_soil_frames(candidates, expected)
            self._claimed_soil_image_ids.update(frame["image_id"] for frame in matched)
            if len(matched) == len(expected):
                return matched
            await asyncio.sleep(2)
        raise TimeoutError("FarmBot images did not finish processing in time")

    def soil_capture(self, capture_id: str) -> dict[str, Any] | None:
        record = self.soil_captures.get(capture_id)
        if record is None:
            return None
        return {
            key: value
            for key, value in record.items()
            if key not in {"expected_frames", "before_image_ids"}
        }

    def start_grid_repair(
        self, *, targets: list[dict[str, float]], firmware_config: dict[str, Any]
    ) -> str:
        """Start a safe, bounded photo-grid repair and return its session ID."""
        state = self.soil_motion_state(firmware_config)
        if not state["connected"]:
            raise ValueError("FarmBot is not connected")
        if state["locked"]:
            raise ValueError("FarmBot is emergency-stopped")
        if (
            state["busy"]
            or self._soil_capture_lock.locked()
            or any(not task.done() for task in self._grid_repair_tasks)
            or any(not task.done() for task in self._soil_capture_tasks)
        ):
            raise ValueError("FarmBot is busy")
        bounds = state["axis_bounds"]
        if any(bounds[axis] is None for axis in ("x", "y", "z")):
            raise ValueError("FarmBot axis bounds are unavailable")
        normalized = []
        for target in targets:
            try:
                item = {axis: float(target[axis]) for axis in ("x", "y", "z")}
            except (KeyError, TypeError, ValueError) as err:
                raise ValueError("Repair targets require numeric X, Y and Z") from err
            if not all(math.isfinite(value) for value in item.values()):
                raise ValueError("Repair target coordinates must be finite")
            if any(
                not bounds[axis][0] <= item[axis] <= bounds[axis][1] for axis in ("x", "y", "z")
            ):
                raise ValueError("Repair target is outside FarmBot bounds")
            normalized.append(item)
        repair_id = str(uuid.uuid4())
        self.grid_repairs[repair_id] = {
            "repair_id": repair_id,
            "status": "queued",
            "message": "Photo-grid repair queued",
            "targets": normalized,
            "frames": [],
            "completed_targets": [],
            "failed_targets": [],
            "created_at": dt_util.utcnow().isoformat(),
        }
        task = asyncio.create_task(
            self._run_grid_repair(
                repair_id=repair_id,
                targets=normalized,
                original_position=state["position"],
            ),
            name=f"farmbot-grid-repair-{repair_id}",
        )
        self._grid_repair_tasks.add(task)
        task.add_done_callback(self._grid_repair_tasks.discard)
        return repair_id

    async def _run_grid_repair(
        self,
        *,
        repair_id: str,
        targets: list[dict[str, float]],
        original_position: dict[str, Any],
    ) -> None:
        record = self.grid_repairs[repair_id]
        final_status, final_message = "failed", "Photo-grid repair failed"
        async with self._soil_capture_lock:
            try:
                for target in targets:
                    before = {
                        int(item["id"])
                        for item in await self.api.async_get_images()
                        if isinstance(item, dict) and item.get("id") is not None
                    }
                    started_at = dt_util.utcnow()
                    record.update(
                        status="running",
                        message=(
                            "Moving safely to photo-grid cell "
                            f"X {target['x']:.1f}, Y {target['y']:.1f}, Z {target['z']:.1f}"
                        ),
                        started_at=record.get("started_at") or started_at.isoformat(),
                        current_target=target,
                    )
                    # Movement is its own acknowledged RPC. Keeping take_photo
                    # out of the same multi-command request prevents a camera
                    # failure from allowing later targets to run at a stale
                    # position, which previously produced repeated images.
                    await self.async_rpc_request(
                        [
                            {
                                "kind": "move",
                                "args": {
                                    **target,
                                    "speed": 100,
                                    "safe_z": True,
                                },
                            }
                        ],
                        timeout=SOIL_RPC_TIMEOUT_SECONDS,
                    )
                    frame = None
                    for attempt in range(1, GRID_REPAIR_MAX_PHOTO_ATTEMPTS + 1):
                        record.update(
                            status="waiting_images",
                            message=(
                                f"Taking photo {len(record['frames']) + 1} of {len(targets)} "
                                f"(camera attempt {attempt}/{GRID_REPAIR_MAX_PHOTO_ATTEMPTS})"
                            ),
                            photo_attempt=attempt,
                        )
                        try:
                            await self.async_rpc_request(
                                [
                                    {
                                        "kind": "wait",
                                        "args": {"milliseconds": SOIL_CAPTURE_SETTLE_MILLISECONDS},
                                    },
                                    {"kind": "take_photo", "args": {}},
                                ],
                                timeout=SOIL_RPC_TIMEOUT_SECONDS,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as err:  # pylint: disable=broad-except
                            _LOGGER.warning(
                                "Photo-grid repair %s photo RPC attempt %d/%d failed: %s",
                                repair_id,
                                attempt,
                                GRID_REPAIR_MAX_PHOTO_ATTEMPTS,
                                err,
                            )
                            continue
                        frame = await self._wait_for_grid_image(
                            before=before,
                            target=target,
                            started_at=started_at,
                            timeout=GRID_REPAIR_IMAGE_TIMEOUT_SECONDS,
                        )
                        if frame is not None:
                            break
                        _LOGGER.warning(
                            "Photo-grid repair %s got no processed image at "
                            "X %.1f Y %.1f Z %.1f on attempt %d/%d",
                            repair_id,
                            target["x"],
                            target["y"],
                            target["z"],
                            attempt,
                            GRID_REPAIR_MAX_PHOTO_ATTEMPTS,
                        )
                    if frame is None:
                        record["failed_targets"].append(target)
                        raise TimeoutError(
                            "FarmBot did not produce a processed image at "
                            f"X {target['x']:.1f}, Y {target['y']:.1f}, Z {target['z']:.1f} "
                            f"after {GRID_REPAIR_MAX_PHOTO_ATTEMPTS} attempts"
                        )
                    record["frames"].append(frame)
                    record["completed_targets"].append(target)
                final_status = "complete"
                final_message = (
                    f"Verified {len(record['frames'])} photo-grid image(s) at "
                    "the requested coordinates"
                )
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Photo-grid repair %s failed: %s", repair_id, err)
                final_message = str(err)[:240] or final_message
            finally:
                if all(original_position.get(axis) is not None for axis in ("x", "y", "z")):
                    try:
                        await self.async_rpc_request(
                            [
                                {
                                    "kind": "move",
                                    "args": {
                                        **{
                                            axis: float(original_position[axis])
                                            for axis in ("x", "y", "z")
                                        },
                                        "speed": 100,
                                        "safe_z": True,
                                    },
                                }
                            ],
                            timeout=60,
                        )
                    except Exception as err:  # pylint: disable=broad-except
                        _LOGGER.warning(
                            "Could not restore position after grid repair %s: %s",
                            repair_id,
                            err,
                        )
                record.update(
                    status=final_status,
                    message=final_message,
                    completed_at=dt_util.utcnow().isoformat(),
                )

    async def _wait_for_grid_image(
        self,
        *,
        before: set[int],
        target: dict[str, float],
        started_at,
        timeout: float,
    ) -> dict[str, Any] | None:
        """Return a new processed image only when it matches the target cell.

        ``take_photo`` failures are out-of-band in FarmBot OS. Polling the
        image API is therefore the success signal; checking coordinates also
        rejects the repeated-at-the-old-position failure mode.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            images = await self.api.async_get_images()
            matches: list[tuple[float, dict[str, Any]]] = []
            for image in images:
                if not isinstance(image, dict) or not vision.is_image_ready(image):
                    continue
                try:
                    image_id = int(image["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if image_id in before or not vision.same_device(
                    image.get("device_id"), self.device_id
                ):
                    continue
                created = dt_util.parse_datetime(str(image.get("created_at") or ""))
                if created is not None:
                    try:
                        if created < started_at:
                            continue
                    except TypeError:
                        continue
                meta = self._image_meta(image)
                try:
                    distance = math.sqrt(
                        sum((float(meta[axis]) - target[axis]) ** 2 for axis in ("x", "y", "z"))
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if distance <= GRID_REPAIR_COORDINATE_TOLERANCE_MM:
                    matches.append((distance, image))
            if matches:
                distance, image = min(matches, key=lambda item: item[0])
                meta = self._image_meta(image)
                return {
                    "image_id": int(image["id"]),
                    "x": float(meta["x"]),
                    "y": float(meta["y"]),
                    "z": float(meta.get("z") or 0),
                    "distance_from_target_mm": distance,
                }
            await asyncio.sleep(2)
        return None

    def grid_repair(self, repair_id: str) -> dict[str, Any] | None:
        record = self.grid_repairs.get(repair_id)
        return dict(record) if record is not None else None

    def _claim_active_soil_images(self, ready: dict[int, dict[str, Any]]) -> None:
        """Claim matching capture frames before the ordinary image event can see them."""
        for record in self.soil_captures.values():
            if record.get("status") not in {"running", "waiting_images"}:
                continue
            expected = record.get("expected_frames") or []
            started_at = dt_util.parse_datetime(str(record.get("started_at") or ""))
            before = set(record.get("before_image_ids") or [])
            candidates = []
            for image_id, image in ready.items():
                if image_id in before:
                    continue
                created = dt_util.parse_datetime(str(image.get("created_at") or ""))
                if started_at is not None and created is not None:
                    try:
                        if created < started_at:
                            continue
                    except TypeError:
                        continue
                candidates.append(image)
            self._claimed_soil_image_ids.update(
                frame["image_id"] for frame in self._match_soil_frames(candidates, expected)
            )

    # -------------------- FarmBot Vision bridge --------------------

    def _trigger_reauth_from_async(self) -> None:
        """Reauth trigger for callbacks invoked directly on the event loop.

        Used by FarmbotApiClient (all of whose calls run on the event
        loop), as opposed to the executor-thread callers (MQTT, blocking
        HTTP) that must hop back onto the loop via call_soon_threadsafe.
        Shares ``_auth_failed`` with those other triggers so a FarmBot
        auth failure detected anywhere only starts reauth once.
        """
        if self._entry and not self._auth_failed:
            _LOGGER.warning("FarmBot API authentication failed, triggering reauth flow")
            self._auth_failed = True
            self._entry.async_start_reauth(self.hass)

    def vision_options(self) -> dict:
        """Return FarmBot Vision integration options, merged with defaults."""
        options = dict(self._entry.options) if self._entry is not None else {}
        return {
            OPTION_VISION_ENABLED: options.get(OPTION_VISION_ENABLED, DEFAULT_VISION_ENABLED),
            OPTION_VISION_HEARTBEAT_TIMEOUT_MINUTES: options.get(
                OPTION_VISION_HEARTBEAT_TIMEOUT_MINUTES,
                DEFAULT_VISION_HEARTBEAT_TIMEOUT_MINUTES,
            ),
        }

    def vision_is_available(self, *, now=None) -> bool:
        """True when a FarmBot Vision heartbeat was received within the timeout."""
        if self.vision_last_heartbeat is None:
            return False
        now = now or dt_util.utcnow()
        timeout_minutes = self.vision_options()[OPTION_VISION_HEARTBEAT_TIMEOUT_MINUTES]
        return (now - self.vision_last_heartbeat) < timedelta(minutes=timeout_minutes)

    def update_vision_status(
        self,
        *,
        available: Optional[bool] = None,
        status: str = "idle",
        job_id: Optional[str] = None,
        last_completed_at=None,
        plants_analysed: Optional[int] = None,
        recommendations: Optional[int] = None,
        automatically_applied: Optional[int] = None,
        uncertain: Optional[int] = None,
        message: Optional[str] = None,
        app_version: Optional[str] = None,
    ) -> bool:
        """Record a FarmBot Vision status report (a heartbeat).

        Real availability is derived from heartbeat recency
        (``vision_is_available``), not from the app's self-reported
        ``available`` flag -- that value is stored only as an attribute,
        never trusted as the source of truth. Returns True if any
        reported value actually changed, so callers can skip redundant
        entity dispatch for identical repeated reports.
        """
        self.vision_last_heartbeat = dt_util.utcnow()
        self.vision_app_reported_available = available

        snapshot = (
            status,
            job_id,
            last_completed_at,
            plants_analysed,
            recommendations,
            automatically_applied,
            uncertain,
            message,
            app_version,
        )
        changed = snapshot != self._last_vision_report_snapshot
        self._last_vision_report_snapshot = snapshot

        self.vision_status = status
        self.vision_job_id = job_id
        self.vision_message = message
        self.vision_app_version = app_version
        if last_completed_at is not None:
            parsed = dt_util.parse_datetime(str(last_completed_at))
            if parsed is not None:
                self.vision_last_completed_at = parsed
        if plants_analysed is not None:
            self.vision_plants_analysed = plants_analysed
        if recommendations is not None:
            self.vision_recommendations = recommendations
        if automatically_applied is not None:
            self.vision_automatically_applied = automatically_applied
        if uncertain is not None:
            self.vision_uncertain = uncertain

        if changed:
            async_dispatcher_send(self.hass, SIGNAL_VISION_STATE)
        return changed

    async def async_poll_new_vision_images(self) -> list[int]:
        """Detect newly processed FarmBot photos and request their analysis.

        FarmBot does not expose a stable image-complete MQTT event across all
        supported firmware versions. Polling the small ``/images`` metadata
        response is therefore the reliable bridge: image bytes are still only
        downloaded by ``get_vision_image`` after the companion app accepts the
        request. The first successful poll establishes a baseline so installing
        or restarting the integration does not replay the whole image history.
        """
        try:
            images = await self.api.async_get_images()
        except FarmbotApiError as err:
            _LOGGER.warning("Could not poll FarmBot images for Vision: %s", err)
            return []

        ready: dict[int, dict] = {}
        for image in images:
            if not isinstance(image, dict) or not vision.is_image_ready(image):
                continue
            if not vision.same_device(image.get("device_id"), self.device_id):
                continue
            try:
                image_id = int(image["id"])
            except (KeyError, TypeError, ValueError):
                continue
            ready[image_id] = image

        ready_ids = set(ready)
        if self._known_ready_vision_image_ids is None:
            # Do not replay historical photos on startup. A photo created after
            # this manager started is not historical, even if it completed
            # while the first metadata request was in flight.
            new_ids = set()
            for image_id, image in ready.items():
                created = dt_util.parse_datetime(str(image.get("created_at") or ""))
                if created is None:
                    continue
                try:
                    created_after_start = created >= self._vision_image_monitor_started_at
                except TypeError:
                    created_after_start = False
                if created_after_start:
                    new_ids.add(image_id)
        else:
            new_ids = ready_ids - self._known_ready_vision_image_ids
        self._claim_active_soil_images(ready)
        new_ids -= self._claimed_soil_image_ids
        if self._known_ready_vision_image_ids is None:
            self._known_ready_vision_image_ids = ready_ids
        else:
            # Keep a durable in-memory seen set so a temporarily incomplete API
            # response cannot make an older image look new on the next poll.
            self._known_ready_vision_image_ids.update(ready_ids)

        ordered_ids = sorted(
            new_ids,
            key=lambda image_id: str(ready[image_id].get("created_at") or ""),
        )
        for image_id in ordered_ids:
            self.hass.bus.async_fire(
                EVENT_VISION_REQUEST,
                {
                    "config_entry_id": self.entry_id,
                    "device_id": self.device_id,
                    "plant_ids": [],
                    "image_id": image_id,
                },
            )
            _LOGGER.info("Requested FarmBot Vision analysis for new image %s", image_id)
        return ordered_ids

    async def async_close(self) -> None:
        """Release any FarmBot resources owned exclusively by this manager.

        The REST client reuses Home Assistant's shared aiohttp session,
        which Home Assistant itself owns and closes; there is nothing
        entry-specific to close today. Kept as an explicit hook so a
        future FarmBot-owned resource has an obvious place to release on
        unload.
        """
        for task in list(self._soil_capture_tasks):
            task.cancel()
        for task in list(self._grid_repair_tasks):
            task.cancel()
        if self._soil_capture_tasks:
            await asyncio.gather(*self._soil_capture_tasks, return_exceptions=True)
        if self._grid_repair_tasks:
            await asyncio.gather(*self._grid_repair_tasks, return_exceptions=True)
        for future in self._pending_rpcs.values():
            if not future.done():
                future.cancel()
        self._pending_rpcs.clear()
