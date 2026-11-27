import csv
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from .hand import CONNECTIONS, FIST, NO_HAND, OPEN_PALM, OTHER, HoldGate, classify

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "models" / "hand_landmarker.task"


def run_camera(stop, publish=None, preview=False, evaluate=False, publish_frame=None):
    import cv2
    import mediapipe as mp
    from picamera2 import Picamera2

    options = mp.tasks.vision.HandLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(MODEL)),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_hands=2,
        min_hand_detection_confidence=0.65,
        min_hand_presence_confidence=0.65,
        min_tracking_confidence=0.60,
    )
    if evaluate:
        results = ROOT / "evaluation" / datetime.now().strftime("%Y%m%d-%H%M%S")
        results.mkdir(parents=True)
        labels = {ord("o"): OPEN_PALM, ord("f"): FIST, ord("n"): NO_HAND, ord("x"): OTHER}
    frames = 0
    attempts = 0
    correct = 0
    gate = HoldGate()
    last_ms = -1
    last_telemetry = 0
    next_web_frame = 0
    last_command = "Waiting"

    with mp.tasks.vision.HandLandmarker.create_from_options(options) as landmarker:
        camera = Picamera2(camera_num=int(os.environ.get("CAMERA_INDEX", "0")))
        try:
            # RGB888 yields a BGR array in Picamera2. MediaPipe needs RGB.
            camera.configure(camera.create_video_configuration(
                main={"size": (640, 480), "format": "RGB888"},
                controls={"FrameRate": 25},
            ))
            camera.start()
            started = time.monotonic()
            while not stop.is_set():
                bgr = camera.capture_array()
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                now = time.monotonic()
                last_ms = max(last_ms + 1, int((now - started) * 1000))
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = landmarker.detect_for_video(image, last_ms)
                hands = result.hand_world_landmarks
                label = NO_HAND if len(hands) == 0 else OTHER
                if len(hands) == 1:
                    label = classify(hands[0])
                command = gate.update(label, time.monotonic())
                if command:
                    last_command = command
                frames += 1
                fps = frames / max(time.monotonic() - started, 0.001)

                if publish:
                    if now - last_telemetry >= 1 or command:
                        publish("air/camera/gesture", label)
                        last_telemetry = now
                    if command:
                        sent = publish("air/camera/command", command)
                        LOG.info("Gesture %s requested %s (local publish queued: %s)", label, command, sent)

                web_frame_due = publish_frame is not None and now >= next_web_frame
                if not (preview or web_frame_due):
                    continue
                height, width = bgr.shape[:2]
                for hand in result.hand_landmarks:
                    points = [(int(p.x * width), int(p.y * height)) for p in hand]
                    for a, b in CONNECTIONS:
                        cv2.line(bgr, points[a], points[b], (90, 230, 130), 2)
                    for point in points:
                        cv2.circle(bgr, point, 3, (255, 255, 255), -1)
                cv2.putText(bgr, f"{label} | {fps:.1f} FPS", (12, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                cv2.putText(bgr, f"Last request: {last_command}", (12, height - 15),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2)
                if web_frame_due:
                    next_web_frame = now + 0.1
                    ready, jpeg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 78])
                    if ready:
                        publish_frame(jpeg.tobytes())
                if not preview:
                    continue
                cv2.imshow("Air purifier - hand landmarks", bgr)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    return
                if evaluate and key in labels:
                    expected = labels[key]
                    attempts += 1
                    correct += int(expected == label)
                    sample = f"{attempts:03d}_{'success' if expected == label else 'failure'}.jpg"
                    cv2.imwrite(str(results / sample), bgr)
                    with (results / "trials.csv").open("a", newline="", encoding="utf-8") as stream:
                        writer = csv.writer(stream)
                        if attempts == 1:
                            writer.writerow(["image", "expected", "detected", "fps"])
                        writer.writerow([sample, expected, label, round(fps, 2)])
        finally:
            camera.close()
            if preview:
                cv2.destroyAllWindows()
            if evaluate and attempts:
                LOG.info("Accuracy %.1f%% (%d/%d), %.1f processed FPS. Saved to %s",
                         correct / attempts * 100, correct, attempts, fps, results)
