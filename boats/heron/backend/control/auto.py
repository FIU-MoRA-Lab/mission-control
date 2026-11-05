"""Verified AUTO waypoint transfers. Upload work stays outside the command loop."""
from dataclasses import asdict
import math
import threading
import time
from .planning import Waypoint
from boats.heron.backend.mapping.constraints import xy, waypoint


class AutoTransfer:
    def __init__(self, service, destination, speed, arrival, heading):
        self.service,self.destination = service,destination
        self.speed,self.arrival,self.heading = speed,arrival,heading
        self.phase = "uploading" if service.adapter.mode == "live" else "travel"
        self.error = None
        self.cancelled = threading.Event()
    def start(self):
        if self.phase == "uploading":
            self.worker = threading.Thread(target=self._upload,name="sdk-auto-upload",daemon=True)
            self.service.auto_workers.append(self.worker)
            self.worker.start()

    def _valid(self):
        return not self.cancelled.is_set() and self.service.navigator is self and self.service.owner is not None

    def _upload(self):
        s,c = self.service,self.service.runtime.controller
        saved_points,saved_speed = None,None
        try:
            with s.runtime._commands:
                if not self._valid(): return
                with c._wp_lock:
                    saved_points,saved_speed = c.waypoints,c.mission_speed
                    c.waypoints = [dict(asdict(self.destination),id=1,alt=0)]
                    c.mission_speed = self.speed
                c.is_uploading_mission = True
            if not c.upload_mission(): raise ValueError("AUTO transfer mission upload failed")
            with s.runtime._commands:
                if not self._valid(): return
                if not c.connected or not c._confirmed_armed: raise ValueError("Vehicle lost link or disarmed before AUTO transfer")
                s.adapter.neutral()
                c.transport.send("mission_set_current",c.master.target_system,c.autopilot_component or 1,1)
                if not c.set_mode("AUTO"): raise ValueError("AUTO mode request failed")
                self.phase = "confirming_auto"
                self.mode_deadline = time.monotonic()+8
        except Exception as exc:
            self.error = str(exc)
        finally:
            with s.runtime._commands:
                if saved_points is not None:
                    with c._wp_lock:
                        c.waypoints,c.mission_speed = saved_points,saved_speed
                c.is_uploading_mission = False
                c.mission_uploaded = False  # The autopilot now holds the transfer mission.

    def step(self, dt):
        s,a = self.service,self.service.adapter
        if self.error: raise ValueError(self.error)
        position,heading,speed = a.state()
        distance = math.hypot(*xy(self.destination,position))
        if self.phase == "uploading": return False
        if self.phase == "confirming_auto":
            if a.controller._confirmed_mode == "AUTO": self.phase = "travel"
            elif time.monotonic() >= self.mode_deadline: raise ValueError("Vehicle did not confirm AUTO mode")
            return False
        if self.phase == "travel":
            if a.mode == "live" and a.controller._confirmed_mode != "AUTO": raise ValueError("AUTO transfer interrupted by mode change")
            if distance > self.arrival:
                if a.mode == "simulation":
                    dx,dy = xy(self.destination,position)
                    step = min(distance,self.speed*dt)
                    a.position = waypoint((dx/distance*step,dy/distance*step),position)
                    a.heading = math.degrees(math.atan2(dx,dy))%360
                    a.speed = self.speed
                return False
            if a.mode == "live":
                if not a.controller.set_mode("MANUAL"): raise ValueError("Could not restore MANUAL after AUTO transfer")
                self.phase = "confirming_manual"
                self.mode_deadline = time.monotonic()+8
                return False
            self.phase = "align"
        if self.phase == "confirming_manual":
            if a.controller._confirmed_mode == "MANUAL": self.phase = "align"
            elif time.monotonic() >= self.mode_deadline: raise ValueError("Vehicle did not confirm MANUAL after AUTO transfer")
            return False
        if self.phase == "align":
            if self.heading is not None:
                error = (self.heading-a.state()[1]+180)%360-180
                if abs(error)>3:
                    turn = max(-.3,min(.3,error/90))
                    a.apply({"kind":"thrusters","left":turn,"right":-turn},dt)
                    s.owner.update(left=turn,right=-turn,throttle=0.0,requested_throttle=0.0,steering=turn)
                    return False
            a.neutral()
            return True
        return False

    def cancel(self):
        self.cancelled.set()
        if self.service.adapter.mode == "live" and self.phase in {"confirming_auto","travel"}:
            self.service.runtime.controller.set_mode("MANUAL")
