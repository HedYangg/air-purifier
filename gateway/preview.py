"""Read-only camera and telemetry preview. Bind to loopback by default."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .hand import HOLD_SECONDS
from .history import RANGES, metrics_text, query_history

LOG = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Air purifier monitor</title>
<style>
  :root { color-scheme: dark; font-family: system-ui, sans-serif; background: #0c141a; color: #edf7f5; }
  main { width: min(1120px, calc(100% - 32px)); margin: clamp(24px, 5vw, 52px) auto; }
  h1 { margin: 0 0 .4rem; font-size: clamp(1.5rem, 4vw, 2.5rem); }
  p, small { color: #aec3c6; line-height: 1.55; }
  header { margin-bottom: 1.25rem; }
  header p { margin: 0; }
  .feed { background: #080e12; border: 1px solid #2c444b; border-radius: 14px; overflow: hidden; }
  img { display: block; width: 100%; aspect-ratio: 4 / 3; object-fit: contain; }
  dl { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 14px 0 0; }
  dl div { padding: 14px 16px; background: #14232a; border-radius: 10px; }
  dt { color: #8ce5cb; font-weight: 650; }
  dd { margin: 4px 0 0; }
  .layout { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 24px; }
  .layout.camera-only { display: block; max-width: 720px; }
  h2 { margin: 0; font-size: 1.15rem; }
  .connection { margin: 6px 0 0; font-size: .9rem; }
  .readings dt { color: #aec3c6; font-size: .84rem; }
  .readings dd { font-variant-numeric: tabular-nums; font-size: 1.1rem; }
  .history { margin-top: 24px; padding: 18px; border: 1px solid #2c444b; border-radius: 14px; background: #101d24; }
  .history-header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; }
  .history-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; font-size: .9rem; }
  select { font: inherit; color: inherit; background: #14232a; border: 1px solid #47626a; border-radius: 6px; padding: 6px; }
  canvas { display: block; width: 100%; height: 245px; margin-top: 10px; }
  @media (max-width: 760px) { .layout { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<main>
  <header>
    <h1>Air purifier monitor</h1>
    <p>@@MODE@@</p>
  </header>
  <div class="layout @@LAYOUT@@">
    <section aria-label="Gesture camera">
      <div class="feed">
        <img src="/stream.mjpeg" alt="Live camera with hand landmarks and latest gesture">
      </div>
      <dl>
        <div><dt>Open palm</dt><dd>@@HOLD@@ s &middot; ON request</dd></div>
        <div><dt>Closed fist</dt><dd>@@HOLD@@ s &middot; OFF request</dd></div>
      </dl>
      <p>Keep one full hand in view. To change commands, switch poses and hold the new pose steady.</p>
    </section>
    <section id="telemetry" @@TELEMETRY_HIDDEN@@ aria-labelledby="telemetry-title">
      <h2 id="telemetry-title">Live sensor readings</h2>
      <p class="connection" id="connections">Waiting for gateway data…</p>
      <dl class="readings">
        <div><dt>Temperature</dt><dd id="temperature">—</dd></div>
        <div><dt>Humidity</dt><dd id="humidity">—</dd></div>
        <div><dt>PM2.5</dt><dd id="pm25">—</dd></div>
        <div><dt>Gas response (MQ-135)</dt><dd id="gas">—</dd></div>
        <div><dt>Tilt</dt><dd id="tilt">—</dd></div>
        <div><dt>Side tilt</dt><dd id="side-tilt">—</dd></div>
        <div><dt>Motion</dt><dd id="moving">—</dd></div>
        <div><dt>Fan relay</dt><dd id="fan">—</dd></div>
        <div><dt>Safety lock</dt><dd id="lock">—</dd></div>
      </dl>
      <p class="connection" id="availability">— means waiting for a valid reading.</p>
    </section>
  </div>
  <section class="history" id="history" @@TELEMETRY_HIDDEN@@ aria-labelledby="history-title">
    <div class="history-header">
      <h2 id="history-title">Sensor history</h2>
      <div class="history-controls">
        <label>Reading
          <select id="history-reading">
            <option value="PM25">PM2.5 (µg/m³)</option>
            <option value="Temperature">Temperature (°C)</option>
            <option value="Humidity">Humidity (% RH)</option>
            <option value="GasADCmV">Gas response (mV)</option>
            <option value="TiltDegrees">Tilt (°)</option>
            <option value="SideTiltDegrees">Side tilt (°)</option>
          </select>
        </label>
        <label>Period
          <select id="history-range">
            <option value="15m">Last 15 minutes</option>
            <option value="1h">Last hour</option>
            <option value="6h">Last 6 hours</option>
            <option value="24h">Last 24 hours</option>
          </select>
        </label>
      </div>
    </div>
    <canvas id="history-chart" role="img" aria-label="Historical readings over time"></canvas>
    <p class="connection" id="history-status" aria-live="polite">Waiting for stored samples…</p>
  </section>
</main>
<script>
  const set = (id, value) => { document.getElementById(id).textContent = value; };
  const number = (value, unit, digits = 0) =>
    Number.isFinite(value) ? value.toFixed(digits) + unit : "—";
  const sensor = (value, unit, digits = 0) =>
    value >= 0 ? number(value, unit, digits) : "—";
  const flag = (value, on, off) => value === 1 ? on : value === 0 ? off : "—";

  function show(readings) {
    set("temperature", number(readings.Temperature, " °C", 1));
    set("humidity", number(readings.Humidity, " % RH", 1));
    set("pm25", sensor(readings.PM25, " µg/m³"));
    set("gas", sensor(readings.GasADCmV, " mV"));
    set("tilt", sensor(readings.TiltDegrees, "°", 1));
    set("side-tilt", sensor(readings.SideTiltDegrees, "°", 1));
    const stillLabel = readings.MotionLock === 1 ? "Settling after motion" : "Still";
    set("moving", flag(readings.Moving, "Moving", stillLabel));
    set("fan", readings.RelayEnabled === 0 ? "Dry run" : flag(readings.FanOn, "On", "Off"));
    set("lock", flag(readings.SafetyLock, "Locked", "Clear"));
  }

  async function updateReadings() {
    try {
      const response = await fetch("/api/stats", { cache: "no-store", signal: AbortSignal.timeout(3000) });
      if (!response.ok) throw new Error("Stats request failed");
      const { mqtt, blynk, readings } = await response.json();
      set("connections", "MQTT " + mqtt.toLowerCase() + " · ESP32 " +
        flag(readings.NodeOnline, "online", "offline") + " · Blynk " + blynk.toLowerCase());
      show(readings);
      set("availability", readings.ClimateOnline === 1
        ? "Showing the Pi's latest MQTT readings. — means unavailable."
        : "Waiting for a fresh KY-015 reading. — means unavailable.");
    } catch (error) {
      show({});
      set("connections", "Preview connection unavailable");
      set("availability", "Waiting for gateway data…");
    } finally {
      setTimeout(updateReadings, 2000);
    }
  }
  const historyReading = document.getElementById("history-reading");
  const historyRange = document.getElementById("history-range");
  const historyChart = document.getElementById("history-chart");
  let historyData = null;
  let historyRequest = 0;

  function drawHistory() {
    const width = historyChart.clientWidth;
    const height = historyChart.clientHeight;
    const scale = Math.min(window.devicePixelRatio || 1, 2);
    historyChart.width = width * scale;
    historyChart.height = height * scale;
    const ctx = historyChart.getContext("2d");
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    set("history-status", historyData ? "No stored samples for this reading and period yet." : "Waiting for stored samples…");
    const points = historyData?.series[historyReading.value] || [];
    if (!points.length || width < 100) return;
    const { start, end, step } = historyData;
    const values = points.map(([, value]) => value);
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const padding = Math.max((hi - lo) * 0.08, 0.3);
    const min = lo - padding, max = hi + padding;
    const left = 50, right = width - 12, top = 14, bottom = height - 32;
    const x = ts => left + (ts - start) * (right - left) / (end - start);
    const y = val => bottom - (val - min) * (bottom - top) / (max - min);
    const format = new Intl.DateTimeFormat(undefined, {hour: "2-digit", minute: "2-digit"});
    ctx.font = "12px system-ui";
    ctx.strokeStyle = "#2c444b";
    ctx.fillStyle = "#aec3c6";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const level = min + (max - min) * i / 4;
      const py = y(level);
      ctx.beginPath();
      ctx.moveTo(left, py);
      ctx.lineTo(right, py);
      ctx.stroke();
      ctx.fillText(level.toLocaleString(undefined, {maximumFractionDigits: 1}), left - 7, py);
    }
    const ticks = width < 500 ? 2 : 4;
    ctx.textBaseline = "top";
    for (let i = 0; i <= ticks; i++) {
      const time = start + (end - start) * i / ticks;
      ctx.textAlign = i === ticks ? "right" : i === 0 ? "left" : "center";
      ctx.fillText(format.format(time * 1000), x(time), bottom + 10);
    }
    ctx.strokeStyle = "#8ce5cb";
    ctx.fillStyle = "#8ce5cb";
    ctx.lineWidth = 2;
    ctx.beginPath();
    let previous = null;
    for (const [time, value] of points) {
      if (previous === null || time - previous > step * 1.5) {
        ctx.moveTo(x(time), y(value));
        ctx.fillRect(x(time) - 1, y(value) - 1, 3, 3);
      } else {
        ctx.lineTo(x(time), y(value));
      }
      previous = time;
    }
    ctx.stroke();
    set("history-status", "Prometheus · " + points.length + " chart points · " +
      "Latest " + values.at(-1).toLocaleString(undefined, {maximumFractionDigits: 1}) +
      " at " + format.format(points.at(-1)[0] * 1000));
  }

  async function updateHistory() {
    const request = ++historyRequest;
    try {
      const options = {cache: "no-store", signal: AbortSignal.timeout(5500)};
      const response = await fetch("/api/history?range=" + encodeURIComponent(historyRange.value), options);
      if (!response.ok) throw new Error("Prometheus history unavailable");
      const data = await response.json();
      if (request !== historyRequest) return;
      historyData = data;
      drawHistory();
    } catch (error) {
      if (request !== historyRequest) return;
      historyData = null;
      drawHistory();
      set("history-status", "History unavailable. Check the Prometheus service on the Pi.");
    }
  }

  if (!document.getElementById("telemetry").hidden) {
    updateReadings();
    updateHistory();
    historyReading.addEventListener("change", drawHistory);
    historyRange.addEventListener("change", updateHistory);
    window.addEventListener("resize", () => { if (historyData) drawHistory(); });
    setInterval(updateHistory, 15000);
  }
</script>
</body>
</html>
"""


