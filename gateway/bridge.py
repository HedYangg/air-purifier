import json
import logging
import math
import ssl
import threading
import time

import paho.mqtt.client as mqtt

LOG = logging.getLogger(__name__)

NODE_STATE = {
    "fan": "FanOn",
    "locked": "SafetyLock",
    "imu_ok": "ImuOK",
    "moving": "Moving",
    "manual_stop": "ManualStop",
    "relay_enabled": "RelayEnabled",
}
NODE_FIELDS = (*NODE_STATE.values(), "MotionLock", "TiltDegrees", "SideTiltDegrees", "PM25", "GasADCmV")


def decode_telemetry(topic, payload):
    if topic == "air/node/pm25":
        return {"PM25": max(-1, int(payload))}
    if topic == "air/node/gas_mv":
        return {"GasADCmV": int(payload)}
    if topic == "air/node/state":
        packet = json.loads(payload)
        tilt = float(packet["tilt"])
        side_tilt = float(packet.get("side_tilt", -1))
        if any(not math.isfinite(angle) or not -1 <= angle <= 180 for angle in (tilt, side_tilt)):
            raise ValueError("Invalid tilt angle")
        state = {name: int(packet[key]) for key, name in NODE_STATE.items()}
        state["MotionLock"] = int(packet.get("motion_lock", 0))
        if any(value not in (0, 1) for value in state.values()):
            raise ValueError("Invalid state flag")
        state.update(TiltDegrees=tilt, SideTiltDegrees=side_tilt, NodeOnline=1)
        return state
    if topic == "air/node/online" and payload == "0":
        return {"NodeOnline": 0}
    if topic == "air/gateway/climate":
        packet = json.loads(payload)
        temperature = float(packet["temperature"])
        humidity = float(packet["humidity"])
        if not -20 <= temperature <= 80 or not 0 <= humidity <= 100:
            raise ValueError("Invalid climate values")
        return {"Temperature": temperature, "Humidity": humidity}
    if topic == "air/camera/gesture" and payload in {"OPEN_PALM", "FIST", "OTHER", "NO_HAND"}:
        return {"Gesture": payload}
    return {}


class Bridge:
    def __init__(self, local_host, user, password, cloud_host, token, template_id):
        self.host = local_host
        self.cloud_host = cloud_host
        self.fields = {}
        self.lock = threading.Lock()
        self.local = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="air-pi")
        self.local.username_pw_set(user, password)
        self.local.on_connect = self._local_connected
        self.local.on_message = self._on_message
        self.cloud = None
        if token:
            self.cloud = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="air-blynk-pi")
            self.cloud.username_pw_set("device", token)
            self.cloud.tls_set_context(ssl.create_default_context())
            self.cloud.on_connect = self._cloud_connected
            self.template_id = template_id

    def _local_connected(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            LOG.error("Mosquitto connection refused: %s", reason)
            return
        client.subscribe([("air/node/#", 0), ("air/gateway/climate", 0), ("air/camera/gesture", 0)])
        LOG.info("Connected to local Mosquitto")

    def _cloud_connected(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            LOG.error("Blynk connection refused: %s", reason)
            return
        if self.template_id:
            info = {"tmpl": self.template_id, "type": self.template_id, "ver": "1.0.0",
                    "build": "air-purifier"}
            client.publish("info/mcu", json.dumps(info), qos=0)
        LOG.info("Connected to Blynk over TLS")

    def _on_message(self, client, userdata, message):
        try:
            payload = message.payload.decode("utf-8")
            values = decode_telemetry(message.topic, payload)
        except (ValueError, TypeError, KeyError, OverflowError):
            LOG.warning("Invalid telemetry on %s", message.topic)
            return
        now = time.monotonic()
        with self.lock:
            if values.get("NodeOnline") == 0:
                for key in NODE_FIELDS:
                    self.fields.pop(key, None)
            self.fields.update({key: (value, now) for key, value in values.items()})

    def start(self):
        self.local.connect_async(self.host, 1883, keepalive=20)
        self.local.loop_start()
        if self.cloud:
            self.cloud.connect_async(self.cloud_host, 8883, keepalive=45)
            self.cloud.loop_start()
        else:
            LOG.info("BLYNK_TOKEN is empty. Local MQTT mode.")

    def close(self):
        for client in (self.cloud, self.local):
            if client is not None:
                client.disconnect()
                client.loop_stop()

    def publish_local(self, topic, value):
        if not self.local.is_connected():
            return False
        result = self.local.publish(topic, value, qos=0, retain=False)
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def batch(self, now):
        with self.lock:
            values = {key: value for key, (value, at) in self.fields.items() if now - at <= 12}
        values.setdefault("NodeOnline", 0)
        values.setdefault("ClimateOnline", int("Temperature" in values and "Humidity" in values))
        values.setdefault("Gesture", "UNAVAILABLE")
        if not values["NodeOnline"]:
            for key in NODE_FIELDS:
                values[key] = -1
        return values

    def push_cloud(self):
        if self.cloud and self.cloud.is_connected():
            values = self.batch(time.monotonic())
            values.pop("SideTiltDegrees", None)
            values.pop("MotionLock", None)
            payload = json.dumps(values, separators=(",", ":"), allow_nan=False)
            self.cloud.publish("batch_ds", payload, qos=0, retain=False)
