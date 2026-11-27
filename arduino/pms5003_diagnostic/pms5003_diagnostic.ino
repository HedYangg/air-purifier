#include <Arduino.h>

constexpr int PMS_RX = 16;
constexpr unsigned long WARMUP_MS = 30000;
HardwareSerial pms(2);

unsigned long startedAt;
unsigned long lastReportAt;
unsigned long validFrames = 0;
unsigned long rejectedFrames = 0;

unsigned int readWord(const uint8_t* bytes) {
  return (static_cast<unsigned int>(bytes[0]) << 8) | bytes[1];
}

void setup() {
  Serial.begin(115200);
  pms.begin(9600, SERIAL_8N1, PMS_RX, -1);
  pms.setTimeout(200);
  startedAt = millis();
  lastReportAt = startedAt;
}

void loop() {
  static bool sawFirstHeaderByte = false;
  while (pms.available()) {
    const int byte = pms.read();
    if (!sawFirstHeaderByte || byte != 0x4D) {
      sawFirstHeaderByte = byte == 0x42;
      continue;
    }
    sawFirstHeaderByte = false;

    uint8_t frame[32] = {0x42, 0x4D};
    const size_t receivedBytes = 2 + pms.readBytes(frame + 2, sizeof(frame) - 2);
    if (receivedBytes != sizeof(frame)) {
      ++rejectedFrames;
      Serial.printf("INCOMPLETE FRAME: received=%u bytes rejected=%lu\n",
                    static_cast<unsigned int>(receivedBytes), rejectedFrames);
      continue;
    }
    const unsigned int payloadLength = readWord(frame + 2);
    if (payloadLength != 28) {
      ++rejectedFrames;
      Serial.printf("BAD FRAME LENGTH: length_field=%u rejected=%lu\n",
                    payloadLength, rejectedFrames);
      continue;
    }

    unsigned int calculatedChecksum = 0;
    for (size_t i = 0; i < sizeof(frame) - 2; ++i) calculatedChecksum += frame[i];
    const unsigned int receivedChecksum = readWord(frame + sizeof(frame) - 2);
    if (calculatedChecksum != receivedChecksum) {
      ++rejectedFrames;
      Serial.printf("CHECKSUM FAIL: calc=0x%04X recv=0x%04X rejected=%lu\n",
                    calculatedChecksum, receivedChecksum, rejectedFrames);
      continue;
    }
    ++validFrames;

    const unsigned long now = millis();
    if (now - startedAt < WARMUP_MS) continue;
    lastReportAt = now;
    Serial.printf("PMraw=%u PM1=%u PM10=%u PM2.5_CF1=%u count_0.3um=%u "
                  "CHECKSUM OK: calc=0x%04X recv=0x%04X valid=%lu rejected=%lu\n",
                  readWord(frame + 12), readWord(frame + 10), readWord(frame + 14),
                  readWord(frame + 6), readWord(frame + 16), calculatedChecksum,
                  receivedChecksum, validFrames, rejectedFrames);
  }

  const unsigned long now = millis();
  if (now - lastReportAt >= 5000) {
    lastReportAt = now;
    const char* status = now - startedAt < WARMUP_MS ? "Warming up" : "No recent valid frames";
    Serial.printf("%s (valid=%lu rejected=%lu)\n", status, validFrames, rejectedFrames);
  }
  delay(2);
}
