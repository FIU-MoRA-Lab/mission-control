"""Platform input adapters for manual waypoint teleoperation."""

from __future__ import annotations

import threading
import queue
from boats.heron.backend.input.calibration import StickCalibration

try:
    import hid
    _HID_AVAILABLE = True
except ImportError:
    _HID_AVAILABLE = False


class JoystickReader(threading.Thread):
    """Read all stick axes live; sample left-stick throttle and right-stick steering for control."""

    def __init__(self, output_queue, live_callback=None):
        super().__init__(name="heron-gamepad", daemon=True)
        self.output_queue = output_queue
        self.live_callback = live_callback
        self.running = True
        self.error = ""
        self.calibration = StickCalibration()

    def _publish(self, axes):
        """Keep the latest axis state instead of queuing stale movements."""
        if self.live_callback:
            self.live_callback(*axes)
        try:
            self.output_queue.put_nowait(axes)
        except queue.Full:
            try:
                self.output_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.output_queue.put_nowait(axes)
            except queue.Full:
                pass

    @staticmethod
    def normalize_position(raw, invert=False):
        """Physical stick position before driving deadzone/gain, -1..1; Y is up."""
        if raw < 0 or raw > 255:
            value = raw / (32768 if raw < 0 else 32767)
        else:
            delta = raw - 128
            value = delta / (128 if delta < 0 else 127)
        value = max(-1.0, min(1.0, value))
        return -value if invert else value

    @staticmethod
    def decode_positions(data):
        return {
            name: JoystickReader.normalize_position(data[index], invert=index in (1, 3))
            for index, name in enumerate(("left_x", "left_y", "right_x", "right_y"))
        }

    @staticmethod
    def normalize_axis(raw, invert=False):
        """Legacy uncalibrated conversion; live driving uses StickCalibration."""
        if raw < 0 or raw > 255:
            value = raw / (32768 if raw < 0 else 32767)
        else:
            value = (raw - 128) / 128.0

        # Scale up slightly to ensure worn thumbsticks can easily reach 100%
        value = value * 1.1
        value = max(-1.0, min(1.0, value))

        if abs(value) < 0.08:
            value = 0.0
        if invert:
            value = -value
        return int(value * 1000)

    def run(self):
        if not _HID_AVAILABLE:
            self.error = "Joystick HID library unavailable; run uv sync and restart"
            self._publish((0, 0, "NO JOYSTICK"))
            return
        import time
        gamepad = None
        try:
            device_info = None
            for d in hid.enumerate():
                if "logitech" in d.get("manufacturer_string", "").lower() or "logitech" in d.get("product_string", "").lower() or d.get("vendor_id") == 0x046d:
                    device_info = d
                    break
            if not device_info:
                self.error = "No supported Logitech joystick detected"
                self._publish((0, 0, "NO JOYSTICK"))
                return

            gamepad = hid.device()
            gamepad.open_path(device_info["path"])
            gamepad.set_nonblocking(0)

            name = device_info.get("product_string", "Logitech Dual Action")
            self._publish((0, 0, f"GAMEPAD · {name}"))

            axes = {"throttle": 0, "yaw": 0}
            positions = dict.fromkeys(("left_x", "left_y", "right_x", "right_y"), 0.0)
            raw_axes = [128] * 4

            last_control_sample = 0.0
            while self.running:
                # Read each report as it arrives instead of accumulating a 20 Hz backlog.
                data = gamepad.read(64, 8)
                self.calibration.expire()
                if data and len(data) >= 4:
                    # Logitech Dual Action maps:
                    # data[0] = Left X, data[1] = Left Y
                    # data[2] = Right X, data[3] = Right Y
                    raw_axes = list(data[:4])
                    positions = self.decode_positions(data)
                    corrected = self.calibration.update(positions)
                    axes["throttle"] = int(max(-1.0, min(1.0, corrected["left_y"] * 1.1)) * 1000)
                    axes["yaw"] = int(max(-1.0, min(1.0, corrected["right_x"] * 1.1)) * 1000)
                if self.calibration.snapshot()["pending"]:
                    axes = {"throttle": 0, "yaw": 0}
                sample = (axes["throttle"], axes["yaw"], "GAMEPAD", positions.copy(), raw_axes.copy(), time.time_ns())
                if data and self.live_callback:
                    self.live_callback(*sample)
                # Control state/recording keep their 20 Hz cadence, independently of display.
                if time.monotonic() - last_control_sample >= 0.05:
                    self._publish(sample)
                    last_control_sample = time.monotonic()
        except Exception as exc:
            self.error = f"Joystick read failed: {exc}"
            self._publish((0, 0, "NO JOYSTICK"))
            return
        finally:
            if gamepad is not None:
                gamepad.close()


