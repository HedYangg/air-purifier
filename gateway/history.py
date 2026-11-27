"""Expose IoT readings to Prometheus and retrieve recent time series."""

import json
import math
import time
from urllib.parse import urlencode
from urllib.request import urlopen

RANGES = {"15m": (900, 5), "1h": (3600, 15), "6h": (21600, 60), "24h": (86400, 120)}
SENSOR_FIELDS = ("Temperature", "Humidity", "PM25", "GasADCmV", "TiltDegrees", "SideTiltDegrees")
STATE_FIELDS = ("NodeOnline", "ClimateOnline", "FanOn", "Moving", "SafetyLock", "ImuOK",
                "ManualStop", "RelayEnabled")
PROMETHEUS_URL = "http://127.0.0.1:9090"


def metrics_text(readings):
    """Emit fresh numeric gauges. The -1 missing sentinel is excluded for node readings."""
    lines = [
        "# HELP air_purifier_reading Latest MQTT reading from the air purifier gateway.",
        "# TYPE air_purifier_reading gauge",
    ]
    for name in SENSOR_FIELDS + STATE_FIELDS:
        value = readings.get(name)
        if not isinstance(value, (float, int)) or not math.isfinite(value):
            continue
        if name != "Temperature" and value < 0:
            continue
        lines.append(f'air_purifier_reading{{sensor="{name}"}} {value:g}')
    return "\n".join(lines) + "\n"


def query_history(window, *, base_url=PROMETHEUS_URL):
    duration, step = RANGES[window]
    end = int(time.time())
    start = end - duration
    sensors = "|".join(SENSOR_FIELDS)
    query = f'avg_over_time(air_purifier_reading{{job="air-purifier",sensor=~"{sensors}"}}[{max(10, step)}s])'
    params = urlencode({"query": query, "start": start, "end": end, "step": step, "timeout": "3s"})
    with urlopen(f"{base_url}/api/v1/query_range?{params}", timeout=4) as upstream:
        payload = json.load(upstream)
    if payload["status"] != "success" or payload["data"]["resultType"] != "matrix":
        raise ValueError("Prometheus did not return a range result")

    series = {name: [] for name in SENSOR_FIELDS}
    for item in payload["data"]["result"]:
        name = item["metric"].get("sensor")
        if name not in series:
            continue
        for at, value in item["values"]:
            number = float(value)
            if math.isfinite(at) and math.isfinite(number):
                series[name].append([at, number])
    return {"start": start, "end": end, "step": step, "series": series}
