# Raspberry Pi 5 + ESP32 air purifier

Open [the three-page wiring diagram](docs/wiring.drawio) in Draw.io Desktop or diagrams.net. Open [air_node.ino](arduino/air_node/air_node.ino) in Arduino IDE and run [the gateway](gateway/run.py) on the Raspberry Pi 5. The previous SCD40 project is in the sibling `archived-scd40` folder.

## What runs where

| Platform | Hardware and responsibility |
| --- | --- |
| ESP32-S3 DevKit | PMS5003 over UART, MQ-135 through a voltage divider on ADC1, MPU6050 over I²C, KY-019 relay and DC fan |
| Raspberry Pi 5 | KY-015 (DHT11 temperature and humidity), CSI camera, MediaPipe Hand Landmarker, gesture logic, Mosquitto, and Blynk bridge |
| Prometheus on the Pi | On-disk time-series database for the gateway's numeric IoT readings |
| Blynk.Cloud | Live dashboard and history charts for the configured datastreams |

The ESP32 sends telemetry to Mosquitto on the Pi over Wi-Fi. The Pi reads the camera and KY-015 locally, publishes their data to Mosquitto, and forwards the latest readings to Blynk via MQTT over TLS. Gesture commands travel back from the Pi to the ESP32 via local MQTT. MediaPipe processes the live camera feed locally. The optional AI Camera Check sends a single snapshot to Gemini on request. Blynk and Gemini require internet access.

