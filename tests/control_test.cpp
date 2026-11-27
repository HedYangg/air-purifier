#include <cassert>
#include <cstring>
#include "../arduino/air_node/control.h"

int main() {
  constexpr float diagonal = 0.70710678f;
  const Vector3 upright{-diagonal, -diagonal, 0}, still{0, 0, 0}, tipped{0, 0, 1};
  Control c({-1, -1, 0}, 20);
  c.updatePM(21);
  assert(c.locked && !c.purifierEnabled && !c.requestsFan());
  assert(std::strcmp(c.lockReason, "startup") == 0);
  for (uint32_t ms = 0; ms <= 3000; ms += 20) {
    c.updateIMU(upright, still, ms);
    assert(c.locked == (ms < 3000));
    assert(!c.requestsFan());
  }
  c.updatePM(4);
  c.gesture(true);
  assert(c.purifierEnabled && !c.requestsFan());
  c.updatePM(21);
  assert(c.requestsFan());
  c.updatePM(20);
  assert(!c.requestsFan());
  c.updatePM(21);
  assert(c.requestsFan());
  c.updatePM(-1);
  assert(c.purifierEnabled && !c.requestsFan());
  c.updatePM(25);
  c.gesture(false);
  assert(!c.purifierEnabled && !c.requestsFan());
  c.updatePM(4);
  c.updatePM(25);
  assert(!c.purifierEnabled && !c.requestsFan());
  c.gesture(true);
  assert(c.requestsFan());
  c.updateIMU({1.5f * upright.x, 1.5f * upright.y, 0}, {0, 0, 70}, 3020);
  c.updateIMU(upright, still, 3040);
  c.updateIMU(upright, still, 3060);
  assert(!c.moving && !c.locked && c.requestsFan());
  for (uint32_t ms = 3080; ms <= 3280; ms += 20) {
    c.updateIMU(upright, {0, 0, 20}, ms);
    assert(!c.moving && c.requestsFan());
  }
  for (uint32_t ms = 3300; ms <= 3460; ms += 20) {
    c.updateIMU(upright, {0, 0, 30}, ms, ms == 3460);
    assert(c.moving == (ms == 3460));
    assert(c.requestsFan());
  }
  assert(!c.locked && c.moving && c.purifierEnabled && c.requestsFan());
  for (uint32_t ms = 3480; ms <= 6600; ms += 20) {
    c.updateIMU(tipped, still, ms);
    assert(c.locked && !c.requestsFan());
    assert(std::strcmp(c.lockReason, "orientation") == 0);
  }
  for (uint32_t ms = 6620; ms <= 9640; ms += 20) {
    c.updateIMU(upright, still, ms);
    assert(c.requestsFan() == (ms >= 9620));
  }
  c.updateIMU(upright, still, 9900);
  assert(c.locked && !c.moving && !c.requestsFan());
  assert(c.tilt == 100 && c.sideTilt == 100);
  assert(std::strcmp(c.lockReason, "imu_gap") == 0 && c.lockGapMs == 260);
  c.updateIMU(upright, still, 9920);
  assert(std::strcmp(c.lockReason, "imu_gap") == 0 && c.lockGapMs == 260);
  c.invalidateIMU();
  assert(c.locked && !c.imuOK && !c.requestsFan());
  assert(std::strcmp(c.lockReason, "imu_read") == 0 && c.lockGapMs == 0);
  for (uint32_t ms = 9920; ms <= 12920; ms += 20) c.updateIMU(upright, still, ms);
  assert(c.requestsFan());
  c.updateIMU({0.3f * upright.x, 0.3f * upright.y, 0}, still, 12940);
  assert(c.moving && !c.locked && c.requestsFan());

  const Vector3 directions[] = {
      {diagonal, -diagonal, 0}, {-diagonal, diagonal, 0}, {0, 0, 1}, {0, 0, -1},
  };
  for (const Vector3 direction : directions) {
    Control mounted({-1, -1, 0}, 20);
    mounted.gesture(true);
    mounted.updatePM(21);
    uint32_t now = 0;
    for (; now <= 3000; now += 20) mounted.updateIMU(upright, still, now);
    assert(mounted.tilt == 100 && mounted.sideTilt == 100 && mounted.requestsFan());

    // Check the boundary in both directions on each axis.
    const auto lean = [&](float degrees) {
      const float radians = degrees * 0.0174532925f;
      const float along = std::cos(radians);
      const float across = std::sin(radians);
      return Vector3{upright.x * along + direction.x * across,
                     upright.y * along + direction.y * across, direction.z * across};
    };
    for (int degrees = 1; degrees <= 31; ++degrees, now += 20) {
      mounted.updateIMU(lean(degrees), still, now);
      assert(!mounted.moving);
      assert(mounted.locked == (degrees > 30));
      assert(mounted.requestsFan() == (degrees <= 30));
    }
    const float changedTilt = direction.z != 0 ? mounted.tilt : mounted.sideTilt;
    assert(std::fabs(std::fabs(changedTilt - 100.0f) - 31.0f) < 0.05f);

    const uint32_t recoveryStart = now;
    for (; now <= recoveryStart + 3000; now += 20) {
      mounted.updateIMU(lean(30), still, now);
      assert(mounted.requestsFan() == (now == recoveryStart + 3000));
    }
    mounted.updateIMU(lean(30.01f), still, now);
    assert(mounted.locked && !mounted.moving && !mounted.requestsFan());
  }

  Control inverted({-1, -1, 0}, 20);
  inverted.gesture(true);
  inverted.updatePM(21);
  for (uint32_t ms = 0; ms <= 3200; ms += 20) inverted.updateIMU({diagonal, diagonal, 0}, still, ms);
  assert((inverted.tilt < 70 || inverted.tilt > 130) && inverted.locked && !inverted.moving);
  assert(!inverted.requestsFan());

  inverted.updateIMU({0, 0, -1}, still, 3220);
  assert(inverted.tilt == 180 && inverted.locked && !inverted.requestsFan());

  Control synced({-1, -1, 0}, 20);
  synced.gesture(true);
  synced.updatePM(21);
  for (uint32_t ms = 0; ms <= 3000; ms += 20) {
    synced.updateIMU(upright, still, ms, ms % 1000 == 0);
  }
  assert(synced.requestsFan());
  synced.updateIMU(tipped, still, 3020, false);
  assert(synced.requestsFan() && synced.tilt == 100);
  for (uint32_t ms = 3040; ms < 4000; ms += 20) synced.updateIMU(upright, still, ms, false);
  assert(synced.requestsFan());
  synced.updateIMU(tipped, still, 4000, true);
  assert(synced.tilt < 70 && !synced.requestsFan());
  assert(std::strcmp(synced.lockReason, "orientation") == 0);
  for (uint32_t ms = 4020; ms <= 8000; ms += 20) {
    synced.updateIMU(upright, {0, 0, 70}, ms, ms % 1000 == 0);
    assert(synced.requestsFan() == (ms == 8000));
  }
  assert(synced.moving && !synced.locked);
}
