"""Small synchronous client; control scheduling and recording stay in the backend."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import time
import requests
from .discovery import endpoint_path
from backend.control.planning import Waypoint
from backend.control.adapter import PARAMETERS


class Run:
    def __init__(self, client, info):
        self.client,self.id = client,info["id"]
        self.directory = Path(info["directory"])
        self.control_id = info.get("control_id")
        self.owns_recording = info.get("owns_recording", True)

    def status(self):
        return self.client._call("status",id=self.id)

    def wait_until_finished(self, timeout=None):
        deadline = time.monotonic()+timeout if timeout is not None else float("inf")
        while True:
            if self.control_id and not self.owns_recording:
                control = self.client.state()["sdk"]["control"]
                if control is None or control["id"] != self.control_id:
                    raise RuntimeError("Execution status is no longer available")
                if control["status"] != "active":
                    if control["status"] != "completed":
                        raise RuntimeError(f"Execution {control['status']}: {control['reason']}")
                    return control
                if time.monotonic() >= deadline:
                    raise TimeoutError("Path is still active; timeout does not cancel it")
                time.sleep(0.1)
                continue
            status = self.status()
            if status["status"] not in {"recording","finishing"}:
                if status["status"] != "completed":
                    raise RuntimeError(f"Run {status['status']}: {status['reason']}; logs: {self.directory}")
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError("Run is still active; timeout does not cancel it")
            time.sleep(0.1)

    def cancel(self):
        if self.control_id:
            return self.client._call("release",id=self.control_id)
        return self.client._call("record_end",id=self.id,status="cancelled",reason="Cancelled by client")


class Control:
    def __init__(self,client,info):
        self.client,self.id,self.kind = client,info["id"],info["kind"]

    def move(self, *, speed_mps, duration_s, heading_deg=None, turn_rate_deg_s=None,
             acceleration_mps2=None):
        """Maintain measured speed for a backend-timed duration, then command neutral.

        Omit heading to hold the starting heading. Alternatively specify a clockwise
        turn rate in degrees/second. Blocks until the duration ends; no caller sleep.
        Optional acceleration bounds the speed reference on entry and exit, within
        duration_s. This controls the reference, not guaranteed physical acceleration.
        """
        return self._movement("move", speed_mps=speed_mps, duration_s=duration_s,
                              heading_deg=heading_deg, turn_rate_deg_s=turn_rate_deg_s,
                              acceleration_mps2=acceleration_mps2)

    def turn_to(self, *, heading_deg, tolerance_deg=3, timeout_s=60):
        """Turn in place to a compass heading; return once aligned, or raise on timeout.

        Commands zero forward thrust; this does not hold geographic position in drift.
        """
        return self._movement("turn_to", heading_deg=heading_deg,
                              tolerance_deg=tolerance_deg, timeout_s=timeout_s)

    def go_to(self, destination, *, speed_mps=1, arrival_radius_m=1, timeout_s=180):
        """Navigate a validated direct segment; command neutral on arrival and return."""
        return self._movement("go_to", destination=asdict(destination), speed_mps=speed_mps,
                              arrival_radius_m=arrival_radius_m, timeout_s=timeout_s)

    def stop(self):
        """Cancel this session's movement and command neutral immediately."""
        return self.client._call("control_stop", id=self.id)

    def _movement(self, operation, **data):
        info = self.client._call(operation, id=self.id, **data)
        try:
            while True:
                control = self.client.state()["sdk"]["control"]
                if not control or control["id"] != self.id or control["status"] != "active":
                    raise RuntimeError("Movement stopped: " + (control or {}).get("reason", "control unavailable"))
                if control.get("motion_id") != info["motion_id"]:
                    raise RuntimeError("Movement replaced by another request")
                if control.get("motion_status") == "completed":
                    return control
                if control.get("motion_status") != "active":
                    raise RuntimeError("Movement " + control.get("motion_status", "unavailable"))
                time.sleep(0.05)
        except BaseException:
            # A cancelled wait must not leave this movement running or stop its replacement.
            try:
                self.client._call("control_stop", id=self.id, motion_id=info["motion_id"])
            except Exception:
                pass
            raise

    def set(self, *, valid_for_s=0.25, **target):
        """Replace the output target. It expires unless refreshed, including on crashes."""
        return self.client._call("set",id=self.id,target=target,valid_for_s=valid_for_s)

    def play(self, keyframes):
        """Evaluate a smooth finite input profile on every backend control tick."""
        info = self.client._call("play",id=self.id,keyframes=keyframes)
        while True:
            control = self.client.state()["sdk"]["control"]
            if not control or control["id"] != self.id or control["status"] != "active":
                raise RuntimeError("Input profile stopped: " + (control or {}).get("reason","control unavailable"))
            if control.get("profile_id") != info["profile_id"]:
                raise RuntimeError("Input profile replaced by another request")
            if control.get("profile_complete"): return
            time.sleep(.05)

    def follow(self, plan, *, valid_for_s=0.5):
        """Atomically replace this session's intent with a short-lived path."""
        return self.client._call("follow",id=self.id,plan=plan.to_dict(),valid_for_s=valid_for_s)

    def hold(self, seconds, **target):
        """Utility for maintaining a target; equivalent to a short set/sleep loop."""
        if not 0 < seconds <= 86400:
            raise ValueError("Hold duration must be positive and at most one day")
        end = time.monotonic()+seconds
        try:
            while time.monotonic() < end:
                self.set(**target)
                time.sleep(min(0.05,max(0,end-time.monotonic())))
        finally:
            self.set(**{key:0 for key in target})

    def release(self):
        return self.client._call("release",id=self.id)


