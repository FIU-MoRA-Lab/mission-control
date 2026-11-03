"""Normalized controls to existing MAVLink inputs, or a kinematic simulation."""
import math
import time
from backend.mapping.constraints import waypoint
from .planning import Waypoint

PARAMETERS = ("PILOT_STEER_TYPE", "SERVO1_FUNCTION", "SERVO3_FUNCTION", "RCMAP_ROLL", "RCMAP_THROTTLE",
              "RC1_MIN", "RC1_TRIM", "RC1_MAX", "RC1_REVERSED", "RC3_MIN", "RC3_TRIM", "RC3_MAX", "RC3_REVERSED")


class Adapter:
    def __init__(self, controller, mode, position, heading):
        self.controller,self.mode = controller,mode
        self.position,self.heading,self.speed = position,heading,0.0
        self.navigation = False
        self.testing_without_gps = False
        self._verified_mapping = None

    def state(self):
        if self.mode == "live":
            self.check_live()
            c = self.controller
            if self.testing_without_gps:
                values = [v if isinstance(v,(int,float)) and math.isfinite(v) else 0.0 for v in (c.lat,c.lon,c.heading,c.groundspeed)]
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

    def parameters(self, kind):
        c = self.controller
        if self._verified_mapping is not None:
            for key,value in self._verified_mapping.items():
                current = c._control_parameters.get(key)
                if current is not None and current[0] != value:
                    raise ValueError("Vehicle mapping changed during control: "+key)
            return
        values = {}
        for key in PARAMETERS:
            entry = c._control_parameters.get(key)
            if entry is None or time.monotonic()-entry[1] > 60:
                raise ValueError("Call mc.read_parameters() before live control; vehicle mapping is not verified")
            values[key] = entry[0]
        if values["PILOT_STEER_TYPE"] not in {0,1}:
            raise ValueError("Supported PILOT_STEER_TYPE values are 0 (throttle/steering) and 1 (two paddles)")
        expected = {"SERVO1_FUNCTION":73,"SERVO3_FUNCTION":74,"RCMAP_ROLL":1,"RCMAP_THROTTLE":3}
        for key,value in expected.items():
            if values[key] != value:
                raise ValueError(f"This live adapter requires {key}={value}; got {values[key]}. Vehicle parameters are never changed automatically")
        for channel in (1,3):
            low,trim,high = (values[f"RC{channel}_{suffix}"] for suffix in ("MIN","TRIM","MAX"))
            if not all(math.isfinite(v) for v in (low,trim,high)) or not 800 <= low < trim < high <= 2200:
                raise ValueError(f"Invalid RC{channel} min/trim/max calibration")
            if values[f"RC{channel}_REVERSED"] not in {0,1}:
                raise ValueError(f"Invalid RC{channel} reversal setting")
        self._verified_mapping = dict(values)

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
        self.parameters(kind)
        mapping = self._verified_mapping or {key:entry[0] for key,entry in self.controller._control_parameters.items()}
        if mapping["PILOT_STEER_TYPE"] == 0:
            # Inverse of Rover's differential mixer. Default pilot input reverses
            # steering when backing up, so compensate for that here as well.
            roll = -steering if throttle < 0 else steering
            throttle_input = throttle
        else:
            if kind == "thrusters":
                roll,throttle_input = target["left"],target["right"]
            else:
                left,right = throttle+steering,throttle-steering
                scale = max(1,abs(left),abs(right))
                roll,throttle_input = left/scale,right/scale
        self._send_rc(roll,throttle_input,mapping)

    def _send_rc(self, roll, throttle_input, mapping):
        def pwm(channel, value):
            prefix = f"RC{channel}_"
            low,trim,high = (mapping[prefix+name] for name in ("MIN","TRIM","MAX"))
            if mapping[prefix+"REVERSED"]: value = -value
            return round(trim+value*((high-trim) if value >= 0 else (trim-low)))
        ch1,ch3 = pwm(1,roll),pwm(3,throttle_input)
        c = self.controller
        with c._mav_send_lock:
            c.transport.send("rc_channels_override",c.master.target_system,c.master.target_component,
                             ch1,0,ch3,0,0,0,0,0)
        c.last_manual_send = dict(time=time.monotonic(),source="SDK",sent=True,error="",
                                 throttle=round(throttle_input*1000),yaw=round(roll*1000),chan1_raw=ch1,chan3_raw=ch3)

    def neutral(self):
        if self.mode == "live":
            if self._verified_mapping and self.controller.master:
                self._send_rc(0,0,self._verified_mapping)
            else:
                self.controller.send_manual_control(0,0)
        self.speed = 0.0
