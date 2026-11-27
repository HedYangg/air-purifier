#include <Arduino.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <Wire.h>

#include "config.h"
#include "control.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3)
#error "Select an ESP32-S3 board in Arduino IDE."
#endif

// ESP32-S3 DevKit header GPIO numbers.
constexpr int RELAY = 17;
constexpr int PMS_RX = 16;
constexpr int MQ_ADC = 4; // ADC1_CH3
constexpr int SDA_PIN = 8;
constexpr int SCL_PIN = 9;
constexpr uint8_t MPU_ADDRESS = 0x68;
constexpr int RELAY_OFF = RELAY_ACTIVE_HIGH ? LOW : HIGH;
constexpr uint32_t PMS_WARMUP_MS = 30000;
constexpr char COMMAND_TOPIC[] = "air/camera/command";
constexpr Vector3 UPRIGHT_ACCEL{UPRIGHT_X, UPRIGHT_Y, UPRIGHT_Z};

static_assert(PM_ON >= 0, "Set a nonnegative PM2.5 threshold");
static_assert(UPRIGHT_X != 0.0f || UPRIGHT_Y != 0.0f,
              "Set the upright acceleration reference for the side-mounted MPU6050");

struct Snapshot {
  int pm25 = -1;
  int gasMv = -1;
  float tilt = -1;
  float sideTilt = -1;
  bool fan = false;
  bool locked = true;
  bool imuOK = false;
  bool moving = false;
  bool demand = false;
  bool purifierEnabled = false;
};

Control control(UPRIGHT_ACCEL, PM_ON);
QueueHandle_t commandQueue;
QueueHandle_t snapshotQueue;
HardwareSerial pms(2);
WiFiClient transport;
PubSubClient broker(transport);
uint32_t started;
uint32_t lastPM = 0;
int pm25 = -1;
bool mpuReady = false;

bool mpuWrite(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(MPU_ADDRESS);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool mpuRead(uint8_t reg, uint8_t* bytes, size_t length) {
  Wire.beginTransmission(MPU_ADDRESS);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MPU_ADDRESS, length) != length) return false;
  for (size_t i = 0; i < length; ++i) bytes[i] = Wire.read();
  return true;
}

bool initMPU() {
  uint8_t id;
  return mpuRead(0x75, &id, 1) && id == 0x68 &&
      mpuWrite(0x6B, 0x01) &&   // Wake, use gyro PLL
      mpuWrite(0x1A, 0x04) &&   // Low-pass filter around 20 Hz
      mpuWrite(0x19, 19) &&     // 50 Hz internal sample rate
      mpuWrite(0x1B, 0x08) &&   // +/-500 deg/s
      mpuWrite(0x1C, 0x08);     // +/-4 g
}

int16_t signedWord(const uint8_t* bytes) {
  return static_cast<int16_t>((uint16_t(bytes[0]) << 8) | bytes[1]);
}

bool readMPU(Vector3& accel, Vector3& gyro) {
  uint8_t data[14];
  if (!mpuRead(0x3B, data, sizeof(data))) return false;
  accel = {signedWord(data)/8192.0f, signedWord(data+2)/8192.0f,
           signedWord(data+4)/8192.0f};
  gyro = {signedWord(data+8)/65.5f, signedWord(data+10)/65.5f,
          signedWord(data+12)/65.5f};
  return true;
}

// Streaming PMS5003 parser. Use the atmospheric PM2.5 field and verify checksum.
void readPMS(uint32_t now) {
  static uint8_t frame[32];
  static size_t used = 0;
  for (int i = 0; i < 96 && pms.available(); ++i) {
    const uint8_t value = pms.read();
    if (used == 0 && value != 0x42) continue;
    if (used == 1 && value != 0x4D) {
      used = value == 0x42 ? 1 : 0;
      continue;
    }
    frame[used++] = value;
    if (used != sizeof(frame)) continue;
    used = 0;
    uint16_t sum = 0;
    for (int j = 0; j < 30; ++j) sum += frame[j];
    if (frame[2] != 0 || frame[3] != 28 ||
        sum != ((uint16_t(frame[30]) << 8) | frame[31])) continue;
    if (now - started < PMS_WARMUP_MS) continue;
    pm25 = (uint16_t(frame[12]) << 8) | frame[13];
    lastPM = now;
    control.updatePM(pm25);
  }
  if (pm25 >= 0 && now - lastPM > 10000) {
    pm25 = -1;
    control.updatePM(-1);
  }
}

void onCommand(char* topic, uint8_t* payload, unsigned int size) {
  if (strcmp(topic, COMMAND_TOPIC) != 0) return;
  const bool on = size == 2 && memcmp(payload, "ON", 2) == 0;
  if (!on && !(size == 3 && memcmp(payload, "OFF", 3) == 0)) return;
  // One-slot mailbox makes the newest gesture take precedence.
  xQueueOverwrite(commandQueue, &on);
}

void publishInt(const char* topic, int value) {
  char data[16];
  snprintf(data, sizeof(data), "%d", value);
  broker.publish(topic, data);
}

