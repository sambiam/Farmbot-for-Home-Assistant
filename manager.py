import json
import threading
import uuid
import logging
import requests
import base64
import paho.mqtt.client as mqtt

from homeassistant.helpers.dispatcher import dispatcher_send
from .const import (
    API_BASE_URL,
    MQTT_PORT,
    TOPIC_STATUS,
    TOPIC_COMMAND,
    TOPIC_LOGS,
    SIGNAL_STATE,
)

_LOGGER = logging.getLogger(__name__)

class FarmbotManager:
    def __init__(self, hass, token, device_id, mqtt_host):
        self.hass = hass
        self.token = token
        self.device_id = device_id
        self.mqtt_host = mqtt_host
        self.status = {}

    def connect_mqtt(self):
        """Initialize MQTT client with TLS and credentials."""
        self._mqtt = mqtt.Client()
        self._mqtt.tls_set()
        # decode JWT to get bot claim for username
        try:
            _, payload_b64, _ = self.token.split(".")
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            bot_id = str(payload.get("bot"))
            _LOGGER.debug("Using MQTT username = bot claim %s", bot_id)
        except Exception:
            bot_id = None
            _LOGGER.debug("Could not decode bot claim; using token as password")

        if bot_id:
            self._mqtt.username_pw_set(username=bot_id, password=self.token)
        else:
            self._mqtt.username_pw_set(username="", password=self.token)

        self._mqtt.on_connect = self._on_mqtt_connect
        self._mqtt.on_message = self._on_mqtt_message
        self._mqtt.connect(self.mqtt_host, MQTT_PORT)
        self._mqtt.loop_start()

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        _LOGGER.debug("MQTT connected (rc=%s)", rc)
        self._mqtt.subscribe(TOPIC_STATUS.format(device_id=self.device_id))
        self._mqtt.subscribe(TOPIC_LOGS.format(device_id=self.device_id))

    def _on_mqtt_message(self, client, userdata, msg):
        payload = json.loads(msg.payload.decode())
        if msg.topic == TOPIC_STATUS.format(device_id=self.device_id):
            state = payload.get("body", payload)
            self.status = state
            self.hass.loop.call_soon_threadsafe(
                dispatcher_send, self.hass, SIGNAL_STATE, state
            )

    def send_rpc_request(self, body, priority=600):
        topic = TOPIC_COMMAND.format(device_id=self.device_id)
        kind = body[0].get("kind")
        _LOGGER.debug("Publishing RPC kind=%s to %s", kind, topic)
        rpc = {
            "kind": "rpc_request",
            "args": {"label": str(uuid.uuid4()), "priority": priority},
            "body": body,
        }
        self._mqtt.publish(topic, json.dumps(rpc))

    def send_write_pin(self, pin, value):
        _LOGGER.debug("Writing pin %s → %s", pin, value)
        cs = [{
            "kind": "write_pin",
            "args": {"pin_number": pin, "pin_value": value, "pin_mode": 0},
        }]
        self.send_rpc_request(cs)

    def send_toggle_pin(self, pin):
        _LOGGER.debug("Toggling pin %s", pin)
        cs = [{"kind": "toggle_pin", "args": {"pin_number": pin}}]
        self.send_rpc_request(cs)

    def disconnect_mqtt(self):
        """Cleanly stop MQTT loop and disconnect."""
        if hasattr(self, "_mqtt"):
            _LOGGER.debug("Disconnecting MQTT client")
            self._mqtt.loop_stop()
            self._mqtt.disconnect()

    def fetch_peripherals(self):
        """Fetch all peripherals (pin + label) via the FarmBot HTTP API."""
        url = f"{API_BASE_URL}/peripherals"
        headers = {"Authorization": f"Bearer {self.token}"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        peripherals = []
        if isinstance(data, dict) and "data" in data:
            entries = data["data"]
        elif isinstance(data, list):
            entries = data
        else:
            entries = data.get("peripherals", [])

        for p in entries:
            if isinstance(p, dict) and "attributes" in p:
                attrs = p["attributes"]
                pin   = attrs.get("pin")
                label = attrs.get("label") or f"Peripheral {pin}"
            else:
                pin = p.get("pin")
                label = p.get("label", f"Peripheral {pin}")
            peripherals.append({"pin": pin, "label": label})

        return peripherals

    def fetch_sequences(self):
        """Fetch all sequences (id + name) via the FarmBot HTTP API."""
        url = f"{API_BASE_URL}/sequences"
        headers = {"Authorization": f"Bearer {self.token}"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict) and "data" in data:
            entries = data["data"]
        elif isinstance(data, list):
            entries = data
        else:
            entries = data.get("sequences", [])

        sequences = []
        for item in entries:
            if isinstance(item, dict) and "attributes" in item:
                seq_id = int(item.get("id"))
                name   = item["attributes"].get("name")
            else:
                seq_id = int(item.get("id"))
                name   = item.get("name")
            sequences.append({"id": seq_id, "name": name})

        return sequences
