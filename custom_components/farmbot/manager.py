import json
import logging
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
    DEFAULT_ALLOW_AUTOMATIC_RADIUS_INCREASES,
    DEFAULT_ALLOW_VISION_CURVE_WRITES,
    DEFAULT_MAXIMUM_PLANT_RADIUS_MM,
    DEFAULT_MINIMUM_AUTOMATIC_CONFIDENCE,
    DEFAULT_VISION_ENABLED,
    DEFAULT_VISION_HEARTBEAT_TIMEOUT_MINUTES,
    EVENT_VISION_REQUEST,
    MQTT_PORT,
    OPTION_ALLOW_AUTOMATIC_RADIUS_INCREASES,
    OPTION_ALLOW_VISION_CURVE_WRITES,
    OPTION_MAXIMUM_PLANT_RADIUS_MM,
    OPTION_MINIMUM_AUTOMATIC_CONFIDENCE,
    OPTION_VISION_ENABLED,
    OPTION_VISION_HEARTBEAT_TIMEOUT_MINUTES,
    SIGNAL_SEQUENCE_SELECTED,
    SIGNAL_STATE,
    SIGNAL_VISION_STATE,
    TOKEN_REFRESH_WINDOW,
    TOPIC_COMMAND,
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
    for scheme in ("mqtts://", "mqtt://", "amqps://", "amqp://", "ssl://", "tcp://", "wss://", "ws://"):
        if host.lower().startswith(scheme):
            host = host[len(scheme):]
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
        self.token = str(token).strip()                 # encoded JWT
        self.device_id = str(device_id).strip()         # 'device_<id>' or numeric
        self.mqtt_host_raw = str(mqtt_host).strip()     # must come from token.unencoded.mqtt
        self.status: dict = {}
        self.device_name = f"FarmBot {self.device_id}"
        self.entry_id: Optional[str] = (
            getattr(entry, "entry_id", None) if entry is not None else None
        )
        self._mqtt: Optional[mqtt.Client] = None
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
            _LOGGER.info("Token expires in %s seconds (< %s window), will refresh",
                        time_until_expiry, TOKEN_REFRESH_WINDOW)
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
            host, port, username, _mask(self.token, 8, 8),
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
            _LOGGER.info("MQTT disconnected for %s", self.device_id)

    async def disconnect_mqtt(self):
        await self.hass.async_add_executor_job(self._disconnect_mqtt_blocking)

    # -------------------- MQTT callbacks --------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        # reason_code is a paho ReasonCode: compare by name/int, not by the old
        # MQTTv3.1.1 rc values (e.g. "bad auth" is numeric 4 pre-migration,
        # 134 as a ReasonCode) - see paho.mqtt.reasoncodes for the mapping.
        if reason_code == 0:
            client.subscribe(TOPIC_STATUS.format(device_id=self.device_id))
            client.subscribe(TOPIC_LOGS.format(device_id=self.device_id))
            _LOGGER.info("MQTT connected and subscribed for %s", self.device_id)
            # Reset auth failure flag on successful connection
            self._auth_failed = False
        elif reason_code == "Bad user name or password":
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
                self.hass.loop.call_soon_threadsafe(
                    self._entry.async_start_reauth, self.hass
                )
        else:
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
        else:
            _LOGGER.debug("Unhandled topic %s", msg.topic)

    # -------------------- Command helpers --------------------
    def _publish_rpc(self, rpc: dict):
        assert self._mqtt is not None, "MQTT client not connected"
        topic = TOPIC_COMMAND.format(device_id=self.device_id)
        _LOGGER.debug("Publishing RPC to %s: %s", topic, rpc)
        self._mqtt.publish(topic, json.dumps(rpc))

    def send_rpc_request(self, commands: list, priority: int = 600, label: str | None = None):
        if label is None:
            label = f"ha-{uuid.uuid4()}"
        rpc = {
            "kind": "rpc_request",
            "args": {"label": label, "priority": priority},
            "body": commands,
        }
        self._publish_rpc(rpc)

    def send_write_pin(self, pin: int, value: int):
        cs = [{
            "kind": "write_pin",
            "args": {"pin_number": int(pin), "pin_value": int(value), "pin_mode": 0},
        }]
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
                self.hass.loop.call_soon_threadsafe(
                    self._entry.async_start_reauth, self.hass
                )
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
            OPTION_ALLOW_AUTOMATIC_RADIUS_INCREASES: options.get(
                OPTION_ALLOW_AUTOMATIC_RADIUS_INCREASES,
                DEFAULT_ALLOW_AUTOMATIC_RADIUS_INCREASES,
            ),
            OPTION_ALLOW_VISION_CURVE_WRITES: options.get(
                OPTION_ALLOW_VISION_CURVE_WRITES, DEFAULT_ALLOW_VISION_CURVE_WRITES
            ),
            OPTION_MAXIMUM_PLANT_RADIUS_MM: options.get(
                OPTION_MAXIMUM_PLANT_RADIUS_MM, DEFAULT_MAXIMUM_PLANT_RADIUS_MM
            ),
            OPTION_MINIMUM_AUTOMATIC_CONFIDENCE: options.get(
                OPTION_MINIMUM_AUTOMATIC_CONFIDENCE, DEFAULT_MINIMUM_AUTOMATIC_CONFIDENCE
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
            status, job_id, last_completed_at, plants_analysed, recommendations,
            automatically_applied, uncertain, message, app_version,
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
        return None
