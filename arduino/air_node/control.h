#pragma once

#include <cmath>
#include <cstdint>

struct Vector3 {
  float x, y, z;
  constexpr float dot(const Vector3& other) const {
    return x * other.x + y * other.y + z * other.z;
  }
  constexpr Vector3 cross(const Vector3& other) const {
    return {y * other.z - z * other.y, z * other.x - x * other.z,
            x * other.y - y * other.x};
  }
  float norm() const { return std::sqrt(dot(*this)); }
  Vector3 normalized() const {
    const float length = norm();
    return {x / length, y / length, z / length};
  }
  Vector3 operator-(const Vector3& other) const {
    return {x - other.x, y - other.y, z - other.z};
  }
};

struct Control {
  bool purifierEnabled = false;
  bool locked = true;
  bool moving = false;
  bool imuOK = false;
  float tilt = -1;
  float sideTilt = -1;
  const char* lockReason = "startup";
  uint32_t lockGapMs = 0;

  Control(Vector3 upright, float pmThreshold)
      : pmThreshold(pmThreshold), uprightAxis(upright.normalized()),
        sideAxis(Vector3{-upright.y, upright.x, 0}.normalized()),
        frontAxis(uprightAxis.cross(sideAxis)) {}

  void updatePM(float value) {
    pmAboveThreshold = value > pmThreshold;
  }

  void gesture(bool on) {
    purifierEnabled = on;
  }

  bool requestsFan() const {
    return !locked && purifierEnabled && pmAboveThreshold;
  }

  void invalidateIMU() {
    imuOK = false;
    moving = false;
    tilt = -1;
    sideTilt = -1;
    locked = true;
    lockReason = "imu_read";
    lockGapMs = 0;
    settling = false;
    motionPending = false;
  }

  // Acceleration in g, angular rate in degrees per second.
  void updateIMU(Vector3 a, Vector3 gyro, uint32_t now, bool checkOrientation = true) {
    const float magnitude = a.norm();
    const uint32_t sampleGap = now - lastSample;
    if (imuOK && sampleGap > 150) {
      invalidateIMU();
      lockReason = "imu_gap";
      lockGapMs = sampleGap;
    }
    const float jerk = imuOK ? (a - previous).norm() : 0;
    const float deviation = std::fabs(magnitude - 1.0f);
    const float rotation = gyro.norm();
    previous = a;
    lastSample = now;
    imuOK = true;

    constexpr uint32_t motionConfirmMs = 150;
    const bool motionDetected = rotation > 25.0f || deviation > 0.15f || jerk > 0.10f;
    if (!motionDetected) {
      motionPending = false;
    } else if (!motionPending) {
      motionPending = true;
      motionSince = now;
    }
    const bool falling = magnitude < 0.5f;
    moving = falling || (motionPending && now - motionSince >= motionConfirmMs);
    if (!checkOrientation) return;

    // A near-zero acceleration sample has no usable gravity direction.
    if (magnitude <= 0.05f) {
      tilt = -1;
      sideTilt = -1;
      settling = false;
      return;
    }
    const float uprightProjection = a.dot(uprightAxis);
    tilt = tiltAngle(a.dot(frontAxis), uprightProjection);
    sideTilt = tiltAngle(a.dot(sideAxis), uprightProjection);
    constexpr float maxTiltDeviation = 30.0f;
    constexpr float angleRoundoff = 0.0001f;
    const bool outsideRange = std::fabs(tilt - normalTiltDegrees) > maxTiltDeviation + angleRoundoff ||
        std::fabs(sideTilt - normalTiltDegrees) > maxTiltDeviation + angleRoundoff;
    if (outsideRange) {
      locked = true;
      lockReason = "orientation";
      lockGapMs = 0;
      settling = false;
      return;
    }
    if (!locked) return;

    if (!settling) {
      settling = true;
      stableSince = now;
    } else if (now - stableSince >= 3000) {
      locked = false;
      lockReason = "clear";
      lockGapMs = 0;
    }
  }

 private:
  static constexpr float normalTiltDegrees = 100.0f;

  static float tiltAngle(float across, float upright) {
    const float angle = normalTiltDegrees - std::atan2(across, upright) * 57.2957795f;
    return std::fmax(0.0f, std::fmin(180.0f, angle));
  }

  const float pmThreshold;
  bool pmAboveThreshold = false;
  const Vector3 uprightAxis;
  const Vector3 sideAxis;
  const Vector3 frontAxis;
  uint32_t lastSample = 0;
  uint32_t motionSince = 0;
  bool motionPending = false;
  uint32_t stableSince = 0;
  bool settling = false;
  Vector3 previous{};
};
