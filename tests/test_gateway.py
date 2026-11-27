import json
import unittest
from http.client import HTTPConnection
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from gateway.bridge import Bridge, decode_telemetry
from gateway.hand import FIST, NO_HAND, OPEN_PALM, OTHER, HoldGate, classify
from gateway.history import metrics_text, query_history
from gateway.preview import WebPreview


class GatewayTests(unittest.TestCase):
    def test_gesture_uses_world_landmarks(self):
        def hand(open_palm):
            points = [SimpleNamespace(x=0, y=0, z=0) for _ in range(21)]
            points[4] = SimpleNamespace(x=-0.6, y=0.6, z=0)
            for base, x in zip((5, 9, 13, 17), (-0.2, 0, 0.2, 0.4)):
                points[base] = SimpleNamespace(x=x, y=1, z=0)
                points[base + 1] = SimpleNamespace(x=x, y=1.4, z=0)
                points[base + 3] = SimpleNamespace(x=x, y=2 if open_palm else 0.9,
                                                   z=0 if open_palm else 0.3)
            return points

        self.assertEqual(classify(hand(True)), OPEN_PALM)
        self.assertEqual(classify(hand(False)), FIST)

    def test_direct_switch_in_both_directions_after_three_seconds(self):
        gate = HoldGate()
        for label, start, command in (
            (OPEN_PALM, 0, "ON"), (FIST, 4, "OFF"), (OPEN_PALM, 8, "ON"),
        ):
            for frame in range(12):
                self.assertIsNone(gate.update(label, start + frame / 4))
            self.assertEqual(gate.update(label, start + 3), command)
            for frame in (13, 14, 15):
                self.assertIsNone(gate.update(label, start + frame / 4))

    def test_uncertain_frame_or_camera_gap_restarts_hold(self):
        gate = HoldGate()
        for frame in range(12):
            self.assertIsNone(gate.update(FIST, frame / 4))
        self.assertIsNone(gate.update(OTHER, 3))
        for frame in range(12):
            self.assertIsNone(gate.update(FIST, 3.25 + frame / 4))
        for frame in range(12):
            self.assertIsNone(gate.update(FIST, 7 + frame / 4))
        self.assertEqual(gate.update(FIST, 10), "OFF")

    def test_hand_release_allows_same_command_again(self):
        gate = HoldGate()
        for frame in range(12):
            self.assertIsNone(gate.update(OPEN_PALM, frame / 4))
        self.assertEqual(gate.update(OPEN_PALM, 3), "ON")
        for now in (3.25, 3.5, 3.75):
            self.assertIsNone(gate.update(NO_HAND, now))
        for frame in range(12):
            self.assertIsNone(gate.update(OPEN_PALM, 4 + frame / 4))
        self.assertEqual(gate.update(OPEN_PALM, 7), "ON")

    def test_telemetry_freshness_and_blynk_types(self):
        bridge = Bridge("localhost", "air-gateway", "", "", "", "")
        state = decode_telemetry("air/node/state", '{"fan":1,"locked":0,"imu_ok":1,'
            '"moving":0,"tilt":2.5,"manual_stop":0,"relay_enabled":1}')
        self.assertEqual(state["SideTiltDegrees"], -1)
        self.assertEqual(state["MotionLock"], 0)
        climate = decode_telemetry("air/gateway/climate", '{"temperature":30,"humidity":63}')
        bridge.fields = {key: (value, 100) for key, value in (state | climate).items()}
        self.assertEqual(bridge.batch(105)["FanOn"], 1)
        self.assertEqual(bridge.batch(105)["Temperature"], 30.0)
        self.assertEqual(bridge.batch(113)["FanOn"], -1)
        self.assertEqual(bridge.batch(113)["ClimateOnline"], 0)

    def test_node_reports_two_angles_and_a_settling_motion_lock(self):
        bridge = Bridge("localhost", "air-gateway", "", "", "", "")
        bridge.cloud = Mock()
        bridge.cloud.is_connected.return_value = True
        message = SimpleNamespace(topic="air/node/state", payload=(
            b'{"fan":0,"locked":1,"imu_ok":1,"moving":0,"motion_lock":1,'
            b'"tilt":90.2,"side_tilt":95.6,"manual_stop":0,"relay_enabled":1}'))
        with patch("gateway.bridge.time.monotonic", return_value=100):
            bridge._on_message(bridge.local, None, message)
            bridge.push_cloud()
        readings = bridge.batch(100)
        self.assertEqual(readings["TiltDegrees"], 90.2)
        self.assertEqual(readings["SideTiltDegrees"], 95.6)
        self.assertEqual(readings["Moving"], 0)
        self.assertEqual(readings["MotionLock"], 1)
        cloud_readings = json.loads(bridge.cloud.publish.call_args.args[1])
        self.assertEqual(cloud_readings["TiltDegrees"], 90.2)
        self.assertNotIn("SideTiltDegrees", cloud_readings)
        self.assertNotIn("MotionLock", cloud_readings)
        self.assertEqual(bridge.batch(113)["SideTiltDegrees"], -1)
        self.assertEqual(bridge.batch(113)["MotionLock"], -1)

    def test_preview_exposes_current_readings_without_blynk(self):
        status = {"mqtt": "Connected", "blynk": "Disabled", "readings": {
            "Temperature": 28.2, "Humidity": 65.0, "PM25": -1, "GasADCmV": 1300, "NodeOnline": 1,
        }}
        with WebPreview(port=0, read_status=lambda: status) as preview:
            conn = HTTPConnection("127.0.0.1", preview.server.server_address[1], timeout=3)
            try:
                conn.request("GET", "/")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                page = response.read()
                self.assertIn(b"Live sensor readings", page)
                self.assertIn(b"Gas response (MQ-135)", page)
                self.assertIn(b'<option value="GasADCmV">Gas response (mV)</option>', page)
                self.assertIn(b'id="side-tilt"', page)
                self.assertIn(b'<option value="SideTiltDegrees">', page)
                self.assertNotIn(b"CO2EstimatePpm", page)
                conn.request("GET", "/api/stats")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(json.loads(response.read()), status)
            finally:
                conn.close()

    def test_mq135_publishes_raw_millivolts_to_blynk(self):
        bridge = Bridge("localhost", "air-gateway", "", "", "", "")
        bridge.fields["NodeOnline"] = (1, 100)
        bridge.cloud = Mock()
        bridge.cloud.is_connected.return_value = True
        message = SimpleNamespace(topic="air/node/gas_mv", payload=b"1500")
        with patch("gateway.bridge.time.monotonic", return_value=100):
            bridge._on_message(bridge.local, None, message)
            bridge.push_cloud()
        self.assertEqual(bridge.cloud.publish.call_count, 1)
        topic, payload = bridge.cloud.publish.call_args.args
        self.assertEqual(topic, "batch_ds")
        self.assertEqual(json.loads(payload), {
            "GasADCmV": 1500, "NodeOnline": 1, "ClimateOnline": 0, "Gesture": "UNAVAILABLE",
        })
        self.assertEqual(bridge.batch(113)["GasADCmV"], -1)

    def test_metrics_export_preserves_negative_temperature_and_omits_missing_sentinels(self):
        text = metrics_text({"Temperature": -1, "Humidity": 65.2, "PM25": -1,
                             "GasADCmV": 1300, "CO2EstimatePpm": 750, "SideTiltDegrees": 95.6,
                             "NodeOnline": 0, "BLYNK_TOKEN": "private"})
        self.assertIn('sensor="Temperature"} -1', text)
        self.assertIn('sensor="Humidity"} 65.2', text)
        self.assertIn('sensor="GasADCmV"} 1300', text)
        self.assertIn('sensor="SideTiltDegrees"} 95.6', text)
        self.assertIn('sensor="NodeOnline"} 0', text)
        self.assertNotIn("PM25", text)
        self.assertNotIn("CO2EstimatePpm", text)
        self.assertNotIn("private", text)

    def test_history_queries_prometheus_and_keeps_series_separate(self):
        payload = {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"sensor": "Temperature"}, "values": [[990, "28.1"], [995, "NaN"]]},
            {"metric": {"sensor": "PM25"}, "values": [[995, "12"], [1000, "25"]]},
            {"metric": {"sensor": "GasADCmV"}, "values": [[995, "1300"], [1000, "1400"]]},
            {"metric": {"sensor": "SideTiltDegrees"}, "values": [[995, "95.6"]]},
            {"metric": {"sensor": "CO2EstimatePpm"}, "values": [[995, "750"]]},
        ]}}
        with patch("gateway.history.time.time", return_value=1000), patch(
                "gateway.history.urlopen", return_value=BytesIO(json.dumps(payload).encode())) as request:
            result = query_history("15m")
        url = urlsplit(request.call_args.args[0])
        self.assertEqual(url.path, "/api/v1/query_range")
        query = parse_qs(url.query)["query"][0]
        self.assertIn('job="air-purifier"', query)
        self.assertIn("GasADCmV", query)
        self.assertNotIn("CO2EstimatePpm", query)
        self.assertEqual(result["end"] - result["start"], 900)
        self.assertEqual(result["series"]["Temperature"], [[990, 28.1]])
        self.assertEqual(result["series"]["PM25"], [[995, 12.0], [1000, 25.0]])
        self.assertEqual(result["series"]["GasADCmV"], [[995, 1300.0], [1000, 1400.0]])
        self.assertEqual(result["series"]["SideTiltDegrees"], [[995, 95.6]])
        self.assertNotIn("CO2EstimatePpm", result["series"])
        self.assertEqual(result["series"]["Humidity"], [])
