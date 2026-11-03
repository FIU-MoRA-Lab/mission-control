"""One control owner, expiring commands, route execution and scoped recordings.

Public methods and tick run under ControllerRuntime._commands. Recording callbacks
only enqueue traffic; finalization runs off the control thread.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time
from uuid import uuid4
from backend.mapping.constraints import OperatingArea, xy
from backend.paths import data_directory
from .adapter import Adapter, PARAMETERS
from .navigation import RouteFollower
from .planning import Plan, Waypoint, number
from .recording import RunRecording
from .profile import InputProfile
from .auto import AutoTransfer
from .storage import RunStore, save_json, stamp


class ControlService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.owner = None
        self.last = None
        self.target = None
        self.recording = None
        self.manifest = None
        self.store = None
        self.finalizers = []
        self._last_tick = time.monotonic()
        self._neutral_until = 0
        self._neutral_adapter = None
        self._record_failure = None
        self.navigator = None
        self.auto_workers = []
        self._route_file = None
        self._routes = None
        runtime.controller.recorder.subscribe(self._copy_record)

    @property
    def busy(self):
        return self.owner is not None

    def _copy_record(self, record):
        recording = self.recording
        if recording is not None:
            recording.copy(record)

    @property
    def no_gps(self):
        return bool(getattr(self.runtime,"testing_without_gps",False))

    def area(self):
        return OperatingArea(self.runtime.geometry.lake_geojson())

    def state(self):
        return {"testing_without_gps":self.no_gps,"neutralizing": time.monotonic() < self._neutral_until,
                "control":dict(self.owner) if self.owner else self.last,
                "recording":dict(self.manifest) if self.manifest else None}

    def _write(self, data):
        if self.recording and self.manifest["status"] != "finishing":
            self.recording.log(data)
        else:
            from .recording import control_record
            try:
                self.runtime.controller.recorder.write(control_record(data))
            except RuntimeError:
                pass

    def acquire(self, data):
        if self.owner or self._neutral_until > time.monotonic():
            raise ValueError("Another controller owns the vehicle or is neutralizing")
        c = self.runtime.controller
        if c.mission_started or c.is_starting_mission or c.is_uploading_mission or c.simulating_mission:
            raise ValueError("Stop the current mission before acquiring SDK control")
        mode = data.get("mode","simulation")
        if mode not in {"simulation","live"}:
            raise ValueError("Choose simulation or live")
        allowed_mode = getattr(self.runtime, "execution_mode", None)
        if allowed_mode and mode != allowed_mode:
            raise ValueError(f"This backend is reserved for {allowed_mode} execution")
        if mode == "simulation" and (c.quit_is_armed or self.runtime._manual_active):
            raise ValueError("Disarm and pause joystick input before SDK simulation")
        kind = data.get("kind","axes")
        if kind not in {"axes","thrusters"}:
            raise ValueError("Choose axes or thrusters control")
        points = self.runtime.session.waypoints_snapshot()
        start = data.get("start") or (points[0] if points else {"lat":c.lat,"lon":c.lon})
        position = Waypoint.from_dict(start)
        if mode == "simulation" and self.last and self.last["mode"] == mode:
            position = Waypoint(self.last["lat"],self.last["lon"])
        heading = number(data.get("heading_deg",c.heading),0,360,"heading")
        area = self.area()
        if mode == "simulation" and self.last and self.last["mode"] == mode:
            heading = self.last["heading"]
        adapter = Adapter(c,mode,position,heading)
        adapter.testing_without_gps = self.no_gps
        position,_,_ = adapter.state()
        if mode == "live":
            adapter.parameters(kind)
        if not self.no_gps and area.segment_issue(position,position):
            raise ValueError("Control must start inside the lake with configured clearance; select a start point on the map")
        self.runtime._manual_active = False
        self.runtime.session.manual.suspend(True)
        adapter.neutral()
        self.adapter,self._area = adapter,area
        self.follower = None
        self.profile = None
        self._owns_recording = False
        self.owner = dict(id=uuid4().hex,status="active",mode=mode,kind=kind,started_at=stamp(),
                          lat=position.lat,lon=position.lon,heading=heading,speed=0.0,
                          requested_throttle=0.0,throttle=0.0,steering=0.0,left=0.0,right=0.0,
                          area_revision=area.revision,gps_bypass=self.no_gps,geofence_enabled=not self.no_gps,reason="")
        if data.get("end") and not self.no_gps:
            endpoint = Waypoint.from_dict(data["end"])
            issue = area.segment_issue(position,endpoint)
            if issue:
                self.release(self.owner["id"],"failed","Invalid experiment corridor")
                raise ValueError("Experiment corridor crosses a safety boundary: "+issue)
            self.owner["waypoints"] = [asdict(position),asdict(endpoint)]
        self.target = None
        self._lease = time.monotonic()+2
        self._started = time.monotonic()
        self._last_tick = self._started
        self._owns_recording = False
        self._execution = False
        if self.recording and self.manifest["status"] != "finishing":
            self.manifest["mode"] = mode
        self._write({"event":"control_acquired",**self.owner})
        return dict(self.owner)

    def require(self, token):
        if self.owner is None or token != self.owner["id"]:
            raise ValueError("Control session is no longer active")

    def set_target(self, data):
        self.require(data.get("id"))
        if self._execution:
            raise ValueError("Cancel the saved run before sending direct controls")
        kind = self.owner["kind"]
        keys = ("throttle","steering") if kind == "axes" else ("left","right")
        if set(data.get("target",{})) != set(keys):
            raise ValueError(f"This session needs {', '.join(keys)}")
        target = {key:number(data["target"][key],-1,1,key) for key in keys}
        valid_for = number(data.get("valid_for_s",0.25),0.05,1,"command lifetime")
        self.target = {"kind":kind,**target}
        self.profile = None
        self.follower = None
        self._lease = time.monotonic()+valid_for
        return {"ok":True}

    def play(self, data):
        self.require(data.get("id"))
        if self._execution or self.navigator:
            raise ValueError("Another motion executor is active")
        keys = ("throttle","steering") if self.owner["kind"] == "axes" else ("left","right")
        profile = InputProfile(data["keyframes"],keys)
        if self.no_gps and profile.duration > 60:
            raise ValueError("No-GPS test profiles must not exceed 60 seconds")
        self.profile = profile
        self.profile_started = time.monotonic()
        self.follower = None
        self.owner.update(profile_id=uuid4().hex,profile_complete=False)
        self._lease = self.profile_started+profile.duration+1
        self._write({"event":"input_profile","keyframes_json":json.dumps(data["keyframes"]),"id":self.owner["id"]})
        return {"profile_id":self.owner["profile_id"]}

    def navigate(self, data):
        if self.no_gps: raise ValueError("Waypoint/AUTO navigation is disabled in no-GPS motor test mode")
        destination = Waypoint.from_dict(data["destination"])
        speed = number(data.get("speed_mps",1),.1,2,"transfer speed")
        arrival = number(data.get("arrival_radius_m",.5),.2,2,"arrival radius")
        heading = data.get("heading_deg")
        if heading is not None: heading = number(heading,0,360,"arrival heading")
        info = self.acquire({"mode":data.get("mode","simulation"),"kind":"thrusters", "start":data["destination"],"heading_deg":heading or 0})
        try:
            position,_,_ = self.adapter.state()
            issue = self._area.segment_issue(position,destination)
            if issue: raise ValueError("AUTO transfer crosses operating-area constraint: "+issue)
            self.adapter.navigation = True
            self._lease = time.monotonic()+number(data.get("timeout_s",180),1,600,"transfer timeout")
            self.owner["waypoints"] = [asdict(position),asdict(destination)]
            self.owner["navigation"] = "AUTO"
            self.navigator = AutoTransfer(self,destination,speed,arrival,heading)
            self.navigator.start()
            self._write({"event":"auto_transfer_started","destination":data["destination"],"id":info["id"]})
            return info
        except Exception:
            self.release(info["id"],"failed","AUTO transfer setup failed")
            raise

    def follow(self, data):
        if self.no_gps: raise ValueError("Waypoint/AUTO navigation is disabled in no-GPS motor test mode")
        self.require(data.get("id"))
        if self.owner["kind"] != "axes" or self._execution:
            raise ValueError("Streaming paths need an axes control session, separate from a saved run")
        plan = Plan.from_dict(data["plan"])
        lifetime = number(data.get("valid_for_s",0.5),0.05,5,"path lifetime")
        report = self._area.validate(plan)
        if not report["valid"]:
            raise ValueError("Plan rejected: " + json.dumps(report["issues"]))
        if plan.legs[0].throttle.start == 0 and plan.launch_throttle == 0:
            raise ValueError("A zero-start path needs explicit launch_throttle, or start above zero")
        position,_,_ = self.adapter.state()
        if self._area.segment_issue(position,plan.waypoints[0]):
            raise ValueError("The approach to the replacement path crosses an operating-area constraint")
        self.follower = RouteFollower(plan)
        self.profile = None
        self.target = None
        self._lease = time.monotonic()+lifetime
        self.owner["waypoints"] = [asdict(p) for p in plan.waypoints]
        self._write({"event":"path_replaced","plan":plan.to_dict(),"control_id":self.owner["id"]})
        return {"ok":True}

    def execute(self, data):
        if self.no_gps: raise ValueError("Waypoint/AUTO navigation is disabled in no-GPS motor test mode")
        plan = Plan.from_dict(data["plan"])
        area = self.area()
        report = area.validate(plan)
        if not report["valid"]:
            raise ValueError("Plan rejected: "+json.dumps(report["issues"]))
        if plan.legs[0].throttle.start == 0 and plan.launch_throttle == 0:
            raise ValueError("A stationary zero-start route needs explicit launch_throttle, or start above zero")
        if self.owner or self.recording and self.manifest["status"] == "finishing":
            raise ValueError("Finish current control and recording finalization before executing a path")
        heading = math.degrees(math.atan2(*xy(plan.waypoints[1],plan.waypoints[0])))%360
        control = self.acquire({"mode":data.get("mode","simulation"),"start":asdict(plan.waypoints[0]),"heading_deg":heading})
        position,_,_ = self.adapter.state()
        if math.dist(xy(position,plan.waypoints[0]),(0,0)) > plan.arrival_radius_m:
            self.release(control["id"],"failed","Vehicle must be at the first waypoint before execution")
            raise ValueError("Vehicle must be at the first waypoint before execution")
        try:
            owns_recording = self.recording is None
            recording = self.begin_recording({**data,"plan":plan.to_dict()}) if owns_recording else dict(self.manifest)
        except Exception:
            self.release(control["id"],"failed","Recording could not start")
            raise
        self._owns_recording = owns_recording
        self._execution = True
        save_json(Path(recording["directory"])/f"plan-{control['id']}.json",plan.to_dict())
        self.owner["waypoints"] = [asdict(p) for p in plan.waypoints]
        self.follower = RouteFollower(plan)
        self._lease = self._started+plan.max_duration_s
        self.owner["run_id"] = recording["id"]
        self._write({"event":"plan_started","plan":plan.to_dict(),"run_id":recording["id"]})
        return {**recording,"control_id":control["id"],"owns_recording":owns_recording}

    def release(self, token=None, status="cancelled", reason="Released by client"):
        if not self.owner:
            return {"ok":True}
        if token is not None and token != self.owner["id"]:
            return {"ok":True}
        owner = self.owner
        if self.navigator:
            self.navigator.cancel()
            self.navigator = None
        # Revoke ownership before any further calculation or transmission.
        self.owner = None
        self.target = None
        self.follower = None
        self.profile = None
        self._neutral_adapter = self.adapter
        self._neutral_until = time.monotonic()+0.3 if owner["mode"] == "live" else 0
        try:
            self.adapter.neutral()
        except Exception as exc:
            reason += f"; neutral transmission failed: {exc}"
        owner.update(status=status,reason=reason,ended_at=stamp(),requested_throttle=0.0,throttle=0.0,steering=0.0,left=0.0,right=0.0,speed=0.0 if owner["mode"] == "simulation" else owner["speed"])
        self.last = owner
        if self.recording and (status == "failed" or reason.startswith("Operator")):
            self._record_failure = (status, reason)
        if owner["mode"] == "simulation":
            self.runtime.session.manual.suspend(False)
        self._write({"event":"control_ended",**owner})
        if self._owns_recording:
            self.end_recording(status,reason)
        return {"ok":True}

    def takeover(self, reason):
        self.release(reason=reason)
        self._neutral_until = 0
        self.runtime.session.manual.suspend(False)

    def display_state(self, snapshot):
        control = self.owner or self.last
        if not control:
            return snapshot
        if control["mode"] == "simulation" and (self.owner or not snapshot["connected"] and not snapshot["started"]):
            snapshot.update(lat=control["lat"],lon=control["lon"],heading=control["heading"],speed=control["speed"],
                            simulating=True, started=self.busy, mode="SIMULATION")
        if self.owner:
            snapshot["current_wp"] = control.get("leg",0)
            if "waypoints" in control:
                snapshot["waypoints"] = control["waypoints"]
            throttle = control.get("throttle",0) if control["kind"] == "axes" else (control["left"]+control["right"])/2
            steering = control.get("steering",0) if control["kind"] == "axes" else (control["left"]-control["right"])/2
            snapshot["manual"] = dict(snapshot["manual"],throttle=throttle*1000,yaw=steering*1000,source="SDK",active=False)
        return snapshot

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        dt = min(max(0,now-self._last_tick),0.1)
        self._last_tick = now
        if self._neutral_until > now:
            try:
                self._neutral_adapter.neutral()
            except Exception:
                pass
        if not self.owner:
            if self._neutral_until <= now:
                self.runtime.session.manual.suspend(False)
            return
        if self.no_gps and now-self._started >= 60:
            self.release(status="failed",reason="No-GPS motor test reached the 60 s control limit")
            return
        if now >= self._lease:
            self.release(status="failed",reason="Control command or plan expired")
            return
        try:
            position,heading,speed = self.adapter.state()
            if not self.no_gps and self.runtime.geometry.safety_padding_m != self._area.padding:
                raise ValueError("Operating area changed")
            if not self.no_gps and self._area.segment_issue(position,position):
                raise ValueError("Vehicle left the permitted operating area")
            if self.navigator:
                if self._area.segment_issue(position,self.navigator.destination):
                    raise ValueError("AUTO transfer left the safe corridor")
                completed = self.navigator.step(dt)
                position,heading,speed = self.adapter.state()
                self.owner.update(lat=position.lat,lon=position.lon,heading=heading,speed=speed,navigation_phase=self.navigator.phase)
                self._write({"event":"control_state",**self.owner})
                if completed: self.release(status="completed",reason="AUTO destination and heading reached")
                return
            if self.profile:
                values,completed = self.profile.evaluate(now-self.profile_started)
                self.target = {"kind":self.owner["kind"],**values}
                self.owner["profile_complete"] = completed
            target = self.target
            fields = {}
            if self.follower:
                result = self.follower.step(position,heading)
                if result.completed:
                    self.owner.update(lat=position.lat,lon=position.lon)
                    if self._execution:
                        self.release(status="completed",reason="Final waypoint reached")
                    else:
                        self.follower = None
                        self.target = {"kind":"axes","throttle":0,"steering":0}
                        self.adapter.neutral()
                    return
                if result.tracking_error_m > self.follower.plan.max_tracking_error_m:
                    raise ValueError("Maximum route tracking error exceeded")
                target = {"kind":"axes","throttle":result.throttle,"steering":result.steering}
                fields = asdict(result)
            if target:
                # Conservative short-horizon clearance, independent of steering sign.
                # This is a stop trigger, not a dynamics-based containment guarantee.
                reserve = abs(speed)*0.5
                p = xy(position,self._area.origin)
                from backend.mapping.constraints import projection
                if not self.no_gps and reserve and (any(projection(p,a,b)[1] <= self._area.padding+reserve for a,b in self._area.edges)
                                or any(math.dist(p,center) <= self._area.padding+reserve for center in self._area.centers)):
                    raise ValueError("Insufficient boundary clearance at the current speed")
                self.adapter.apply(target,dt)
                position,heading,speed = self.adapter.state()
                self.owner.update(**{key:value for key,value in target.items() if key != "kind"})
                if target["kind"] == "thrusters":
                    self.owner.update(throttle=(target["left"]+target["right"])/2,
                                      steering=(target["left"]-target["right"])/2)
                self.owner["requested_throttle"] = self.owner["throttle"]
            self.owner.update(lat=position.lat,lon=position.lon,heading=heading,speed=speed,**fields)
            self._write({"event":"control_state",**self.owner})
            if self.recording and self.recording.recorder.errors:
                raise ValueError("Run recording failed: "+self.recording.recorder.errors[-1])
        except Exception as exc:
            self.release(status="failed",reason=str(exc))
            self.runtime.controller.set_status_msg("[SDK] "+str(exc),error=True)

    def begin_recording(self, data):
        if self.recording or self.manifest and self.manifest["status"] == "finishing":
            raise ValueError("A scoped recording is already open or finalizing")
        name = data.get("name","run")
        if not isinstance(name,str) or not name.strip() or len(name)>100:
            raise ValueError("Choose a recording name of 1–100 characters")
        import re
        slug = re.sub(r"[^a-zA-Z0-9_-]+","-",name).strip("-") or "run"
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ_")+uuid4().hex[:8]
        root = Path(data.get("output_dir") or data_directory()/"runs").expanduser().resolve()
        directory = root/slug/run_id
        metadata = data.get("metadata",{})
        json.dumps(metadata,allow_nan=False)
        area = self.area()
        directory.mkdir(parents=True,exist_ok=False)
        if self.store is None:
            self.store = RunStore()
        manifest = dict(id=run_id,name=name,directory=str(directory),recording=str(directory/"recording.mcap"),
                        status="recording",started_at=stamp(),ended_at=None,reason="",metadata=metadata,
                        area_revision=area.revision,testing_without_gps=self.no_gps,geofence_enabled=not self.no_gps,mode=self.owner["mode"] if self.owner else "observe",owner_pid=os.getpid())
        self.store.save(manifest)
        save_json(directory/"area.geojson",area.geojson)
        if "plan" in data:
            save_json(directory/"plan.json",data["plan"])
        try:
            self.recording = RunRecording(directory,self.runtime.controller.recorder)
        except Exception as exc:
            self.store.save({**manifest,"status":"failed","reason":str(exc),"ended_at":stamp()})
            raise
        self.manifest = manifest
        self._record_failure = None
        self._write({"event":"recording_started", "run_id":run_id,"metadata":metadata})
        return dict(manifest)

    def end_recording(self, status="completed", reason="Recording closed"):
        if self.owner and self._owns_recording:
            raise ValueError("Cancel the saved run to end its recording")
        if not self.recording or self.manifest["status"] == "finishing":
            return dict(self.manifest) if self.manifest else {"ok":True}
        recording,manifest = self.recording,self.manifest
        if status == "completed" and self._record_failure:
            status,reason = self._record_failure
        if status not in {"completed","failed","cancelled","interrupted"}:
            raise ValueError("Invalid recording outcome")
        self._write({"event":"recording_ended","run_id":manifest["id"],"status":status,"reason":reason})
        recording.ended_ns = time.time_ns()
        manifest.update(status="finishing",ended_at=stamp(),reason=reason)
        def finalize():
            try:
                errors = recording.close()
                final_status = "failed" if errors else status
            except Exception as exc:
                errors,final_status = [str(exc)],"failed"
            with self.runtime._commands:
                self.recording = None
                manifest.update(status=final_status,recording_errors=errors)
                if errors:
                    manifest["reason"] = "Recording failed: " + "; ".join(errors)
                self.store.save(manifest)
        worker = threading.Thread(target=finalize,name="scoped-recording-close",daemon=True)
        self.finalizers.append(worker)
        worker.start()
        return dict(manifest)

    def routes(self):
        if self._routes is None:
            self._route_file = data_directory()/"routes.json"
            self._routes = json.loads(self._route_file.read_text()) if self._route_file.exists() else {}
        return self._routes

    def command(self, operation, data):
        if operation == "acquire": return self.acquire(data)
        if operation == "set": return self.set_target(data)
        if operation == "play": return self.play(data)
        if operation == "navigate": return self.navigate(data)
        if operation == "follow": return self.follow(data)
        if operation == "release": return self.release(data.get("id"))
        if operation == "execute": return self.execute(data)
        if operation == "status":
            run_id = data.get("id")
            if self.manifest and self.manifest["id"] == run_id: return dict(self.manifest)
            if self.store is None: self.store = RunStore()
            return self.store.get(run_id)
        if operation == "runs":
            if self.store is None: self.store = RunStore()
            return {"runs":self.store.list()}
        if operation == "record_begin": return self.begin_recording(data)
        if operation == "record_end":
            if not self.manifest or data.get("id") != self.manifest["id"]: raise ValueError("Unknown recording")
            return self.end_recording(data.get("status","completed"),data.get("reason","Recording closed"))
        if operation == "validate": return self.area().validate(Plan.from_dict(data["plan"]))
        if operation == "area": return {"name":"lake","revision":self.area().revision,"geometry":self.area().geojson}
        if operation == "parameters":
            c = self.runtime.controller
            if data.get("request"):
                if not c.master or not c.connected: raise ValueError("Vehicle is disconnected")
                for key in PARAMETERS:
                    c._control_parameters.pop(key,None)
                    c.transport.send("param_request_read",c.master.target_system,c.autopilot_component or 1,key.encode(),-1)
            return {key:value for key,(value,received) in c._control_parameters.items() if time.monotonic()-received <= 60}
        if operation == "route_get":
            name = data.get("name")
            if name is None: return {"waypoints":self.runtime.session.waypoints_snapshot()}
            if name not in self.routes(): raise ValueError("Unknown saved route")
            return self.routes()[name]
        if operation == "route_list": return {"names":sorted(self.routes())}
        if operation in {"route_save","route_set"}:
            if self.busy or self.runtime.controller.quit_is_armed or self.runtime.controller.mission_started:
                raise ValueError("Route editing requires idle, disarmed state")
            points = data.get("waypoints",self.runtime.session.waypoints_snapshot())
            points = [asdict(Waypoint.from_dict(p)) for p in points]
            if not 1 <= len(points) <= 257: raise ValueError("Choose 1–257 points")
            if operation == "route_save":
                name = data["name"]
                if not isinstance(name,str) or not name.strip() or len(name)>100: raise ValueError("Choose a route name")
                value = dict(name=name,revision=uuid4().hex,waypoints=points)
                self.routes()[name] = value
                save_json(self._route_file,self.routes())
                return value
            c = self.runtime.controller
            if c.is_uploading_mission or c.is_starting_mission: raise ValueError("Wait for mission upload/start")
            with c._wp_lock:
                c.waypoints = [dict(p,id=i+1,alt=2.0) for i,p in enumerate(points)]
                c.mission_uploaded = False
            return {"waypoints":points}
        raise ValueError("Unknown SDK operation")