The dashboard shows **Gas response (MQ-135)** as millivolts measured at ESP32-S3 GPIO4 after the divider. After the sensor warms up, compare readings over time under similar temperature and humidity. The sensor responds to multiple substances, including ammonia and some solvent vapors. A change in voltage alone cannot identify the gas, determine CO₂ concentration, or provide an Air Quality Index. [Winsen MQ-135 manual](https://www.winsen-sensor.com/manual/mq135.html?lang=en)

## Fan behavior

The ESP32 checks these conditions in order. Any failed condition turns the fan off.

1. **Tilt in range.** The MPU6050 uses a fixed reference vector in `config.h`. At each one-second serial update, the lock engages if either measured angle is below 70° or above 130°. The lock clears after three seconds of in-range checks. Loss of MPU6050 communication keeps the fan off. Acceleration and rotation report motion in the dashboard.
2. **Purifier enabled.** The purifier starts disabled. Hold an open palm for three seconds to enable automatic fan operation. Hold a closed fist for three seconds to disable it until the next open palm.
3. **PM2.5 above the threshold.** A fresh PMS5003 reading must be strictly above `PM_ON` (20 µg/m³ by default). A reading at or below the threshold, or an unavailable reading, keeps the fan off.

The ESP32 uses the PMS5003's atmospheric PM2.5 value directly for fan control and MQTT. Serial prints that same value as `PM` and `PMraw`. The dashboard uses the MQTT value. Historical samples collected with the earlier firmware contain the previously adjusted readings.

The motion loop runs locally even if Wi-Fi drops. The firmware accepts PM2.5 readings after 30 seconds of warm-up and marks the reading unavailable after 10 seconds without a valid frame. The IMU detects acceleration and rotation. Smooth constant-speed movement can escape detection. Use a rigid mount and tune with the real fan running, since strong vibration can trigger the cutoff. This is a prototype interlock.

## Wiring and first power-up

The diagram uses an **ESP32-S3 DevKit** with GPIO4, GPIO8, GPIO9, GPIO16, and GPIO17 exposed, a **3.3 V-compatible MPU6050 breakout**, a **four-pin MQ-135 breakout with AOUT**, a **DHT11 KY-015**, and a CSI camera supported by your Pi OS. Use the GPIO numbers printed on your specific board. The ESP32-S3 has no GPIO22, and GPIO19/20 are reserved here for native USB. [Espressif ESP32-S3 GPIO reference](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/gpio.html)

- Supply the PMS5003, MQ-135 and relay coil from a regulated 5 V sensor supply. Join its negative to ESP32 GND. Power the ESP32 by USB and the Pi by its own USB-C supply. Verify the 5 V rail under load, especially with the MQ heater on.
- MQ-135 AOUT → **15 kΩ** → GPIO4 (ADC1), with **10 kΩ** from GPIO4 to node GND. At 5 V AOUT the divider gives about 2 V at the ADC. Verify that voltage before attaching GPIO4. Leave DOUT disconnected.
- Power the MPU6050 at 3.3 V, set AD0 low, and ensure the SDA/SCL pull-ups go to 3.3 V. Use the breakout's built-in pull-ups if present. GPIO8 is SDA and GPIO9 is SCL.
- Connect PMS5003 pin 5 TX to ESP32 GPIO16 RX. Use the connector datasheet to identify VCC and GND from the correct side. [Plantower PMS5003 manual](https://www.aqmd.gov/docs/default-source/aq-spec/resources-page/plantower-pms5003-manual_v2-3.pdf)
- KY-015 uses Pi physical pin 1 for 3.3 V, pin 11 for GPIO17 data and pin 6 for ground. Add a **10 kΩ** data pull-up to 3.3 V if the module lacks one. For a variant that requires 5 V, use a bidirectional level shifter on DATA before attaching it to the Pi. [Joy-IT KY-015 examples](https://www.sensorkit.joy-it.net/en/sensors/ky-015)
- The diagram assumes an active-high KY-019. Use a **10 kΩ** pull-down from ESP32-S3 GPIO17 to node GND for startup. Only wire GPIO17 directly to S if your exact powered module accepts 3.3 V and does not drive or pull S above 3.3 V. For a 5 V-only active-high board, use a powered **74AHCT125** non-inverting buffer with its channel OE tied to ground and local 100 nF supply bypass. If the module is a different trigger type, verify its requirements before enabling the relay. [Joy-IT KY-019 documentation](https://sensorkit.joy-it.net/en/sensors/ky-019)
- Use a separately powered low-voltage DC fan. Switch its positive lead through relay COM and NO, add a properly rated fuse, and select the fan/supply so startup current is within the board's DC contact rating. Guard the fan blades and provide the motor's specified transient suppression. Keep mains voltage out of this build.

## Setup

### 1. Pi 5 and local MQTT

Use 64-bit Raspberry Pi OS. With the Pi unplugged, attach the CSI cable with the correct Pi 5 end. After booting, check the camera and install the runtime packages. On Raspberry Pi OS Lite use `rpicam-hello -n -t 3000` for a headless check.

```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients python3-picamera2 python3-opencv \
  python3-numpy python3-matplotlib python3-lgpio python3-venv libportaudio2
rpicam-hello --list-cameras
```

From the `air-purifier` folder, install the provided configuration. The first `mosquitto_passwd` command creates a new file, so only use `-c` the first time. These commands assume port 1883 is available and no conflicting Mosquitto listener has already been configured.

```bash
sudo mosquitto_passwd -c /etc/mosquitto/air-purifier.passwd air-node
sudo mosquitto_passwd /etc/mosquitto/air-purifier.passwd air-gateway
sudo chown root:mosquitto /etc/mosquitto/air-purifier.passwd
sudo chmod 640 /etc/mosquitto/air-purifier.passwd
sudo install -m 644 mosquitto/air-purifier.acl /etc/mosquitto/air-purifier.acl
sudo install -m 644 mosquitto/air-purifier.conf /etc/mosquitto/conf.d/air-purifier.conf
sudo systemctl enable --now mosquitto
sudo systemctl restart mosquitto
hostname -I
```

Keep local port 1883 on a trusted LAN. The broker requires passwords and restricts each client by topic. This LAN hop uses plain MQTT. The Blynk hop uses certificate-verified TLS on port 8883.

### 2. ESP32 firmware

1. In Arduino IDE, install `esp32` by Espressif Systems and `PubSubClient` by Nick O’Leary. Select **ESP32S3 Dev Module**, or the Arduino board entry for your exact ESP32-S3 variant. When using a native USB port for Serial Monitor, enable **USB CDC On Boot**. The sketch folder contains `config.example.h`. Copy it to `config.h` if that file is absent.
2. Edit `config.h` with Wi-Fi credentials, the Pi's LAN IP from `hostname -I`, and the **air-node** broker password. Leave `ENABLE_RELAY=false` for the first tests.
3. Upload and open Serial Monitor at 115200. With the purifier in its normal mounted position and still, copy the `a=(x,y,z)` readings into `UPRIGHT_X`, `UPRIGHT_Y`, and `UPRIGHT_Z` in `config.h`, then upload again. The firmware uses their direction as the reference. The two angles, `tilt` and `sideTilt`, should both read about 100° at rest. The web preview shows both angles live and in Sensor history. Check that `lock` clears after three seconds of in-range checks and re-engages when a serial update shows an out-of-range angle.
4. With **the fan supply disconnected**, set `ENABLE_RELAY=true`, verify the relay polarity and check that COM-to-NO stays open at boot and during the lockout. Use the open-palm gesture and verify `enabled=1` in Serial Monitor. To bench-test automatic start, temporarily lower `PM_ON` below the current PM2.5 reading, then restore it. Connect the fan supply only after all checks pass.

### 3. Pi gateway and MediaPipe

```bash
cp .env.example .env
nano .env
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --only-binary=:all: --no-deps mediapipe==1.2.3
python -c "import mediapipe, picamera2, cv2; print(mediapipe.__version__)"
python -m gateway.run --preview
```

Set `MQTT_PASSWORD` to the **air-gateway** password. Keep `MQTT_HOST=127.0.0.1` when Mosquitto runs on the same Pi. The MediaPipe install reuses the Pi OS OpenCV and NumPy packages so Picamera2 can use its system binaries. The model belongs at `models/hand_landmarker.task`. If the model was omitted when copying the folder to the Pi, restore it from Google's official model download.

```bash
mkdir -p models
curl -fL 'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task' -o models/hand_landmarker.task
```

For headless operation, use `python -m gateway.run`. For MQTT and KY-015 bring-up before attaching the camera, add `--no-camera`. The preview draws the 21 landmarks. The Python rules use the angles of the four fingers and normalized fingertip distances to classify one hand at a time. Keep the entire hand in view under even lighting. [Google MediaPipe setup for Raspberry Pi 5](https://developers.google.com/edge/mediapipe/solutions/setup_python) · [Raspberry Pi camera setup](https://www.raspberrypi.com/documentation/accessories/camera-software.html)

#### Browser preview over SSH

Stop any running gateway or camera test before starting the preview. From the project folder with `.venv` active, run:

```bash
python -m gateway.run --web-preview --camera-only
```

You can test the camera and gesture recognition without Mosquitto or Blynk. The video shows the detected pose and the last ON/OFF request. Keep one hand in view and hold the pose steady for three seconds. The timer starts when the pose is recognized and resets if the detected pose changes or tracking is lost. You can go directly from palm to fist or fist to palm. If you need to repeat the same command, move your hand out of view for about half a second before showing the pose again.

The preview listens on Pi port 8080. From your PC, open an SSH tunnel in a second terminal and browse to `http://127.0.0.1:8080`:

```bash
ssh -N -L 8080:127.0.0.1:8080 YOUR_PI_USER@YOUR_PI_HOST
```

On a trusted local network you can instead add `--web-host 0.0.0.0` and open `http://PI_LAN_IP:8080` from your PC. Anyone who can reach that port can view the stream, so keep it off public or shared networks. Find the Pi's LAN IP with `hostname -I`.

When Mosquitto is configured, omit `--camera-only` to run the full gateway and browser preview in the same process. The preview shows temperature, humidity, PM2.5, MQ-135 gas response in millivolts, both tilt angles, motion, fan relay, safety lock, and the MQTT, ESP32, and Blynk connection states. The values refresh from the Pi's local MQTT readings even if Blynk is disabled. A dash means there is no current valid reading. The fan card shows Dry run while the firmware's relay output is disabled.

Omit `--web-preview` for regular operation without video in a browser. The gateway must run while you want gestures and Blynk updates. Closing the preview tab leaves the gateway running.

#### AI Camera Check and laptop mode

Under **AI Camera Check**, select **Open palm** or **Closed fist** and press **Check camera frame** while holding the pose. The gateway sends one current snapshot to Gemini for a tip about visible issues such as bent fingers, a tucked thumb, an edge-on hand, cropping, or lighting. The preview shows the MediaPipe result from the same snapshot alongside Gemini's suggestion and response time. MediaPipe runs continuously on the device and handles gesture recognition and the three-second hold.

Create a [Gemini API key](https://aistudio.google.com/apikey) and set `GEMINI_API_KEY` in the project's `.env`. `GEMINI_MODEL` defaults to `gemini-3.1-flash-lite`. Keep the `.env` file local. On the Pi, restart the gateway with your usual `--web-preview` command to enable the button. [Gemini image understanding](https://ai.google.dev/gemini-api/docs/image-understanding)

On a Windows laptop, open PowerShell in the project folder and run the following with Python 3.12 installed.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-laptop.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Save `GEMINI_API_KEY` in `.env`, then download the hand landmark model if it is missing and start the webcam preview.

```powershell
if (-not (Test-Path models/hand_landmarker.task)) {
    New-Item -ItemType Directory -Path models -Force | Out-Null
    curl.exe -fL 'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task' -o models/hand_landmarker.task
}
.\.venv\Scripts\python.exe -m gateway.run --laptop
```

Open [the laptop preview](http://127.0.0.1:8080). Laptop mode starts the webcam, local MediaPipe tracking, and the optional Gemini check. It requires no Raspberry Pi, ESP32, MQTT broker, Blynk connection, or Prometheus database. The webcam and gesture preview also work while the API key field is empty. If Windows picks the wrong camera, restart with `--laptop --camera-index 1`. Press **Ctrl+C** to stop the preview.

#### Store and view sensor history on the Pi

Prometheus scrapes the gateway's read-only `/metrics` endpoint every five seconds and stores timestamped samples on disk. The preview's **Sensor history** card retrieves them from Prometheus. Keep the full gateway running with `--web-preview` for collection. The browser tab can be closed during collection. Readings that stopped arriving at the gateway appear as gaps after the freshness interval.

1. Install Prometheus on the Pi.

   ```bash
   sudo apt update
   sudo apt install -y --no-install-recommends prometheus
   ```

2. In `sudo nano /etc/default/prometheus`, set the existing `ARGS=` line to `ARGS="--web.listen-address=127.0.0.1:9090"` on a fresh installation. If the line already contains custom flags, append the listen-address flag inside the quotes. This keeps the database API local to the Pi.

3. In `sudo nano /etc/prometheus/prometheus.yml`, add this job at the end of the existing `scrape_configs:` list. Keep the two spaces before the first dash and keep the existing jobs.

   ```yaml
     - job_name: air-purifier
       scrape_interval: 5s
       scrape_timeout: 3s
       static_configs:
         - targets: ["127.0.0.1:8080"]
   ```

4. Check the configuration, then restart the database service.

   ```bash
   sudo promtool check config /etc/prometheus/prometheus.yml &&
   sudo systemctl restart prometheus
   ```

5. After copying the current `gateway/history.py` and `gateway/preview.py` to the Pi, restart the full gateway with your usual `python -m gateway.run --web-preview` command and options. Wait for two or three scrapes. Refresh the preview and scroll to **Sensor history**. You can pick a reading and a period from **15 minutes to 24 hours**. The plot averages samples over a wider interval for longer periods.

To verify collection before opening the browser, run `curl -fsS http://127.0.0.1:8080/metrics` on the Pi and check for lines with a sensor name and number. Then run `curl -fsSG http://127.0.0.1:9090/api/v1/query --data-urlencode 'query=up{job="air-purifier"}'` and look for a value of `1`. Prometheus starts recording when its scrape job and the gateway are both running. It retains the samples across gateway restarts. [Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/) · [Time-range API](https://prometheus.io/docs/prometheus/latest/querying/api/)

### 4. Blynk dashboard

Create a Blynk template and device, then add virtual-pin datastreams with these **exact names**. Use the listed virtual pins or any distinct available pins. MQTT uses the names. Set `BLYNK_HOST` to the regional hostname for your Console, `BLYNK_TOKEN` to the device Auth Token, and `BLYNK_TEMPLATE_ID` to the template ID. Restart the gateway. [Blynk MQTT authentication](https://docs.blynk.io/en/blynk.cloud-mqtt-api/device-mqtt-api/authentication) · [Blynk named datastreams](https://docs.blynk.io/en/blynk.cloud-mqtt-api/device-mqtt-api/datastreams)

| Pin | Name | Type | Range and display |
| --- | --- | --- | --- |
| V0 | `Temperature` | Double | −20 to 80 °C, chart/value |
| V1 | `Humidity` | Double | 0 to 100 %RH, chart/value |
| V2 | `PM25` | Integer | −1 to 65535 µg/m³, chart/value |
| V3 | `GasADCmV` | Integer | −1 to 2500 mV, label the chart or value widget Gas response (MQ-135) |
| V4 | `TiltDegrees` | Double | −1 to 180°, chart/value |
| V5 | `FanOn` | Integer | −1 to 1, relay output state |
| V6 | `SafetyLock` | Integer | −1 to 1 |
| V7 | `ImuOK` | Integer | −1 to 1 |
| V8 | `Moving` | Integer | −1 to 1 |
| V9 | `ManualStop` | Integer | 1 when the purifier is disabled, 0 when enabled, −1 when unavailable |
| V10 | `RelayEnabled` | Integer | −1 to 1, 0 in dry run |
| V11 | `NodeOnline` | Integer | 0 to 1 |
| V12 | `ClimateOnline` | Integer | 0 to 1 |
| V13 | `Gesture` | String | Show the current detection as text |

Use value widgets for the latest readings and configure charts with history enabled where supported by your Blynk plan. Use separate axes or charts for different units. For ESP32 fields, −1 means unavailable or stale. `ClimateOnline=0` means the last climate values in Blynk are stale. `FanOn` reports the commanded relay output. It does not prove the physical fan is spinning. A Blynk connection requires a working Internet link and valid device token. Blynk receives monitoring data every five seconds.

If upgrading a Blynk dashboard that had an MQ-135 CO₂ widget, remove that widget and display `GasADCmV` on V3 in mV.

| Local MQTT topic | Source and purpose |
| --- | --- |
| `air/node/pm25` | PMS5003 concentration used for automatic control and dashboard |
| `air/node/gas_mv` | MQ-135 ADC voltage used for monitoring |
| `air/node/state` | Relay, movement, both tilt angles, and interlock status |
| `air/node/online` | ESP32 presence and broker last will |
| `air/gateway/climate` | KY-015 temperature and relative humidity |
| `air/camera/gesture` | Latest MediaPipe-based classification |
| `air/camera/command` | Non-retained enable/disable requests sent to the ESP32 |

## Evaluation and rubric

With the normal gateway stopped, run `python -m gateway.run --evaluate` on the Pi desktop. Label independent trials with **o** (open palm), **f** (fist), **n** (no hand) or **x** (other). Press **q** to finish. The evaluator prints classification accuracy and processed FPS and saves a CSV plus success/failure images locally. Test different people, distances and lighting. During real bring-up also verify whether each deliberate gesture changes the relay state.

There are three separate sensors on the ESP32, one climate module plus the camera on the Pi, one I²C device (MPU6050), and one actuator (fan). Confirm with the instructor that camera input counts toward the gateway's two sensors and the five sensor types. If the camera is counted only under the video item, this parts list has four sensing modules of different types plus video. MediaPipe is local pretrained AI. Confirm whether a separate AI service is still required under that rubric item.

For the time-series database criterion, show Prometheus collecting from the `air-purifier` target and querying a past period. For criterion 13, show the live cards and **Sensor history** chart in the same dashboard. Record data ahead of the demo so the history period contains several minutes of real readings.