class Routes:
    def __init__(self,client): self.client = client

    def get(self,name=None):
        return tuple(Waypoint.from_dict(p) for p in self.client._call("route_get",name=name)["waypoints"])

    def set(self,waypoints):
        return self.client._call("route_set",waypoints=[asdict(p) for p in waypoints])

    def save(self,name,waypoints=None):
        data = {"name":name}
        if waypoints is not None: data["waypoints"] = [asdict(p) for p in waypoints]
        return self.client._call("route_save",**data)

    def load(self,name):
        points = self.get(name)
        self.set(points)
        return points

    def names(self):
        return self.client._call("route_list")["names"]

    def wait_for_points(self,count,timeout=None):
        """Read the existing map editor; no new UI or background editing mode."""
        deadline = time.monotonic()+timeout if timeout is not None else float("inf")
        while time.monotonic() < deadline:
            points = self.get()
            if len(points) == count: return points
            time.sleep(0.2)
        raise TimeoutError("Waiting for waypoints in the existing map editor")


class MissionControl:
    def __init__(self,url,token,*,mode="simulation"):
        if not url.startswith("http://127.0.0.1:"):
            raise ValueError("The initial SDK supports the local backend only")
        self.url = url.rstrip("/")
        if mode not in {"simulation", "live"}:
            raise ValueError("Choose simulation or live")
        self.default_mode = mode
        self.testing_without_gps = False
        self.http = requests.Session()
        self.http.trust_env = False
        self.http.headers.update(Authorization=f"Bearer {token}")
        self.routes = Routes(self)
        self._controls = []

    @classmethod
    def connect(cls,endpoint_file=None,*,mode="simulation"):
        path = Path(endpoint_file) if endpoint_file else endpoint_path()
        try:
            info = json.loads(path.read_text())
        except FileNotFoundError:
            raise RuntimeError("Start Mission Control first; no backend connection file exists") from None
        client = cls(info["url"],info["token"],mode=mode)
        try:
            client.testing_without_gps = bool(client.state()["sdk"].get("testing_without_gps",False))
        except Exception:
            client.close()
            raise
        return client

    def _request(self,path,data=None):
        response = self.http.get(self.url+path,timeout=5) if data is None else self.http.post(self.url+path,json=data,timeout=5)
        payload = response.json()
        if not response.ok:
            raise RuntimeError(payload.get("error",f"HTTP {response.status_code}"))
        return payload

    def _call(self,operation,**data):
        return self._request("/command",{"action":"sdk","operation":operation,**data})

    def _wait_for_neutral(self):
        """Allow an earlier release's neutral transmissions to finish; never navigate."""
        while self.state()["sdk"].get("neutralizing", False):
            time.sleep(0.05)

    def state(self):
        return self._request("/sdk/state")

    def area(self): return self._call("area")
    def runs(self): return self._call("runs")["runs"]
    def validate(self,plan): return self._call("validate",plan=plan.to_dict())

    def read_parameters(self,timeout=5):
        values = self._call("parameters",request=True)
        deadline = time.monotonic()+timeout
        while not set(PARAMETERS) <= values.keys():
            if time.monotonic() >= deadline: raise TimeoutError("Vehicle did not return all control parameters")
            time.sleep(0.1)
            values = self._call("parameters")
        return values

    @contextmanager
    def control(self,mode=None,*,kind="axes",start=None,end=None,heading_deg=0):
        self._wait_for_neutral()
        info = self._call("acquire",mode=mode or self.default_mode,kind=kind,start=asdict(start) if start else None,end=asdict(end) if end else None,heading_deg=heading_deg)
        control = Control(self,info)
        self._controls.append(control)
        try:
            yield control
        finally:
            try:
                control.release()
            finally:
                self._controls.remove(control)

    @contextmanager
    def record(self,name,*,output_dir=None,metadata=None):
        data = dict(name=name,metadata=metadata or {})
        if output_dir is not None: data["output_dir"] = str(output_dir)
        run = Run(self,self._call("record_begin",**data))
        try:
            yield run
        except BaseException as exc:
            self._call("record_end",id=run.id,status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                       reason=f"{type(exc).__name__}: {exc}")
            raise
        else:
            self._call("record_end",id=run.id)
        finally:
            # Wait for indexed files to be closed and published, including partial runs.
            while self._call("status",id=run.id)["status"] == "finishing":
                time.sleep(0.05)

    def run(self,name,plan,*,mode=None,output_dir=None,metadata=None):
        data = dict(name=name,plan=plan.to_dict(),mode=mode or self.default_mode,metadata=metadata or {})
        if output_dir is not None: data["output_dir"] = str(output_dir)
        self._wait_for_neutral()
        return Run(self,self._call("execute",**data))

    def navigate(self, destination, *, speed_mps=1, heading_deg=None, timeout_s=180):
        """Use a verified AUTO transfer in live mode; simulate the same transfer offline."""
        self._wait_for_neutral()
        info = self._call("navigate",mode=self.default_mode,destination=asdict(destination),
                          speed_mps=speed_mps,heading_deg=heading_deg,timeout_s=timeout_s)
        try:
            while True:
                control = self.state()["sdk"]["control"]
                if not control or control["id"] != info["id"]: raise RuntimeError("AUTO transfer ownership changed")
                if control["status"] != "active":
                    if control["status"] != "completed": raise RuntimeError("AUTO transfer failed: "+control["reason"])
                    return control
                time.sleep(.05)
        finally:
            self._call("release",id=info["id"])

    def stop(self):
        return self._request("/command",{"action":"stop"})

    def close(self):
        for control in list(self._controls):
            try: control.release()
            except requests.RequestException: pass
        self.http.close()

    def __enter__(self): return self
    def __exit__(self,*_): self.close()
