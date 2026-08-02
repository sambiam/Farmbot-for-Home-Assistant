import asyncio
import json
import logging
import math
import re
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

import paho.mqtt.client as mqtt
import requests
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from . import gcode as gcode_lib
from . import vision
from .api import FarmbotApiClient, FarmbotApiError
from .const import (
    API_BASE_URL,
    DEFAULT_VISION_ENABLED,
    DEFAULT_VISION_HEARTBEAT_TIMEOUT_MINUTES,
    EVENT_BUTTON_INPUT,
    EVENT_VISION_REQUEST,
    GCODE_CHUNK_RPC_TIMEOUT_SECONDS,
    GCODE_DEFAULT_FEED_MM_PER_MIN,
    GRID_REPAIR_COORDINATE_TOLERANCE_MM,
    GRID_REPAIR_FLAT_TRAVEL_TOP_MARGIN_MM,
    GRID_REPAIR_IMAGE_TIMEOUT_SECONDS,
    GRID_REPAIR_LIGHTING_PIN,
    GRID_REPAIR_MAX_CONSECUTIVE_FAILURES,
    GRID_REPAIR_MAX_PHOTO_ATTEMPTS,
    GRID_REPAIR_POSITION_TIMEOUT_SECONDS,
    GRID_REPAIR_POSITION_TOLERANCE_MM,
    MQTT_PORT,
    OPTION_VISION_ENABLED,
    OPTION_VISION_HEARTBEAT_TIMEOUT_MINUTES,
    SIGNAL_BUTTON_INPUT,
    SIGNAL_SEQUENCE_SELECTED,
    SIGNAL_STATE,
    SIGNAL_VISION_STATE,
    SOIL_CAPTURE_COORDINATE_TOLERANCE_MM,
    SOIL_CAPTURE_IMAGE_TIMEOUT_SECONDS,
    SOIL_CAPTURE_LIGHTING_PIN,
    SOIL_CAPTURE_MAX_PHOTO_ATTEMPTS,
    SOIL_CAPTURE_POSITION_TIMEOUT_SECONDS,
    SOIL_CAPTURE_POSITION_TOLERANCE_MM,
    SOIL_CAPTURE_SETTLE_MILLISECONDS,
    SOIL_RPC_TIMEOUT_SECONDS,
    TOKEN_REFRESH_WINDOW,
    TOPIC_COMMAND,
    TOPIC_FROM_DEVICE,
    TOPIC_LOGS,
    TOPIC_STATUS,
    WEEDING_MAX_ATTEMPTS,
    WEEDING_MAX_PATH_MM,
    WEEDING_RPC_TIMEOUT_SECONDS,
)
from .image_utils import inspect_capture_image
from .jwt_util import decode_jwt_payload

_LOGGER = logging.getLogger(__name__)

