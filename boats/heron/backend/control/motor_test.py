"""Isolated, firmware-timed Rover bench tests; never steering/throttle mixing."""
import math
import time

from pymavlink import mavutil

from .adapter import Adapter


class MotorTestAdapter(Adapter):
    def __init__(self, *args):
        super().__init__(*args)
        self.started_at = None
        self.motor = None
        self.armed_seen = False
        self.deadline = 0

    def check_live(self):
        c = self.controller
        if (c.autopilot_type != mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA
                or c.vehicle_type not in {mavutil.mavlink.MAV_TYPE_GROUND_ROVER,
                                          mavutil.mavlink.MAV_TYPE_SURFACE_BOAT}):
            raise ValueError("Isolated motor tests require an ArduRover vehicle")
        if not c.connected or c._last_vehicle_heartbeat is None or not c.check_vehicle_link():
            raise ValueError("Motor test requires a fresh vehicle heartbeat")
        if c._confirmed_mode != "MANUAL":
            raise ValueError("Motor test requires confirmed MANUAL mode")
        if self.started_at is not None:
            ack = c._motor_test_ack
            if ack and ack[1] >= self.started_at:
                if ack[0] != mavutil.mavlink.MAV_RESULT_ACCEPTED:
                    raise ValueError(f"Vehicle rejected motor test (result {ack[0]})")
            elif time.monotonic() - self.started_at > 2:
                raise ValueError("Vehicle did not acknowledge motor test")
            if c._confirmed_armed:
                self.armed_seen = True
            elif self.armed_seen and time.monotonic() < self.deadline:
                raise ValueError("Motor test was disarmed")

    def start(self, target, duration):
        self.check_live()
        c = self.controller
        if c._confirmed_armed:
            raise ValueError("Disarm before starting an isolated motor test")
        if self.started_at is not None:
            raise ValueError("Use a new scope for each isolated motor test")
        left, right = target["left"], target["right"]
        if bool(left) == bool(right):
            raise ValueError("An isolated motor test needs exactly one nonzero thruster")
        # Rover motor-test order is NOT the servo channel number: 3=left, 4=right.
        self.motor = 3 if left else 4
        percent = (left or right) * 100
        if not math.isclose(percent, round(percent), rel_tol=0, abs_tol=1e-9):
            raise ValueError("Motor test percentage must be a whole number")
        if not 0 < duration <= 30:
            raise ValueError("Motor test duration must be positive and at most 30 seconds")
        self._command(round(percent), duration)

    def _command(self, percent, duration):
        c = self.controller
        with c._mav_send_lock:
            if duration and (c.emergency_stopped or not c.running):
                raise ValueError("Motor test blocked by kill switch or shutdown")
            if duration:
                self.started_at = time.monotonic()
                self.deadline = self.started_at + duration
            c.transport.send("command_long", c.master.target_system,
                             c.autopilot_component or 1,
                             mavutil.mavlink.MAV_CMD_DO_MOTOR_TEST, 0,
                             self.motor, 0, percent, duration, 0, 0, 0)

    def apply(self, target, dt):
        # The firmware owns the finite test. Never refresh it or send mixed axes.
        if not target["left"] and not target["right"]:
            if self.started_at is not None:
                ack = self.controller._motor_test_ack
                if not ack or ack[1] < self.started_at:
                    raise ValueError("Vehicle did not acknowledge motor test")
            self.neutral()
        else:
            self.check_live()

    def neutral(self):
        if self.started_at is not None:
            # A zero timeout stops Rover's motor-test mode (which also disarms).
            # Clear only after a successful write so a failed send can be retried.
            # After natural expiry, another DO_MOTOR_TEST could start a NEW
            # test and briefly rearm. Let the onboard timeout finish instead.
            if time.monotonic() < self.deadline:
                self._command(0, 0)
            self.started_at = None
        super().neutral()
