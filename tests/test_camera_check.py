import base64
import json
import time
import unittest
from http.client import HTTPConnection
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from gateway.camera_check import CameraCheckError, DEFAULT_MODEL, check_frame, describe_observation
from gateway.hand import FIST, NO_HAND, OPEN_PALM, OTHER
from gateway.preview import WebPreview


class CameraCheckTests(unittest.TestCase):
    def test_gemini_receives_one_jpeg_and_returns_feedback(self):
        jpeg = b"\xff\xd8test-frame\xff\xd9"
        reply = {"candidates": [{"content": {"parts": [
            {"text": "Internal reasoning", "thought": True},
            {"text": "Move your hand toward the center."},
        ]}}]}
        with patch("gateway.camera_check.urlopen", return_value=BytesIO(json.dumps(reply).encode())) as send:
            result = check_frame(jpeg, target=OPEN_PALM, local_feedback="MediaPipe saw an unclear pose.",
                                 api_key="test-key", model=DEFAULT_MODEL)
        request = send.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith(f"{DEFAULT_MODEL}:generateContent"))
        self.assertEqual(request.get_header("X-goog-api-key"), "test-key")
        self.assertNotIn("test-key", request.full_url)
        parts = json.loads(request.data)["contents"][0]["parts"]
        self.assertEqual(len(parts), 2)
        self.assertIn("intended gesture is open palm", parts[0]["text"])
        self.assertIn("thumb extended", parts[0]["text"])
        self.assertIn("MediaPipe saw an unclear pose.", parts[0]["text"])
        self.assertEqual(parts[1]["inlineData"]["mimeType"], "image/jpeg")
        self.assertEqual(base64.b64decode(parts[1]["inlineData"]["data"]), jpeg)
        self.assertEqual(result["feedback"], "Move your hand toward the center.")
        self.assertEqual(result["model"], DEFAULT_MODEL)
        self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_gemini_quota_error_is_shown_without_raw_response_details(self):
        error = HTTPError("https://example.invalid", 429, "provider error details", {}, None)
        with patch("gateway.camera_check.urlopen", side_effect=error) as send:
            with self.assertRaisesRegex(CameraCheckError, "quota or rate limit"):
                check_frame(b"test-frame", target=FIST, local_feedback="MediaPipe saw no hand.",
                            api_key="test-key", model=DEFAULT_MODEL)
        send.assert_called_once()

    def test_local_feedback_distinguishes_hand_detection_pose_and_hold(self):
        cases = (
            (NO_HAND, 0, False, "did not detect a hand"),
            (OTHER, 2, False, "multiple hands"),
            (OTHER, 1, False, "could not classify"),
            (FIST, 1, False, "You selected open palm"),
            (OPEN_PALM, 1, False, "Hold it steady for 3 seconds"),
            (OPEN_PALM, 1, True, "generated its request"),
        )
        for label, hand_count, request_generated, expected in cases:
            with self.subTest(label=label, hand_count=hand_count, request_generated=request_generated):
                self.assertIn(expected, describe_observation(OPEN_PALM, {
                    "gesture": label, "hand_count": hand_count, "request_generated": request_generated,
                }))

    def test_preview_checks_only_fresh_raw_frames_on_button_request(self):
        result = {"feedback": "The entire hand is visible.", "model": DEFAULT_MODEL, "elapsed_ms": 320}
        checker = Mock(return_value=result)
        with WebPreview(port=0, camera_only=True, camera_check=checker) as preview:
            conn = HTTPConnection("127.0.0.1", preview.server.server_address[1], timeout=3)
            try:
                conn.request("GET", "/")
                response = conn.getresponse()
                page = response.read()
                self.assertEqual(response.status, 200)
                self.assertIn(b"AI Camera Check", page)
                self.assertIn(b'id="camera-check-target"', page)
                self.assertIn(b'id="telemetry" hidden', page)

                conn.request("POST", "/api/camera-check")
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                response.read()

                headers = {"X-Camera-Check": preview.check_token, "X-Gesture-Target": FIST}
                observation = {"gesture": OTHER, "hand_count": 1, "request_generated": False}

                conn.request("POST", "/api/camera-check", headers={**headers, "X-Gesture-Target": "UNSUPPORTED"})
                response = conn.getresponse()
                self.assertEqual(response.status, 400)
                response.read()

                preview.publish(b"annotated-frame")
                with preview.condition:
                    preview.snapshot = (b"stale-frame", time.monotonic() - 4, observation)
                conn.request("POST", "/api/camera-check", headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 503)
                response.read()
                checker.assert_not_called()

                preview.publish_snapshot(b"raw-frame", observation)
                conn.request("POST", "/api/camera-check", headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                local = "MediaPipe detected one hand but could not classify its pose."
                self.assertEqual(json.loads(response.read()), {**result, "local_feedback": local})
                checker.assert_called_once_with(b"raw-frame", target=FIST, local_feedback=local)

                checker.side_effect = CameraCheckError("Gemini unavailable.")
                conn.request("POST", "/api/camera-check", headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 502)
                self.assertEqual(json.loads(response.read()), {"error": "Gemini unavailable.", "local_feedback": local})
            finally:
                conn.close()
