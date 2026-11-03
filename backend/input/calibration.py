"""Bounded idle calibration for normalized, two-stick HID input."""
import threading
import time


class StickCalibration:
    # Automatic learning stays inside the neutral region. Larger offsets need
    # an explicit release-and-calibrate action; a held stick is indistinguishable.
    AUTO_LIMIT = 0.12
    MANUAL_LIMIT = 0.30
    STABLE_SECONDS = 1.0
    JITTER = 0.016
    AXES = ("left_x", "left_y", "right_x", "right_y")

    def __init__(self):
        self._lock = threading.Lock()
        self.centers = [0.0] * 4
        self.deadzones = [self.AUTO_LIMIT] * 4
        self._windows = [None, None]
        self._last_sample = None
        self._requested_at = None
        self.status = "Auto calibration · release sticks to learn center"
        self.corrected = dict.fromkeys(self.AXES, 0.0)

    def request(self, now=None):
        with self._lock:
            self._requested_at = time.monotonic() if now is None else now
            self._windows = [None, None]
            self.status = "Release both sticks · hold still for 1 second"

    def snapshot(self):
        with self._lock:
            return {"status": self.status, "pending": self._requested_at is not None,
                    "centers": dict(zip(self.AXES, self.centers)),
                    "deadzones": dict(zip(self.AXES, self.deadzones)),
                    "corrected": dict(self.corrected)}

    def expire(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._requested_at is not None and now - self._requested_at > 10.0:
                self._requested_at = None
                self._windows = [None, None]
                self.status = "Calibration timed out · release sticks and retry"

    def update(self, positions, now=None):
        now = time.monotonic() if now is None else now
        values = [positions[name] for name in self.AXES]
        self.expire(now)
        with self._lock:
            manual = self._requested_at is not None
            if self._last_sample is not None and now - self._last_sample > 0.4:
                self._windows = [None, None]
            self._last_sample = now
            limit = self.MANUAL_LIMIT if manual else self.AUTO_LIMIT
            ready = []
            for stick in range(2):
                pair = values[stick * 2:stick * 2 + 2]
                window = self._windows[stick]
                if any(abs(value) > limit for value in pair):
                    self._windows[stick] = None
                    continue
                if window is None or any(abs(value - anchor) > self.JITTER
                                         for value, anchor in zip(pair, window[1])):
                    window = [now, pair, pair.copy(), pair.copy()]
                    self._windows[stick] = window
                window[2] = [min(a, b) for a, b in zip(window[2], pair)]
                window[3] = [max(a, b) for a, b in zip(window[3], pair)]
                if now - window[0] >= self.STABLE_SECONDS:
                    ready.append(stick)
            if not manual or len(ready) == 2:
                for stick in ready:
                    window = self._windows[stick]
                    for local in range(2):
                        axis = stick * 2 + local
                        low, high = window[2][local], window[3][local]
                        self.centers[axis] = (low + high) / 2
                        self.deadzones[axis] = min(0.12, 0.08 + (high - low) / 2)
                if manual and len(ready) == 2:
                    self._requested_at = None
                    self._windows = [None, None]
                    self.status = "Calibrated · auto correction active"
                elif ready:
                    self.status = "Auto correction active"
            corrected = []
            for value, center, zone in zip(values, self.centers, self.deadzones):
                delta = value - center
                travel = (1 - center) if delta >= 0 else (1 + center)
                magnitude = max(0.0, (abs(delta) - zone) / (travel - zone))
                corrected.append(max(-1.0, min(1.0, magnitude if delta >= 0 else -magnitude)))
            self.corrected = dict(zip(self.AXES, corrected))
            # Manual calibration never produces driving input while learning.
            return dict.fromkeys(self.AXES, 0.0) if self._requested_at is not None else dict(self.corrected)
