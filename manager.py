import json
import uuid
import logging
import ssl
from typing import Tuple, Optional
import requests
from .const import API_BASE_URL

import paho.mqtt.client as mqtt
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    MQTT_PORT,
    TOPIC_STATUS,
    TOPIC_COMMAND,
    TOPIC_LOGS,
    SIGNAL_STATE,
)

_LOGGER = logging.getLogger(__name__)

_RC_TEXT = {
    0: "Connection accepted",
    1: "Unacceptable protocol version",
    2: "Identifier rejected",
    3: "Server unavailable",
    4: "Bad username or password",
    5: "Not authorized",
}

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

    def __init__(self, hass, token: str, device_id: str, mqtt_host: str):
        self.hass = hass
        self.token = str(token).strip()                 # encoded JWT
        self.device_id = str(device_id).strip()         # 'device_<id>' or numeric
        self.mqtt_host_raw = str(mqtt_host).strip()     # must come from token.unencoded.mqtt
        self.status: dict = {}
        self.device_name = f"FarmBot {self.device_id}"
        self._mqtt: Optional[mqtt.Client] = None
        # Do not connect here; async_setup_entry will await connect_mqtt()

    # -------------------- Connection (run in executor) --------------------
    def _connect_mqtt_blocking(self):
        """(Blocking) Initialize MQTT client with proper TLS and credentials."""
        username = _normalize_username(self.device_id)
        host, port = _split_host_port(self.mqtt_host_raw, MQTT_PORT)

        client_id = f"ha-{username}-{uuid.uuid4().hex[:8]}"
        self._mqtt = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

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
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(TOPIC_STATUS.format(device_id=self.device_id))
            client.subscribe(TOPIC_LOGS.format(device_id=self.device_id))
            _LOGGER.info("MQTT connected and subscribed for %s", self.device_id)
        else:
            _LOGGER.error("MQTT connect failed: rc=%s (%s)", rc, _RC_TEXT.get(rc, "unknown"))

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

    def move_to(self, x=None, y=None, z=None, speed=100):
        args = {}
        if x is not None: args["x"] = float(x)
        if y is not None: args["y"] = float(y)
        if z is not None: args["z"] = float(z)
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