class WebPreview:
    def __init__(self, host="127.0.0.1", port=8080, *, camera_only=False, read_status=None):
        self.address = (host, port)
        self.read_status = read_status
        mode = ("Camera-only test. Gesture requests are shown on the video."
                if camera_only else "Live camera and MQTT readings from the Raspberry Pi gateway.")
        self.page = (PAGE.replace("@@MODE@@", mode)
                     .replace("@@HOLD@@", f"{HOLD_SECONDS:g}")
                     .replace("@@LAYOUT@@", "camera-only" if camera_only else "")
                     .replace("@@TELEMETRY_HIDDEN@@", "hidden" if camera_only else "")
                     .encode("utf-8"))
        self.condition = threading.Condition()
        self.sequence = 0
        self.frame = None
        self.closed = False

    def __enter__(self):
        preview = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(5)
                super().setup()

            def do_GET(self):
                request = urlsplit(self.path)
                if request.path == "/":
                    self.send_body("text/html; charset=utf-8", preview.page)
                elif request.path == "/api/stats" and preview.read_status is not None:
                    self.send_json(preview.read_status())
                elif request.path == "/metrics" and preview.read_status is not None:
                    readings = preview.read_status()["readings"]
                    self.send_body("text/plain; version=0.0.4; charset=utf-8", metrics_text(readings).encode("utf-8"))
                elif request.path == "/api/history" and preview.read_status is not None:
                    window = parse_qs(request.query).get("range", ["15m"])[0]
                    if window not in RANGES:
                        self.send_json({"error": "Unsupported history period"}, status=400)
                        return
                    try:
                        self.send_json(query_history(window))
                    except (OSError, ValueError, KeyError, TypeError):
                        self.send_json({"error": "Prometheus unavailable"}, status=503)
                elif request.path == "/stream.mjpeg":
                    self.stream_video()
                else:
                    self.send_error(404)

            def send_json(self, payload, status=200):
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_body("application/json; charset=utf-8", body, status)

            def send_body(self, content_type, body, status=200):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def stream_video(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                sequence = 0
                try:
                    while True:
                        with preview.condition:
                            preview.condition.wait_for(
                                lambda: preview.closed or preview.sequence != sequence)
                            if preview.closed:
                                return
                            sequence, jpeg = preview.sequence, preview.frame
                        header = (f"--frame\r\nContent-Type: image/jpeg\r\n"
                                  f"Content-Length: {len(jpeg)}\r\n\r\n").encode("ascii")
                        self.wfile.write(header + jpeg + b"\r\n")
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    return

            def log_message(self, format, *args):
                LOG.debug(format, *args)

        self.server = ThreadingHTTPServer(self.address, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        LOG.info("Browser preview listening at %s:%d", host, port)
        return self

    def publish(self, jpeg):
        with self.condition:
            self.sequence += 1
            self.frame = jpeg
            self.condition.notify_all()

    def __exit__(self, *exc):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
