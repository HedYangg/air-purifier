import argparse
import json
import logging
import os
import signal
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from dotenv import load_dotenv

from .bridge import Bridge
from .camera import run_camera
from .preview import WebPreview

LOG = logging.getLogger(__name__)


def climate_worker(stop, publish):
    import adafruit_dht
    import board

    pin = getattr(board, f"D{int(os.environ.get('DHT_GPIO', '17'))}")
    sensor = adafruit_dht.DHT11(pin, use_pulseio=False)
    try:
        while not stop.is_set():
            try:
                humidity = sensor.humidity
                temperature = sensor.temperature
                if humidity is not None and temperature is not None:
                    publish("air/gateway/climate", json.dumps({
                        "temperature": temperature, "humidity": humidity,
                    }, allow_nan=False))
            except RuntimeError as exc:
                LOG.warning("KY-015 read failed: %s", exc)
            stop.wait(3)
    finally:
        sensor.exit()


def retry_worker(stop, callback):
    while not stop.is_set():
        try:
            callback()
            return
        except Exception:
            LOG.exception("Sensor worker failed. Retrying in five seconds.")
            stop.wait(5)


def main():
    parser = argparse.ArgumentParser(description="Pi 5 air purifier gateway")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="Show camera landmarks on the Pi display")
    mode.add_argument("--no-camera", action="store_true", help="Start monitoring without gestures")
    mode.add_argument("--evaluate", action="store_true", help="Capture evaluation trials with MQTT disabled")
    mode.add_argument("--web-preview", action="store_true", help="Serve a browser preview on port 8080")
    parser.add_argument("--camera-only", action="store_true", help="Test the browser preview without MQTT or KY-015")
    parser.add_argument("--web-host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1",
                        help="Bind the preview locally or to a trusted LAN")
    args = parser.parse_args()
    if (args.camera_only or args.web_host != "127.0.0.1") and not args.web_preview:
        parser.error("--camera-only and --web-host require --web-preview")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if args.evaluate:
        LOG.info("Label trials with o=open palm, f=fist, n=no hand, x=other. q=finish.")
        run_camera(stop, preview=True, evaluate=True)
        return

    if args.camera_only:
        with WebPreview(args.web_host, camera_only=True) as web:
            run_camera(stop, publish_frame=web.publish)
        return
    run_gateway(stop, args)


def run_gateway(stop, args):
    token = os.environ.get("BLYNK_TOKEN", "")
    cloud_host = os.environ.get("BLYNK_HOST", "sgp1.blynk.cloud").lower()
    if token and (not cloud_host.endswith(".blynk.cloud") or "/" in cloud_host):
        raise SystemExit("Set BLYNK_HOST to your regional *.blynk.cloud hostname")
    bridge = Bridge(
        local_host=os.environ.get("MQTT_HOST", "127.0.0.1"),
        user=os.environ.get("MQTT_USER", "air-gateway"),
        password=os.environ.get("MQTT_PASSWORD", ""),
        cloud_host=cloud_host, token=token,
        template_id=os.environ.get("BLYNK_TEMPLATE_ID", ""),
    )
    workers = [
        threading.Thread(target=retry_worker, args=(stop, lambda: climate_worker(stop, bridge.publish_local)),
                         name="ky015", daemon=True),
    ]

    def cloud_loop():
        while not stop.is_set():
            bridge.push_cloud()
            stop.wait(5)

    def read_status():
        mqtt_connected = bridge.local.is_connected()
        blynk_status = "Disabled"
        if bridge.cloud is not None:
            blynk_status = "Connected" if bridge.cloud.is_connected() else "Disconnected"
        return {
            "mqtt": "Connected" if mqtt_connected else "Disconnected",
            "blynk": blynk_status,
            "readings": bridge.batch(time.monotonic()) if mqtt_connected else {},
        }

    workers.append(threading.Thread(target=cloud_loop, name="blynk", daemon=True))
    preview = WebPreview(args.web_host, read_status=read_status) if args.web_preview else nullcontext()
    with preview as web:
        bridge.start()
        for worker in workers:
            worker.start()
        try:
            if args.no_camera:
                stop.wait()
            else:
                retry_worker(stop, lambda: run_camera(
                    stop, bridge.publish_local, args.preview, publish_frame=web.publish if web else None))
        finally:
            stop.set()
            for worker in workers:
                worker.join(timeout=2)
            bridge.close()


if __name__ == "__main__":
    main()
