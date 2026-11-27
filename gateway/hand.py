"""Classify a single hand from the MediaPipe 21-point world landmarks."""

import math

OPEN_PALM = "OPEN_PALM"
FIST = "FIST"
NO_HAND = "NO_HAND"
OTHER = "OTHER"
HOLD_SECONDS = 3.0

FINGERS = ((5, 6, 8), (9, 10, 12), (13, 14, 16), (17, 18, 20))
CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 9), (9, 13), (13, 17), (17, 0),
    (5, 6), (6, 7), (7, 8), (9, 10), (10, 11), (11, 12), (13, 14), (14, 15),
    (15, 16), (17, 18), (18, 19), (19, 20),
)


def joint_angle(a, joint, b):
    left = tuple(x - y for x, y in zip(a, joint))
    right = tuple(x - y for x, y in zip(b, joint))
    scale = math.hypot(*left) * math.hypot(*right)
    if scale < 1e-9:
        return None
    cosine = sum(x * y for x, y in zip(left, right)) / scale
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def classify(landmarks):
    if len(landmarks) != 21:
        return OTHER
    points = [(p.x, p.y, p.z) for p in landmarks]
    palm = math.dist(points[0], points[9])
    if palm < 1e-5 or not all(math.isfinite(v) for point in points for v in point):
        return OTHER

    angles = [joint_angle(points[base], points[joint], points[tip])
              for base, joint, tip in FINGERS]
    if any(angle is None for angle in angles):
        return OTHER
    tip_distances = [math.dist(points[0], points[tip]) / palm for _, _, tip in FINGERS]
    thumb_spread = math.dist(points[4], points[5]) / palm
    if all(angle >= 145 for angle in angles) and min(tip_distances) > 1.35 and thumb_spread > 0.42:
        return OPEN_PALM
    if all(angle <= 115 for angle in angles) and max(tip_distances) < 1.35:
        return FIST
    return OTHER


class HoldGate:
    """Send one request per stable pose and allow direct switches between poses."""

    def __init__(self):
        self.candidate = None
        self.started = 0.0
        self.frames = 0
        self.released_at = None
        self.last_frame = None
        self.last_emitted = None

    def update(self, label, now):
        if self.last_frame is not None and now - self.last_frame > 0.35:
            self.candidate = None
            self.released_at = None
        self.last_frame = now
        if label not in (OPEN_PALM, FIST):
            self.candidate = None
            if label != NO_HAND:
                self.released_at = None
            elif self.released_at is None:
                self.released_at = now
            elif now - self.released_at >= 0.35:
                self.last_emitted = None
            return None

        self.released_at = None
        if label == self.last_emitted:
            self.candidate = None
            return None
        if label != self.candidate:
            self.candidate, self.started, self.frames = label, now, 0
        self.frames += 1
        if self.frames >= 4 and now - self.started >= HOLD_SECONDS:
            self.last_emitted = label
            return "ON" if label == OPEN_PALM else "OFF"
        return None