// Core 0 owns Wi-Fi and MQTT. Relay and sensor checks stay on the Arduino loop.
void networkTask(void*) {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  broker.setServer(MQTT_HOST, 1883);
  broker.setSocketTimeout(2);
  broker.setCallback(onCommand);
  uint32_t attempted = millis() - 5000;
  uint32_t sent = millis() - 1000;
  while (true) {
    const uint32_t now = millis();
    if (WiFi.status() == WL_CONNECTED) {
      if (!broker.connected() && now - attempted >= 5000) {
        attempted = now;
        if (broker.connect("purifier-node", MQTT_USER, MQTT_PASSWORD,
                           "air/node/online", 0, true, "0")) {
          broker.publish("air/node/online", "1", true);
          broker.subscribe(COMMAND_TOPIC);
        }
      }
      if (broker.connected() && broker.loop() && now - sent >= 1000) {
        Snapshot s;
        if (xQueuePeek(snapshotQueue, &s, 0) == pdTRUE) {
          sent = now;
          publishInt("air/node/pm25", s.pm25);
          publishInt("air/node/gas_mv", s.gasMv);
          char state[185];
          snprintf(state, sizeof(state),
              "{\"fan\":%d,\"locked\":%d,\"imu_ok\":%d,\"moving\":%d,"
              "\"tilt\":%.1f,\"side_tilt\":%.1f,\"demand\":%d,\"manual_stop\":%d,\"relay_enabled\":%d}",
              s.fan, s.locked, s.imuOK, s.moving, s.tilt, s.sideTilt, s.demand,
              !s.purifierEnabled, ENABLE_RELAY);
          broker.publish("air/node/state", state);
        }
      }
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

void setup() {
  digitalWrite(RELAY, RELAY_OFF);
  pinMode(RELAY, OUTPUT);
  Serial.begin(115200);
  Wire.begin(SDA_PIN, SCL_PIN, 100000);
  Wire.setTimeOut(5);
  analogSetPinAttenuation(MQ_ADC, ADC_11db);
  pms.begin(9600, SERIAL_8N1, PMS_RX, -1);
  started = millis();
  snapshotQueue = xQueueCreate(1, sizeof(Snapshot));
  commandQueue = xQueueCreate(1, sizeof(bool));
  if (snapshotQueue && commandQueue) {
    xTaskCreatePinnedToCore(networkTask, "mqtt", 6144, nullptr, 1, nullptr, 0);
  }
  Serial.println(ENABLE_RELAY ? "Relay output ENABLED" : "Dry run: relay output disabled");
}

void loop() {
  static uint32_t lastSample = 0;
  static uint32_t lastInit = millis() - 1000;
  static uint32_t lastLog = 0;
  static Vector3 accel{};
  static int gasMv = -1;
  const uint32_t now = millis();
  if (!mpuReady && now - lastInit >= 1000) {
    lastInit = now;
    mpuReady = initMPU();
    if (!mpuReady) control.invalidateIMU();
  }
  const bool imuSampleDue = mpuReady && now - lastSample >= 20;
  // When the MPU is ready, report the same fresh sample used to check orientation.
  const bool logDue = now - lastLog >= 1000 && (!mpuReady || imuSampleDue);
  if (imuSampleDue) {
    lastSample = now;
    Vector3 gyro{};
    if (readMPU(accel, gyro)) {
      control.updateIMU(accel, gyro, now, logDue);
    } else {
      mpuReady = false;
      control.invalidateIMU();
    }
  }
  readPMS(now);
  bool command;
  if (commandQueue && xQueueReceive(commandQueue, &command, 0) == pdTRUE) {
    control.gesture(command);
  }
  const bool requested = control.requestsFan();
  const bool fan = ENABLE_RELAY && requested;
  digitalWrite(RELAY, fan ? !RELAY_OFF : RELAY_OFF);

  if (logDue) {
    lastLog = now;
    uint32_t sum = 0;
    for (int i = 0; i < 12; ++i) sum += analogReadMilliVolts(MQ_ADC);
    gasMv = sum / 12;
    Serial.printf("PM=%d PMraw=%d MQ-135 ADC=%dmV a=(%.2f,%.2f,%.2f) tilt=%.1f sideTilt=%.1f moving=%d lock=%d lock_reason=%s lock_gap_ms=%lu enabled=%d request=%d relay=%d\n",
                  pm25, pm25, gasMv, accel.x, accel.y, accel.z, control.tilt, control.sideTilt,
                  control.moving, control.locked, control.lockReason,
                  static_cast<unsigned long>(control.lockGapMs), control.purifierEnabled, requested, fan);
  }
  Snapshot s;
  s.pm25 = pm25;
  s.gasMv = gasMv;
  s.tilt = control.tilt;
  s.sideTilt = control.sideTilt;
  s.fan = fan;
  s.locked = control.locked;
  s.imuOK = control.imuOK;
  s.moving = control.moving;
  s.demand = requested;
  s.purifierEnabled = control.purifierEnabled;
  if (snapshotQueue) xQueueOverwrite(snapshotQueue, &s);
  delay(2);
}
