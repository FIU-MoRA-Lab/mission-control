"""Manual axes, neutral handling, and the fixed-rate command stream."""
import threading

MAX_MANUAL_INPUT = 1000

class ManualControlState:
    """Manual axes and MAVLink streaming policy, independent of any UI toolkit."""

    def __init__(self, controller):
        self.controller = controller
        self.throttle = 0
        self.yaw = 0
        self.input_source = "NO JOYSTICK"
        self.neutral_ticks = 0
        self.suspended = False
        self._lock = threading.Lock()

    def suspend(self, active):
        """Allow one programmatic owner to replace the joystick output stream."""
        with self._lock:
            if self.suspended == active:
                return
            self.suspended = active
            self.throttle = self.yaw = 0
            self.neutral_ticks = 0

    @staticmethod
    def _clamp(value):
        return max(-MAX_MANUAL_INPUT, min(MAX_MANUAL_INPUT, int(value)))

    def set_axes(self, throttle, yaw, source):
        with self._lock:
            self.throttle = self._clamp(throttle)
            self.yaw = self._clamp(yaw)
            self.input_source = source
            self.neutral_ticks = 5

    def send_tick(self):
        if not self.controller.running or self.controller.mode_str != "MANUAL":
            return
        with self._lock:
            throttle, yaw = self.throttle, self.yaw
            if self.suspended:
                return
            if throttle or yaw:
                self.neutral_ticks = 5
            elif self.neutral_ticks > 0:
                self.neutral_ticks -= 1
            else:
                return
        self.controller.send_manual_control(throttle, yaw)

    def zero(self, source=None, send_neutral=True):
        with self._lock:
            self.throttle = self.yaw = 0
            if source is not None:
                self.input_source = source
            self.neutral_ticks = 5 if send_neutral else 0


class ManualControlStreamer(threading.Thread):
    """Keep manual MAVLink output at 20 Hz without depending on UI frame rate."""

    def __init__(self, session, interval=0.05):
        super().__init__(name="heron-manual-stream", daemon=True)
        self.session = session
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            self.session.manual.send_tick()
            self._stop_event.wait(self.interval)
