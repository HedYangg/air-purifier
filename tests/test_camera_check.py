import base64
import json
import time
import unittest
from http.client import HTTPConnection
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from gateway.camera_check import CameraCheckError, DEFAULT_MODEL, check_frame
from gateway.preview import WebPreview


class CameraCheckTests(unittest.TestCase):
    def test_gemini_receives_one_jpeg_and_returns_feedback(self):
        jpeg = b"\xff\xd8test-frame\xff\xd9"
        reply = {"candidates": [{"content": {"parts": [
            {"text": "Internal reasoning", "thought": True},
            {"text": "Move your hand toward the center."},
        ]}}]}
        with patch("gateway.camera_check.urlopen", return_value=BytesIO(json.dumps(reply).encode())) as send:
            result = check_frame(jpeg, api_key="test-key", model=DEFAULT_MODEL)
        request = send.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith(f"{DEFAULT_MODEL}:generateContent"))
        self.assertEqual(request.get_header("X-goog-api-key"), "test-key")
        self.assertNotIn("test-key", request.full_url)
        parts = json.loads(request.data)["contents"][0]["parts"]
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1]["inlineData"]["mimeType"], "image/jpeg")
        self.assertEqual(base64.b64decode(parts[1]["inlineData"]["data"]), jpeg)
        self.assertEqual(result["feedback"], "Move your hand toward the center.")
        self.assertEqual(result["model"], DEFAULT_MODEL)
        self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_gemini_quota_error_is_shown_without_raw_response_details(self):
        error = HTTPError("https://example.invalid", 429, "provider error details", {}, None)
        with patch("gateway.camera_check.urlopen", side_effect=error) as send:
            with self.assertRaisesRegex(CameraCheckError, "quota or rate limit"):
                check_frame(b"test-frame", api_key="test-key", model=DEFAULT_MODEL)
        send.assert_called_once()

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
                self.assertIn(b'id="telemetry" hidden', page)

                conn.request("POST", "/api/camera-check")
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                response.read()

                headers = {"X-Camera-Check": preview.check_token}
                preview.publish(b"annotated-frame")
                with preview.condition:
                    preview.snapshot = (b"stale-frame", time.monotonic() - 4)
                conn.request("POST", "/api/camera-check", headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 503)
                response.read()
                checker.assert_not_called()

                preview.publish_snapshot(b"raw-frame")
                conn.request("POST", "/api/camera-check", headers=headers)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), result)
                checker.assert_called_once_with(b"raw-frame")
            finally:
                conn.close()