_PIN_BINDING_TRIGGER_RE = re.compile(r"^(?P<label>.+?) triggered, executing (?P<action>.+)$")
_PIN_BINDING_FAILURE_RE = re.compile(
    r"^(?:Failed to find associated Sequence for:|Unknown PinBinding:)\s*(?P<label>.+)$"
)
_BUTTON_PIN_RE = re.compile(r"\(Pi (?P<pin>\d+)\)|Pi GPIO (?P<gpio>\d+)")


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
        self._status_revision = 0
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
        self.last_button_input: Optional[dict[str, Any]] = None
        self.button_input_count = 0
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
        self.gcode_runs: dict[str, dict[str, Any]] = {}
        self._gcode_tasks: set[asyncio.Task] = set()
        self.weeding_runs: dict[str, dict[str, Any]] = {}
        self._weeding_tasks: set[asyncio.Task] = set()

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
            self._status_revision += 1
            # Paho callback thread -> HA loop:
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send, self.hass, SIGNAL_STATE, self.status
            )
        elif msg.topic == TOPIC_FROM_DEVICE.format(device_id=self.device_id):
            self.hass.loop.call_soon_threadsafe(self._resolve_rpc_response, payload)
        elif msg.topic == TOPIC_LOGS.format(device_id=self.device_id):
            self.hass.loop.call_soon_threadsafe(self._handle_log_message, payload)
        else:
            _LOGGER.debug("Unhandled topic %s", msg.topic)

    def _handle_log_message(self, payload: dict[str, Any]) -> None:
        """Turn FarmBot OS PinBinding trigger logs into durable HA diagnostics."""
        message = str(payload.get("message") or "").strip()
        trigger = _PIN_BINDING_TRIGGER_RE.match(message)
        failure = _PIN_BINDING_FAILURE_RE.match(message)
        if trigger is None and failure is None:
            return

        match = trigger or failure
        assert match is not None
        label = match.group("label").strip()
        pin_match = _BUTTON_PIN_RE.search(label)
        if pin_match is None:
            return

        created_at = payload.get("created_at")
        try:
            observed_at = datetime.fromtimestamp(float(created_at), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            observed_at = dt_util.utcnow()

        self.button_input_count += 1
        event = {
            "device_id": self.device_id,
            "gpio": int(pin_match.group("pin") or pin_match.group("gpio")),
            "button": label,
            "action": trigger.group("action").strip() if trigger else "configuration_error",
            "observed_at": observed_at.isoformat(),
            "press_count": self.button_input_count,
            "source": "farmbot_os_pin_binding_log",
            "message": message,
        }
        self.last_button_input = {**event, "observed_at_datetime": observed_at}
        _LOGGER.info(
            "FarmBot button input: GPIO %s (%s), action=%s",
            event["gpio"],
            label,
            event["action"],
        )
        async_dispatcher_send(self.hass, SIGNAL_BUTTON_INPUT)
        self.hass.bus.async_fire(EVENT_BUTTON_INPUT, event)

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
        cs = [
            self._move_command(
                x=None if x is None else float(x),
                y=None if y is None else float(y),
                z=None if z is None else float(z),
                speed=int(speed),
            )
        ]
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
    def _move_command(
        *,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        speed: int = 100,
        safe_z: bool = False,
    ) -> dict[str, Any]:
        """Build the modern FarmBot ``move`` CeleryScript AST.

        FarmBot OS reads coordinates from ``axis_overwrite`` nodes in the
        command body. Putting X/Y/Z in ``move.args`` is accepted but ignored,
        producing a successful no-op RPC.
        """
        coordinates = {"x": x, "y": y, "z": z}
        body: list[dict[str, Any]] = []
        for axis, value in coordinates.items():
            if value is None:
                continue
            body.append(
                {
                    "kind": "axis_overwrite",
                    "args": {
                        "axis": axis,
                        "axis_operand": {
                            "kind": "numeric",
                            "args": {"number": float(value)},
                        },
                    },
                }
            )
        for axis, value in coordinates.items():
            if value is None:
                continue
            body.append(
                {
                    "kind": "speed_overwrite",
                    "args": {
                        "axis": axis,
                        "speed_setting": {
                            "kind": "numeric",
                            "args": {"number": int(speed)},
                        },
                    },
                }
            )
        if safe_z:
            body.append({"kind": "safe_z", "args": {}})
        return {"kind": "move", "args": {}, "body": body}

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

    def _live_connection_state(self) -> dict[str, bool]:
        """Connected/locked flags from live status, independent of firmware config.

        Split out of ``soil_motion_state`` so callers that only need to know
        whether the bot is currently reachable and not emergency-stopped
        (e.g. mid-batch abort checks) don't need a firmware config at hand.
        """
        info = (self.status or {}).get("informational_settings") or {}
        return {
            "connected": self._mqtt is not None and self._mqtt_connected,
            "locked": bool(info.get("locked", False)),
        }

    def soil_motion_state(self, firmware_config: dict[str, Any]) -> dict[str, Any]:
        info = (self.status or {}).get("informational_settings") or {}
        position = (self.status or {}).get("location_data", {}).get("position") or {}
        connection = self._live_connection_state()
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
            "connected": connection["connected"],
            "busy": bool(info.get("busy", False)) or self._soil_capture_lock.locked(),
            "locked": connection["locked"],
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
                        FarmbotManager._move_command(
                            x=x,
                            y=frame_y,
                            z=z,
                            speed=100,
                            safe_z=True,
                        ),
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
        # Prune tasks whose done-callback discard hasn't run yet so a batch
        # that just finished doesn't cause a spurious "FarmBot is busy".
        self._soil_capture_tasks = {t for t in self._soil_capture_tasks if not t.done()}
        self._grid_repair_tasks = {t for t in self._grid_repair_tasks if not t.done()}
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
        light_state = (self.status or {}).get("pins", {}).get(str(SOIL_CAPTURE_LIGHTING_PIN), 0)
        initial_light_value = int(
            bool(light_state.get("value", 0) if isinstance(light_state, dict) else light_state)
        )
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
                _commands, expected = self._soil_capture_commands(
                    x=float(point["x"]),
                    y=float(point["y"]),
                    capture_z=capture_z,
                    lateral_offsets=lateral_offsets,
                    z_offsets=z_offsets,
                    z_direction=z_direction,
                )
                record.update(
                    status="running",
                    message="Preparing verified soil image capture",
                    expected_frames=expected,
                    started_at=started_at.isoformat(),
                    attempts=[],
                )
                if not initial_light_value:
                    await self.async_rpc_request(
                        [
                            {
                                "kind": "write_pin",
                                "args": {
                                    "pin_number": SOIL_CAPTURE_LIGHTING_PIN,
                                    "pin_value": 1,
                                    "pin_mode": 0,
                                },
                            }
                        ],
                        timeout=SOIL_RPC_TIMEOUT_SECONDS,
                    )
                    _LOGGER.info(
                        "Soil capture %s switched lighting pin %d on",
                        capture_id,
                        SOIL_CAPTURE_LIGHTING_PIN,
                    )

                total = len(expected)
                for frame_number, target in enumerate(expected, start=1):
                    record.update(
                        status="running",
                        message=(
                            f"Moving to soil frame {frame_number}/{total} at "
                            f"X {target['x']:.1f}, Y {target['y']:.1f}, Z {target['z']:.1f}"
                        ),
                        current_frame=frame_number,
                    )
                    coordinates = {axis: float(target[axis]) for axis in ("x", "y", "z")}
                    await self.async_rpc_request(
                        [self._move_command(**coordinates, speed=100, safe_z=True)],
                        timeout=SOIL_RPC_TIMEOUT_SECONDS,
                    )
                    reported = await self._wait_for_grid_position(
                        target=target,
                        timeout=SOIL_CAPTURE_POSITION_TIMEOUT_SECONDS,
                        tolerance_mm=SOIL_CAPTURE_POSITION_TOLERANCE_MM,
                    )
                    if reported is None:
                        observed = self._reported_position()
                        observed_text = (
                            "unavailable"
                            if observed is None
                            else (
                                f"X {observed['x']:.1f}, Y {observed['y']:.1f}, "
                                f"Z {observed['z']:.1f}"
                            )
                        )
                        raise RuntimeError(
                            f"soil frame {frame_number}/{total}: FarmBot did not reach "
                            f"X {target['x']:.1f}, Y {target['y']:.1f}, Z {target['z']:.1f} "
                            f"within {SOIL_CAPTURE_POSITION_TOLERANCE_MM:g} mm; "
                            f"last position was {observed_text}"
                        )

                    accepted = None
                    last_reason = "camera did not produce an image"
                    for attempt in range(1, SOIL_CAPTURE_MAX_PHOTO_ATTEMPTS + 1):
                        attempt_started = dt_util.utcnow()
                        record.update(
                            status="waiting_images",
                            message=(
                                f"Soil frame {frame_number}/{total}: capture attempt "
                                f"{attempt}/{SOIL_CAPTURE_MAX_PHOTO_ATTEMPTS}"
                            ),
                            photo_attempt=attempt,
                        )
                        try:
                            await self.async_rpc_request(
                                [
                                    {
                                        "kind": "wait",
                                        "args": {
                                            "milliseconds": SOIL_CAPTURE_SETTLE_MILLISECONDS
                                        },
                                    },
                                    {"kind": "take_photo", "args": {}},
                                ],
                                timeout=SOIL_RPC_TIMEOUT_SECONDS,
                            )
                            frame, image, reason = await self._wait_for_soil_frame_image(
                                before=before,
                                target=target,
                                started_at=attempt_started,
                                timeout=SOIL_CAPTURE_IMAGE_TIMEOUT_SECONDS,
                            )
                            if image is not None and image.get("id") is not None:
                                image_id = int(image["id"])
                                before.add(image_id)
                                self._claimed_soil_image_ids.add(image_id)
                            if frame is None or image is None:
                                last_reason = reason
                            else:
                                attachment_url = image.get("attachment_url")
                                if not attachment_url:
                                    last_reason = "processed image had no downloadable attachment"
                                else:
                                    raw, _content_type = await self.api.async_download_image(
                                        str(attachment_url)
                                    )
                                    quality = await self.hass.async_add_executor_job(
                                        inspect_capture_image, raw
                                    )
                                    if quality.usable:
                                        accepted = {
                                            **frame,
                                            "capture_attempt": attempt,
                                            "quality": "usable",
                                            "contrast": quality.contrast,
                                            "detail_score": quality.laplacian_energy,
                                        }
                                        break
                                    last_reason = quality.reason
                        except asyncio.CancelledError:
                            raise
                        except Exception as err:  # pylint: disable=broad-except
                            last_reason = str(err)[:180] or type(err).__name__

                        attempt_record = {
                            "frame": frame_number,
                            "attempt": attempt,
                            "reason": last_reason,
                        }
                        record["attempts"].append(attempt_record)
                        record["message"] = (
                            f"Soil frame {frame_number}/{total} attempt {attempt}/"
                            f"{SOIL_CAPTURE_MAX_PHOTO_ATTEMPTS} rejected: {last_reason}"
                        )[:240]
                        _LOGGER.warning(
                            "Soil capture %s frame %d/%d attempt %d/%d rejected: %s",
                            capture_id,
                            frame_number,
                            total,
                            attempt,
                            SOIL_CAPTURE_MAX_PHOTO_ATTEMPTS,
                            last_reason,
                        )

                    if accepted is None:
                        raise RuntimeError(
                            f"soil frame {frame_number}/{total} failed after "
                            f"{SOIL_CAPTURE_MAX_PHOTO_ATTEMPTS} attempts: {last_reason}"
                        )
                    record["frames"].append(accepted)
                    _LOGGER.info(
                        "Soil capture %s accepted frame %d/%d image %s on attempt %d at "
                        "X %.1f Y %.1f Z %.1f",
                        capture_id,
                        frame_number,
                        total,
                        accepted["image_id"],
                        accepted["capture_attempt"],
                        accepted["x"],
                        accepted["y"],
                        accepted["z"],
                    )
                final_status = "complete"
                final_message = f"Captured {len(record['frames'])} soil images"
                record.update(message="Restoring the FarmBot starting position")
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Soil capture %s failed: %s", capture_id, err)
                final_message = str(err)[:240] or "Soil capture failed"
                record.update(message="Capture failed; restoring the FarmBot position")
            finally:
                if not initial_light_value:
                    try:
                        await self.async_rpc_request(
                            [
                                {
                                    "kind": "write_pin",
                                    "args": {
                                        "pin_number": SOIL_CAPTURE_LIGHTING_PIN,
                                        "pin_value": 0,
                                        "pin_mode": 0,
                                    },
                                }
                            ],
                            timeout=SOIL_RPC_TIMEOUT_SECONDS,
                        )
                    except Exception as err:  # pylint: disable=broad-except
                        _LOGGER.warning(
                            "Could not restore lighting after soil capture %s: %s",
                            capture_id,
                            err,
                        )
                if all(original_position.get(axis) is not None for axis in ("x", "y", "z")):
                    try:
                        await self.async_rpc_request(
                            [
                                self._move_command(
                                    **{
                                        axis: float(original_position[axis])
                                        for axis in ("x", "y", "z")
                                    },
                                    speed=100,
                                    safe_z=True,
                                )
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

    async def _wait_for_soil_frame_image(
        self,
        *,
        before: set[int],
        target: dict[str, float],
        started_at,
        timeout: float,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        """Wait for one new processed image and validate its recorded coordinates."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            images = await self.api.async_get_images()
            candidates: list[tuple[float, dict[str, Any]]] = []
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
                        sum(
                            (float(meta[axis]) - float(target[axis])) ** 2
                            for axis in ("x", "y", "z")
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                candidates.append((distance, image))
            if candidates:
                distance, image = min(candidates, key=lambda item: item[0])
                meta = self._image_meta(image)
                if distance > SOIL_CAPTURE_COORDINATE_TOLERANCE_MM:
                    return (
                        None,
                        image,
                        f"image coordinates missed the target by {distance:.1f} mm "
                        f"(limit {SOIL_CAPTURE_COORDINATE_TOLERANCE_MM:g} mm)",
                    )
                return (
                    {
                        "image_id": int(image["id"]),
                        "x": float(meta["x"]),
                        "y": float(meta["y"]),
                        "z": float(meta.get("z") or 0),
                        "lateral_offset_mm": float(target["lateral_offset_mm"]),
                        "z_offset_mm": float(target["z_offset_mm"]),
                        "distance_from_target_mm": distance,
                    },
                    image,
                    "usable",
                )
            await asyncio.sleep(2)
        return None, None, "no new processed image appeared before the upload timeout"

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
        # Prune tasks whose done-callback discard hasn't run yet so a batch
        # that just finished doesn't cause a spurious "FarmBot is busy" when
        # the Vision app immediately queues the next chunk.
        self._grid_repair_tasks = {t for t in self._grid_repair_tasks if not t.done()}
        self._soil_capture_tasks = {t for t in self._soil_capture_tasks if not t.done()}
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
        for position, target in enumerate(targets):
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
            # A caller that tracks cells by identity supplies its own index;
            # everyone else gets this call's position, which is still stable
            # for the lifetime of the run.
            raw_index = target.get("index") if isinstance(target, dict) else None
            item["index"] = position if raw_index is None else int(raw_index)
            normalized.append(item)
        if len({item["index"] for item in normalized}) != len(normalized):
            raise ValueError("Repair target indexes must be unique")
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
                flat_travel=self._grid_flat_travel(normalized, bounds["z"]),
            ),
            name=f"farmbot-grid-repair-{repair_id}",
        )
        self._grid_repair_tasks.add(task)
        task.add_done_callback(self._grid_repair_tasks.discard)
        return repair_id

    @staticmethod
    def _grid_flat_travel(
        targets: list[dict[str, float]],
        z_bounds: list[float] | None,
    ) -> bool:
        """May cell-to-cell travel skip FarmBot's ``safe_z`` retract?

        Only when every cell is photographed at the same Z *and* that Z is
        already within GRID_REPAIR_FLAT_TRAVEL_TOP_MARGIN_MM of the top of the
        Z axis. Under those two conditions ``safe_z`` cannot lift the gantry
        anywhere it isn't already, so retracting and descending once per cell
        buys no clearance -- it just adds two Z moves and their settle time to
        all 77 cells. Any lower capture height, or a grid whose cells differ
        in Z, keeps ``safe_z`` on every move.
        """
        if not targets or z_bounds is None:
            return False
        heights = {round(float(target["z"]), 3) for target in targets}
        if len(heights) != 1:
            return False
        z = next(iter(heights))
        top = float(z_bounds[1])
        return math.isfinite(top) and abs(top - z) <= GRID_REPAIR_FLAT_TRAVEL_TOP_MARGIN_MM

    async def _capture_grid_target(
        self,
        *,
        repair_id: str,
        record: dict[str, Any],
        target: dict[str, float],
        target_number: int,
        total: int,
        safe_z: bool = True,
    ) -> dict[str, Any] | None:
        """Move to and photograph a single photo-grid cell.

        Returns the captured frame dict on success. On a known failure mode
        (movement not confirmed, or no processed image after retries) the
        target is appended to ``record["failed_targets"]``, a human-readable
        reason is appended to ``record["failure_reasons"]``, a warning is
        logged, and ``None`` is returned so the caller can move on to the
        next target instead of aborting the whole batch. Any unexpected
        exception (e.g. the move RPC itself failing) is left to propagate so
        the caller can record and continue the same way.
        """

        def _record_failure(reason: str, code: str) -> None:
            record["failed_targets"].append(target)
            record.setdefault("failure_reasons", []).append(reason)
            record.setdefault("failures", []).append(
                {"index": target.get("index"), "reason": reason, "code": code}
            )
            _LOGGER.warning(
                "Photo-grid repair %s target %d/%d at X %.1f Y %.1f Z %.1f failed: %s",
                repair_id,
                target_number,
                total,
                target["x"],
                target["y"],
                target["z"],
                reason,
            )

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
        # Movement is its own acknowledged RPC. Keeping take_photo out of the
        # same multi-command request prevents a camera failure from allowing
        # later targets to run at a stale position, which previously
        # produced repeated images.
        coordinates = {axis: float(target[axis]) for axis in ("x", "y", "z")}
        await self.async_rpc_request(
            [
                self._move_command(
                    **coordinates,
                    speed=100,
                    safe_z=safe_z,
                )
            ],
            timeout=SOIL_RPC_TIMEOUT_SECONDS,
        )
        reported_position = await self._wait_for_grid_position(
            target=target,
            timeout=GRID_REPAIR_POSITION_TIMEOUT_SECONDS,
        )
        record["reported_position"] = reported_position or self._reported_position()
        if reported_position is None:
            observed = record["reported_position"]
            record["movement_failure"] = {
                "requested_target": target,
                "reported_position": observed,
            }
            observed_text = (
                "unavailable"
                if observed is None
                else (f"X {observed['x']:.1f}, Y {observed['y']:.1f}, Z {observed['z']:.1f}")
            )
            _record_failure(
                "FarmBot did not reach the requested photo-grid cell "
                f"X {target['x']:.1f}, Y {target['y']:.1f}, Z {target['z']:.1f}; "
                f"last live position was {observed_text}. No photo was requested.",
                "movement",
            )
            return None
        record.update(
            message=(
                "FarmBot position confirmed at "
                f"X {reported_position['x']:.1f}, "
                f"Y {reported_position['y']:.1f}, "
                f"Z {reported_position['z']:.1f}"
            )
        )
        frame = None
        # "camera" means take_photo itself was never accepted; "upload_timeout"
        # means it was accepted but no matching processed image appeared within
        # GRID_REPAIR_IMAGE_TIMEOUT_SECONDS -- an unknown completion state that
        # must never be reported as a captured cell.
        failure_code = "camera"
        for attempt in range(1, GRID_REPAIR_MAX_PHOTO_ATTEMPTS + 1):
            record.update(
                status="waiting_images",
                message=(
                    f"Taking photo {len(record['frames']) + 1} of {total} "
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
                failure_code = "camera"
                _LOGGER.warning(
                    "Photo-grid repair %s photo RPC attempt %d/%d failed: %s",
                    repair_id,
                    attempt,
                    GRID_REPAIR_MAX_PHOTO_ATTEMPTS,
                    err,
                )
                continue
            failure_code = "upload_timeout"
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
            _record_failure(
                "FarmBot did not produce a processed image at "
                f"X {target['x']:.1f}, Y {target['y']:.1f}, Z {target['z']:.1f} "
                f"after {GRID_REPAIR_MAX_PHOTO_ATTEMPTS} attempts",
                failure_code,
            )
            return None
        return {**frame, "target_index": target.get("index")}

    async def _run_grid_repair(
        self,
        *,
        repair_id: str,
        targets: list[dict[str, float]],
        original_position: dict[str, Any],
        flat_travel: bool = False,
    ) -> None:
        """Photograph every requested cell as one continuous run.

        Lighting, the drive in from wherever the gantry was parked and the
        drive back to it are run-level: they happen once around the whole
        route, never per cell and never per caller-side batch.
        """
        record = self.grid_repairs[repair_id]
        final_status, final_message = "failed", "Photo-grid repair failed"
        light_state = (self.status or {}).get("pins", {}).get(str(GRID_REPAIR_LIGHTING_PIN), 0)
        initial_light_value = int(
            bool(light_state.get("value", 0) if isinstance(light_state, dict) else light_state)
        )
        async with self._soil_capture_lock:
            try:
                if not initial_light_value:
                    await self.async_rpc_request(
                        [
                            {
                                "kind": "write_pin",
                                "args": {
                                    "pin_number": GRID_REPAIR_LIGHTING_PIN,
                                    "pin_value": 1,
                                    "pin_mode": 0,
                                },
                            }
                        ],
                        timeout=SOIL_RPC_TIMEOUT_SECONDS,
                    )
                total = len(targets)
                consecutive_failures = 0
                abort_reason: str | None = None
                for index, target in enumerate(targets, start=1):
                    state = self._live_connection_state()
                    if not state["connected"] or state["locked"]:
                        stop_reason = (
                            "FarmBot is emergency-stopped"
                            if state["locked"]
                            else "FarmBot lost its MQTT connection"
                        )
                        abort_reason = (
                            f"Photo-grid repair stopped before cell {index}/{total}: {stop_reason}"
                        )
                        _LOGGER.warning("Photo-grid repair %s aborted: %s", repair_id, stop_reason)
                        break

                    try:
                        frame = await self._capture_grid_target(
                            repair_id=repair_id,
                            record=record,
                            target=target,
                            target_number=index,
                            total=total,
                            # The move into the grid starts from wherever the
                            # gantry was parked and must clear whatever is
                            # between there and the first cell. Once inside,
                            # flat travel (when allowed) keeps the camera at
                            # its capture height for the whole route.
                            safe_z=not flat_travel or index == 1,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:  # pylint: disable=broad-except
                        frame = None
                        reason = str(err)[:240] or "Unexpected error during photo-grid capture"
                        record["failed_targets"].append(target)
                        record.setdefault("failure_reasons", []).append(reason)
                        record.setdefault("failures", []).append(
                            {"index": target.get("index"), "reason": reason, "code": "error"}
                        )
                        _LOGGER.warning(
                            "Photo-grid repair %s target %d/%d at X %.1f Y %.1f Z %.1f failed: %s",
                            repair_id,
                            index,
                            total,
                            target["x"],
                            target["y"],
                            target["z"],
                            reason,
                        )

                    if frame is not None:
                        record["frames"].append(frame)
                        record["completed_targets"].append(target)
                        consecutive_failures = 0
                        continue

                    consecutive_failures += 1
                    if consecutive_failures >= GRID_REPAIR_MAX_CONSECUTIVE_FAILURES:
                        abort_reason = (
                            f"Photo-grid repair aborted after {consecutive_failures} "
                            f"consecutive failed cells (of {total} requested)"
                        )
                        _LOGGER.warning("Photo-grid repair %s aborted: %s", repair_id, abort_reason)
                        break

                # An abort leaves the tail of the route untouched. Naming those
                # cells lets the caller resume them without re-photographing
                # anything this run already captured.
                attempted = {
                    item.get("index")
                    for group in ("completed_targets", "failed_targets")
                    for item in record[group]
                }
                record["unattempted_targets"] = [
                    item for item in targets if item.get("index") not in attempted
                ]
                succeeded = len(record["frames"])
                failed_targets = record["failed_targets"]
                failure_reasons = record.get("failure_reasons") or []

                def _summary() -> str:
                    plural = "" if total == 1 else "s"
                    text = f"Captured {succeeded} of {total} photo-grid cell{plural}"
                    if failed_targets:
                        text += f"; {len(failed_targets)} failed (first: {failure_reasons[0]})"
                    return text

                if abort_reason is not None:
                    final_status = "failed"
                    final_message = f"{abort_reason}. {_summary()}."
                elif total > 0 and succeeded == total:
                    final_status = "complete"
                    final_message = (
                        f"Verified {succeeded} photo-grid image(s) at the requested coordinates"
                    )
                else:
                    final_status = "failed"
                    final_message = _summary()
                final_message = final_message[:240] or final_message
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Photo-grid repair %s failed: %s", repair_id, err)
                final_message = str(err)[:240] or final_message
            finally:
                _LOGGER.info(
                    "Photo-grid repair %s finished: requested %d target(s), "
                    "captured %d frame(s), %d failed",
                    repair_id,
                    len(targets),
                    len(record["frames"]),
                    len(record["failed_targets"]),
                )
                if not initial_light_value:
                    try:
                        await self.async_rpc_request(
                            [
                                {
                                    "kind": "write_pin",
                                    "args": {
                                        "pin_number": GRID_REPAIR_LIGHTING_PIN,
                                        "pin_value": 0,
                                        "pin_mode": 0,
                                    },
                                }
                            ],
                            timeout=SOIL_RPC_TIMEOUT_SECONDS,
                        )
                    except Exception as err:  # pylint: disable=broad-except
                        _LOGGER.warning(
                            "Could not restore lighting after grid repair %s: %s",
                            repair_id,
                            err,
                        )
                if all(original_position.get(axis) is not None for axis in ("x", "y", "z")):
                    try:
                        await self.async_rpc_request(
                            [
                                self._move_command(
                                    **{
                                        axis: float(original_position[axis])
                                        for axis in ("x", "y", "z")
                                    },
                                    speed=100,
                                    safe_z=True,
                                )
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

    # -------------------- Experimental raw G-code --------------------

    def plan_gcode(
        self, *, lines: list[str], firmware_config: dict[str, Any], feed_mm_per_min: float
    ) -> tuple[gcode_lib.GcodeProgram, dict[str, Any]]:
        """Validate a program against this bot without sending anything.

        Split out from :meth:`start_gcode_run` so a caller can dry-run a
        program -- the app previews one before every send -- and get the exact
        same verdict the real run would give.
        """
        state = self.soil_motion_state(firmware_config)
        if not state["connected"]:
            raise gcode_lib.GcodeError("FarmBot is not connected")
        if state["locked"]:
            raise gcode_lib.GcodeError("FarmBot is emergency-stopped")
        program = gcode_lib.parse_program(
            lines,
            start_position=state["position"],
            axis_bounds=state["axis_bounds"],
            firmware_config=firmware_config,
            default_feed_mm_per_min=feed_mm_per_min or GCODE_DEFAULT_FEED_MM_PER_MIN,
        )
        return program, state

    def start_gcode_run(
        self,
        *,
        lines: list[str],
        firmware_config: dict[str, Any],
        feed_mm_per_min: float,
        return_to_start: bool = True,
    ) -> str:
        """Validate a raw G-code program and start executing it.

        The program is resolved and bounds-checked in full before the first
        chunk is published, so a program that would leave the bed is refused
        outright rather than stopped partway through.
        """
        program, state = self.plan_gcode(
            lines=lines, firmware_config=firmware_config, feed_mm_per_min=feed_mm_per_min
        )
        self._gcode_tasks = {t for t in self._gcode_tasks if not t.done()}
        self._grid_repair_tasks = {t for t in self._grid_repair_tasks if not t.done()}
        self._soil_capture_tasks = {t for t in self._soil_capture_tasks if not t.done()}
        if (
            state["busy"]
            or self._soil_capture_lock.locked()
            or any(not task.done() for task in self._gcode_tasks)
            or any(not task.done() for task in self._grid_repair_tasks)
            or any(not task.done() for task in self._soil_capture_tasks)
        ):
            raise gcode_lib.GcodeError("FarmBot is busy")

        run_id = str(uuid.uuid4())
        extent = program.extent()
        self.gcode_runs[run_id] = {
            "run_id": run_id,
            "status": "queued",
            "message": "Raw G-code run queued",
            "moves": len(program.moves),
            "chunks_total": len(gcode_lib.lua_chunks(program.moves)),
            "chunks_sent": 0,
            "total_distance_mm": round(program.total_distance_mm, 1),
            "feed_mm_per_min": program.feed_mm_per_min,
            "start_position": {
                axis: round(value, 2) for axis, value in program.start_position.items()
            },
            "extent": {
                axis: [round(low, 1), round(high, 1)] for axis, (low, high) in extent.items()
            },
            "warnings": list(program.warnings),
            "created_at": dt_util.utcnow().isoformat(),
        }
        task = asyncio.create_task(
            self._run_gcode(
                run_id=run_id,
                program=program,
                original_position=state["position"] if return_to_start else None,
            ),
            name=f"farmbot-gcode-{run_id}",
        )
        self._gcode_tasks.add(task)
        task.add_done_callback(self._gcode_tasks.discard)
        return run_id

    async def _run_gcode(
        self,
        *,
        run_id: str,
        program: gcode_lib.GcodeProgram,
        original_position: dict[str, Any] | None,
    ) -> None:
        """Publish each Lua chunk in order, aborting on disconnect or e-stop."""
        record = self.gcode_runs[run_id]
        chunks = gcode_lib.lua_chunks(program.moves)
        final_status, final_message = "failed", "Raw G-code run failed"
        async with self._soil_capture_lock:
            try:
                record.update(status="running", message="Executing raw G-code")
                for index, chunk in enumerate(chunks, start=1):
                    state = self._live_connection_state()
                    if not state["connected"] or state["locked"]:
                        stop_reason = (
                            "FarmBot is emergency-stopped"
                            if state["locked"]
                            else "FarmBot lost its MQTT connection"
                        )
                        raise RuntimeError(
                            f"Stopped after {index - 1} of {len(chunks)} chunks: {stop_reason}"
                        )
                    await self.async_rpc_request(
                        [gcode_lib.lua_node(chunk)],
                        timeout=GCODE_CHUNK_RPC_TIMEOUT_SECONDS,
                    )
                    record["chunks_sent"] = index
                    record["message"] = f"Executed chunk {index} of {len(chunks)}"
                final_status = "complete"
                final_message = (
                    f"Executed {len(program.moves)} raw G-code move(s) over "
                    f"{program.total_distance_mm:.0f} mm"
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Raw G-code run %s failed: %s", run_id, err)
                final_message = str(err)[:240] or final_message
            finally:
                _LOGGER.info(
                    "Raw G-code run %s finished: %d/%d chunk(s) sent, %d move(s) planned",
                    run_id,
                    record["chunks_sent"],
                    len(chunks),
                    len(program.moves),
                )
                # Restoring position goes back through FarmBot OS's own planner
                # (safe_z and all), deliberately: whatever the raw program did,
                # the return trip should be the supervised kind of move.
                if original_position is not None and all(
                    original_position.get(axis) is not None for axis in ("x", "y", "z")
                ):
                    try:
                        await self.async_rpc_request(
                            [
                                self._move_command(
                                    **{
                                        axis: float(original_position[axis])
                                        for axis in ("x", "y", "z")
                                    },
                                    speed=100,
                                    safe_z=True,
                                )
                            ],
                            timeout=60,
                        )
                    except Exception as err:  # pylint: disable=broad-except
                        _LOGGER.warning(
                            "Could not restore position after G-code run %s: %s", run_id, err
                        )
                record.update(
                    status=final_status,
                    message=final_message,
                    completed_at=dt_util.utcnow().isoformat(),
                )

    def gcode_run(self, run_id: str) -> dict[str, Any] | None:
        return self.gcode_runs.get(run_id)

    # -------------------- Adaptive rotary-tool weeding --------------------

    @staticmethod
    def _lua_number(value: float) -> str:
        """Render a finite Lua number without exponent notation."""
        if not math.isfinite(float(value)):
            raise ValueError("weeding coordinates must be finite")
        rendered = f"{float(value):.6f}".rstrip("0").rstrip(".")
        return "0" if rendered in {"", "-0"} else rendered

    @classmethod
    def _weeding_lua(cls, weed: dict[str, Any], settings: dict[str, Any]) -> str:
        """Build the trusted adaptive Lua routine for one straight cut.

        Current is watched inside FarmBot OS, not polled through Home Assistant.
        The callback switches the rotary output off as soon as the configured
        load is exceeded. A cut overload reverses the next pass at half speed;
        another overload raises the working height. Contact while lowering
        raises the next attempt immediately.
        """
        number = cls._lua_number
        start = weed["start"]
        end = weed["end"]
        soil_z = float(weed["soil_z"])
        tool_height = float(settings["tool_height_mm"])
        safe_z = float(weed["travel_z"])
        motor_pin = int(settings["motor_pin"])
        current_pin = int(settings["current_pin"])
        max_load = float(settings["max_load"])
        attempts = int(settings["max_attempts"])
        cut_speed = int(settings["cut_speed_percent"])
        approach_speed = int(settings["approach_speed_percent"])
        height_step = float(settings["height_step_mm"])
        waypoint_moves = "\n".join(
            f"move({{x={number(point['x'])},y={number(point['y'])},z=safez,speed=approach}})"
            for point in weed.get("approach_waypoints", [])
        )
        return f"""local motor={motor_pin}; local current={current_pin}
local limit={number(max_load)}
local overloaded=false; local phase='idle'; local zoff={number(tool_height)}
local ax={number(start["x"])}; local ay={number(start["y"])}
local bx={number(end["x"])}; local by={number(end["y"])}
local safez={number(safe_z)}; local soilz={number(soil_z)}
local cutspd={cut_speed}; local approach={approach_speed}
watch_pin(current,function(data)
  if tonumber(data.value) and tonumber(data.value) > limit and not overloaded then
    overloaded=true; off(motor); toast('Rotary overload during '..phase,'warning')
  end
end)
move({{z=safez,speed=approach}})
{waypoint_moves}
move({{x=ax,y=ay,z=safez,speed=approach}})
local fromx=ax; local fromy=ay; local tox=bx; local toy=by
for attempt=1,{attempts} do
  overloaded=false; phase='lower'; on(motor)
  move({{x=fromx,y=fromy,z=soilz+zoff,speed=25}})
  if overloaded then
    zoff=zoff+{number(height_step)}
  else
    phase='cut'
    local speed=cutspd
    if attempt > 1 then speed=math.max(10,math.floor(cutspd/2)) end
    move({{x=tox,y=toy,z=soilz+zoff,speed=speed}})
    if not overloaded then
      off(motor); phase='done'; toast('Weed pass complete','success'); break
    end
    if attempt > 1 then zoff=zoff+{number(height_step)} end
  end
  off(motor); move({{z=safez,speed=approach}})
  local tx=fromx; local ty=fromy; fromx=tox; fromy=toy; tox=tx; toy=ty
  move({{x=fromx,y=fromy,z=safez,speed=approach}})
end
off(motor); phase='retract'; move({{z=safez,speed=approach}})
move({{x=bx,y=by,z=safez,speed=approach}})"""

    def plan_weeding(
        self,
        *,
        weeds: list[dict[str, Any]],
        settings: dict[str, Any],
        firmware_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate every cut and hardware limit before the first weed moves."""
        state = self.soil_motion_state(firmware_config)
        if not state["connected"]:
            raise ValueError("FarmBot is not connected")
        if state["locked"]:
            raise ValueError("FarmBot is emergency-stopped")
        attempts = int(settings["max_attempts"])
        if not 1 <= attempts <= WEEDING_MAX_ATTEMPTS:
            raise ValueError(f"max_attempts must be between 1 and {WEEDING_MAX_ATTEMPTS}")
        for pin_name in ("motor_pin", "current_pin"):
            if not 0 <= int(settings[pin_name]) <= 1000:
                raise ValueError(f"{pin_name} is outside the supported pin range")
        if int(settings["motor_pin"]) == int(settings["current_pin"]):
            raise ValueError("motor and current pins must be different")
        if not 1 <= int(settings["cut_speed_percent"]) <= 100:
            raise ValueError("cut speed must be between 1 and 100 percent")
        if not 1 <= int(settings["approach_speed_percent"]) <= 100:
            raise ValueError("approach speed must be between 1 and 100 percent")
        bounds = state["axis_bounds"]
        if settings.get("manage_tool"):
            for axis in ("x", "y", "z"):
                value = float(settings[f"tool_slot_{axis}"])
                axis_bounds = bounds.get(axis)
                if (
                    not math.isfinite(value)
                    or axis_bounds is None
                    or not axis_bounds[0] <= value <= axis_bounds[1]
                ):
                    raise ValueError(f"tool slot {axis.upper()} is outside FarmBot's axis bounds")
            direction = int(settings["tool_pullout_direction"])
            front_x = float(settings["tool_slot_x"]) + (
                100 if direction == 1 else -100 if direction == 2 else 0
            )
            front_y = float(settings["tool_slot_y"]) + (
                100 if direction == 3 else -100 if direction == 4 else 0
            )
            if not bounds["x"][0] <= front_x <= bounds["x"][1]:
                raise ValueError("tool pullout path leaves FarmBot's X axis bounds")
            if not bounds["y"][0] <= front_y <= bounds["y"][1]:
                raise ValueError("tool pullout path leaves FarmBot's Y axis bounds")
        for weed in weeds:
            start, end = weed["start"], weed["end"]
            if (
                math.hypot(float(end["x"]) - float(start["x"]), float(end["y"]) - float(start["y"]))
                > WEEDING_MAX_PATH_MM
            ):
                raise ValueError("a weeding path exceeds the integration safety limit")
            cut_z = float(weed["soil_z"]) + float(settings["tool_height_mm"])
            values = {
                "start X": (float(start["x"]), bounds.get("x")),
                "end X": (float(end["x"]), bounds.get("x")),
                "start Y": (float(start["y"]), bounds.get("y")),
                "end Y": (float(end["y"]), bounds.get("y")),
                "cut Z": (cut_z, bounds.get("z")),
                "travel Z": (float(weed["travel_z"]), bounds.get("z")),
            }
            for label, (value, axis_bounds) in values.items():
                if (
                    not math.isfinite(value)
                    or axis_bounds is None
                    or not axis_bounds[0] <= value <= axis_bounds[1]
                ):
                    raise ValueError(f"{label} is outside FarmBot's configured axis bounds")
            for waypoint in weed.get("approach_waypoints", []):
                for axis in ("x", "y"):
                    value = float(waypoint[axis])
                    axis_bounds = bounds.get(axis)
                    if (
                        not math.isfinite(value)
                        or axis_bounds is None
                        or not axis_bounds[0] <= value <= axis_bounds[1]
                    ):
                        raise ValueError(
                            f"approach waypoint {axis.upper()} is outside "
                            "FarmBot's configured axis bounds"
                        )
        return state

    @classmethod
    def _mount_tool_lua(cls, settings: dict[str, Any]) -> str:
        """Use FarmBot OS's standard helper, with a synthetic slot fallback."""
        if settings.get("tool_slot_from_bot"):
            target = json.dumps(str(settings["tool_name"]))
            setup = ""
        else:
            number = cls._lua_number
            if settings.get("tool_id"):
                tool_id = str(int(settings["tool_id"]))
                setup = ""
            else:
                tool_name = json.dumps(str(settings["tool_name"]))
                tool_id = "fbv_tool.id"
                setup = (
                    f"local fbv_tool=get_tool{{name={tool_name}}}; "
                    "if not fbv_tool then error('Rotary tool was not found') end; "
                )
            target = (
                "{pointer_type='ToolSlot',gantry_mounted=false,"
                f"tool_id={tool_id},"
                f"x={number(settings['tool_slot_x'])},"
                f"y={number(settings['tool_slot_y'])},"
                f"z={number(settings['tool_slot_z'])},"
                f"pullout_direction={int(settings['tool_pullout_direction'])}}}"
            )
        return (
            setup + "find_home(); "
            f"mount_tool({target}); "
            "if not verify_tool() then error('Rotary tool mounting was not verified') end"
        )

    @classmethod
    def _dismount_tool_lua(cls, settings: dict[str, Any]) -> str:
        """Use the standard helper when possible, or its documented slot motion."""
        if settings.get("tool_slot_from_bot"):
            return (
                "dismount_tool(); "
                "if verify_tool() then error('Rotary tool dismount was not verified') end; "
                "find_home()"
            )
        number = cls._lua_number
        x = float(settings["tool_slot_x"])
        y = float(settings["tool_slot_y"])
        z = float(settings["tool_slot_z"])
        direction = int(settings["tool_pullout_direction"])
        front = {
            1: (x + 100, y),
            2: (x - 100, y),
            3: (x, y + 100),
            4: (x, y - 100),
        }[direction]
        return f"""if not verify_tool() then error('No rotary tool is mounted') end
move({{z=safe_z()}})
move({{x={number(front[0])},y={number(front[1])}}})
move({{z={number(z)}}})
move_absolute({number(x)},{number(y)},{number(z)},50)
move({{z={number(z + 50)}}})
if read_pin(63)==0 then error('Rotary tool dismount was not verified') end
update_device({{mounted_tool_id=0}}); find_home()"""

    def start_weeding_run(
        self,
        *,
        weeds: list[dict[str, Any]],
        settings: dict[str, Any],
        firmware_config: dict[str, Any],
    ) -> str:
        state = self.plan_weeding(weeds=weeds, settings=settings, firmware_config=firmware_config)
        active_sets = (
            self._soil_capture_tasks,
            self._grid_repair_tasks,
            self._gcode_tasks,
            self._weeding_tasks,
        )
        for task_set in active_sets:
            task_set.intersection_update(task for task in task_set if not task.done())
        if state["busy"] or any(active_sets):
            raise ValueError("FarmBot is busy")
        run_id = str(uuid.uuid4())
        self.weeding_runs[run_id] = {
            "run_id": run_id,
            "status": "queued",
            "message": "Adaptive weeding queued",
            "weeds_total": len(weeds),
            "weeds_completed": 0,
            "weeds_failed": 0,
            "results": [],
            "created_at": dt_util.utcnow().isoformat(),
        }
        task = asyncio.create_task(
            self._run_weeding(run_id=run_id, weeds=weeds, settings=settings),
            name=f"farmbot-weeding-{run_id}",
        )
        self._weeding_tasks.add(task)
        task.add_done_callback(self._weeding_tasks.discard)
        return run_id

    async def _run_weeding(
        self, *, run_id: str, weeds: list[dict[str, Any]], settings: dict[str, Any]
    ) -> None:
        record = self.weeding_runs[run_id]
        async with self._soil_capture_lock:
            record.update(status="running", message="Starting adaptive rotary weeding")
            tool_mounted = False
            final_status, final_message = "failed", "Adaptive weeding failed"
            try:
                if settings.get("manage_tool"):
                    record["message"] = "Finding home and mounting the rotary tool"
                    await self.async_rpc_request(
                        [gcode_lib.lua_node(self._mount_tool_lua(settings))],
                        timeout=WEEDING_RPC_TIMEOUT_SECONDS,
                    )
                    tool_mounted = True
                for index, weed in enumerate(weeds, start=1):
                    state = self._live_connection_state()
                    if not state["connected"] or state["locked"]:
                        raise RuntimeError(
                            "FarmBot is emergency-stopped"
                            if state["locked"]
                            else "FarmBot lost its MQTT connection"
                        )
                    record["message"] = f"Mowing weed {index} of {len(weeds)}"
                    try:
                        await self.async_rpc_request(
                            [gcode_lib.lua_node(self._weeding_lua(weed, settings))],
                            timeout=WEEDING_RPC_TIMEOUT_SECONDS,
                        )
                        record["weeds_completed"] += 1
                        record["results"].append(
                            {"weed_id": int(weed["weed_id"]), "status": "attempted"}
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:  # continue with the next weed
                        message = str(err)[:160] or "weeding command failed"
                        record["weeds_failed"] += 1
                        record["results"].append(
                            {
                                "weed_id": int(weed["weed_id"]),
                                "status": "failed",
                                "message": message,
                            }
                        )
                        _LOGGER.warning(
                            "Weeding run %s weed %s failed: %s", run_id, weed["weed_id"], err
                        )
                final_status = "complete"
                final_message = (
                    f"Attempted {record['weeds_completed']} of {len(weeds)} weed(s); "
                    f"{record['weeds_failed']} failed"
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                final_message = str(err)[:240] or final_message
            finally:
                # Lua also switches the tool off. This separate supervised write
                # covers Lua/RPC failure before its cleanup statement executes.
                try:
                    await self.async_rpc_request(
                        [
                            {
                                "kind": "write_pin",
                                "args": {
                                    "pin_number": int(settings["motor_pin"]),
                                    "pin_value": 0,
                                    "pin_mode": 0,
                                },
                            }
                        ],
                        timeout=30,
                    )
                except Exception as err:  # pylint: disable=broad-except
                    _LOGGER.error(
                        "Could not confirm rotary tool off after weeding run %s: %s", run_id, err
                    )
                if tool_mounted:
                    record["message"] = "Returning the rotary tool to its slot"
                    try:
                        await self.async_rpc_request(
                            [gcode_lib.lua_node(self._dismount_tool_lua(settings))],
                            timeout=WEEDING_RPC_TIMEOUT_SECONDS,
                        )
                    except Exception as err:  # pylint: disable=broad-except
                        _LOGGER.error(
                            "Could not dismount rotary tool after run %s: %s", run_id, err
                        )
                        final_status = "failed"
                        final_message = f"Weeding finished, but tool dismount failed: {err}"[:240]
                record.update(
                    status=final_status,
                    message=final_message,
                    completed_at=dt_util.utcnow().isoformat(),
                )

    def weeding_run(self, run_id: str) -> dict[str, Any] | None:
        return self.weeding_runs.get(run_id)

    def _reported_position(self) -> dict[str, float] | None:
        position = (self.status or {}).get("location_data", {}).get("position") or {}
        try:
            normalized = {axis: float(position[axis]) for axis in ("x", "y", "z")}
        except (KeyError, TypeError, ValueError):
            return None
        return normalized if all(math.isfinite(value) for value in normalized.values()) else None

    async def _wait_for_grid_position(
        self,
        *,
        target: dict[str, float],
        timeout: float,
        tolerance_mm: float = GRID_REPAIR_POSITION_TOLERANCE_MM,
    ) -> dict[str, float] | None:
        """Confirm FarmBot's live status reached a target before photography."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            previous_revision = self._status_revision
            try:
                # read_status is also out-of-band: the RPC acknowledgement
                # triggers a fresh status broadcast which updates self.status.
                await self.async_rpc_request(
                    [{"kind": "read_status", "args": {}}],
                    timeout=min(10, remaining),
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.debug("Could not refresh FarmBot position during grid repair: %s", err)
            while (
                self._status_revision <= previous_revision
                and (remaining := deadline - asyncio.get_running_loop().time()) > 0
            ):
                await asyncio.sleep(min(0.1, remaining))
            if self._status_revision <= previous_revision:
                return None
            position = self._reported_position()
            if position is not None:
                distance = math.sqrt(
                    sum((position[axis] - target[axis]) ** 2 for axis in ("x", "y", "z"))
                )
                if distance <= tolerance_mm:
                    return position
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(1, remaining))

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
