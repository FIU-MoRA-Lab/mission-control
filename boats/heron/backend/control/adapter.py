"""Normalized controls to existing MAVLink inputs, or a kinematic simulation."""
import math
import time
from boats.heron.backend.mapping.constraints import waypoint
from .planning import Waypoint

# Optional parameter-inspection API only; not used by the control adapter.
PARAMETERS = ("PILOT_STEER_TYPE", "SERVO1_FUNCTION", "SERVO3_FUNCTION", "RCMAP_ROLL", "RCMAP_THROTTLE",
              "RC1_MIN", "RC1_TRIM", "RC1_MAX", "RC1_REVERSED", "RC1_DZ",
              "RC3_MIN", "RC3_TRIM", "RC3_MAX", "RC3_REVERSED", "RC3_DZ")


class Adapter:
    def __init__(self, controller, mode, position, heading):
        self.controller,self.mode = controller,mode
        self.position,self.heading,self.speed = position,heading,0.0
        self.navigation = False
        self.testing_without_gps = False

    def state(self):
        if self.mode == "live":
            self.check_live()
            c = self.controller
            if self.testing_without_gps:
                values = [float(v) if isinstance(v,(int,float)) and math.isfinite(v) else 0.0 for v in (c.lat,c.lon,c.heading,c.groundspeed)]
                return Waypoint(values[0],values[1]),values[2],values[3]
            return Waypoint(c.lat,c.lon),c.heading,c.groundspeed
        return self.position,self.heading,self.speed

    def check_live(self):
        c = self.controller
        now = time.monotonic()
        if not c.connected or c._last_vehicle_heartbeat is None or not c.check_vehicle_link(now):
            raise ValueError("Live control requires a fresh vehicle heartbeat")
        allowed_modes = {"MANUAL", "AUTO"} if self.navigation else {"MANUAL"}
        if not c._confirmed_armed or c._confirmed_mode not in allowed_modes:
            raise ValueError(f"Live control requires confirmed MANUAL/armed state (mode={c._confirmed_mode}, armed={c._confirmed_armed})")
        if self.testing_without_gps:
            return
        if c.fix_type < 3:
            raise ValueError("GPS fix lost")
        if now-getattr(c,"_last_position_at",0) > 1 or now-getattr(c,"_last_heading_at",0) > 1:
            raise ValueError("Live position or heading is stale")
        if not all(math.isfinite(v) for v in (c.lat,c.lon,c.heading,c.groundspeed)):
            raise ValueError("Invalid vehicle state")

    def apply(self, target, dt):
        kind = target["kind"]
        if kind == "axes":
            throttle,steering = target["throttle"],target["steering"]
        else:
            throttle = (target["left"]+target["right"])/2
            steering = (target["left"]-target["right"])/2
        if self.mode == "simulation":
            self.speed = throttle*2
            self.heading = (self.heading+steering*120*dt)%360
            angle = math.radians(self.heading)
            self.position = waypoint((math.sin(angle)*self.speed*dt,math.cos(angle)*self.speed*dt),self.position)
            return
        self.check_live()
        # Raw standard Rover throttle/steering input. Reverse steering is
        # compensated for PILOT_STEER_TYPE=0; no RC calibration is read/applied.
        roll = -steering if throttle < 0 else steering
        self._send_raw(throttle, roll)

    def _send_raw(self, throttle, steering):
        throttle_input = round(max(-1, min(1, throttle))*1000)
        steering_input = round(max(-1, min(1, steering))*1000)
        c = self.controller
        with c._mav_send_lock:
            if c.emergency_stopped and (throttle_input or steering_input):
                raise ValueError("Kill switch is engaged")
            if not c.running and (throttle_input or steering_input):
                raise ValueError("Ground station is shutting down")
            # ArduRover consumes MANUAL_CONTROL.y (steering) and .z (throttle).
            c.transport.send("manual_control", c.master.target_system,
                             0, steering_input, throttle_input, 0, 0)
        c.last_manual_send = dict(time=time.monotonic(), source="SDK", sent=True,
                                 error="", throttle=throttle_input, yaw=steering_input,
                                 chan1_raw=None, chan3_raw=None)

    def neutral(self):
        if self.mode == "live":
            self._send_raw(0, 0)
        self.speed = 0.0
