"""Small synchronous client; control scheduling and recording stay in the backend."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import time
import requests
from .discovery import endpoint_path
from boats.heron.backend.control.planning import Waypoint, number
from boats.heron.backend.control.adapter import PARAMETERS


class Run:
    """Handle to a recording or path execution, including its log directory."""
    def __init__(self, client, info):
        self.client,self.id = client,info["id"]
        self.directory = Path(info["directory"])
        self.control_id = info.get("control_id")
        self.owns_recording = info.get("owns_recording", True)

    def status(self):
        """Return the backend's recording status, completion reason and file metadata."""
        return self.client._call("status",id=self.id)

    def wait_until_finished(self, timeout=None):
        """Block until successful completion and return the final status dictionary.

        timeout is an optional wait limit in seconds; None waits indefinitely.
        Raises RuntimeError on failed/cancelled execution, or TimeoutError when
        the wait limit expires. A wait timeout does not cancel the execution.
        """
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
        """Request cancellation, releasing owned control or ending the recording."""
        if self.control_id:
            return self.client._call("release",id=self.control_id)
        return self.client._call("record_end",id=self.id,status="cancelled",reason="Cancelled by client")


class Control:
    """Exclusive control scope yielded by MissionControl.control().

    Inputs are normalized to [-1, 1]; movement methods use physical units.
    Exiting the parent context requests neutral and releases ownership.
    """
    def __init__(self,client,info):
        self.client,self.id,self.kind = client,info["id"],info["kind"]

    def move(self, *, speed_mps, duration_s, heading_deg=None, turn_rate_deg_s=None,
             acceleration_mps2=None):
        """Maintain measured speed for a backend-timed duration, then command neutral.

        Omit heading to hold the starting heading. Alternatively specify a clockwise
        turn rate in degrees/second. Blocks until the duration ends; no caller sleep.
        Optional acceleration bounds the speed reference on entry and exit, within
        duration_s. This controls the reference, not guaranteed physical acceleration.
        speed_mps is metres/second, heading_deg is a clockwise compass heading
        (0 north, 90 east), and acceleration_mps2 is metres/second squared.
        Returns the completed control status; raises on interruption or failure.
        Requires position, heading and speed feedback; unavailable in no-GPS mode.
        """
        return self._movement("move", speed_mps=speed_mps, duration_s=duration_s,
                              heading_deg=heading_deg, turn_rate_deg_s=turn_rate_deg_s,
                              acceleration_mps2=acceleration_mps2)

    def turn_to(self, *, heading_deg, tolerance_deg=3, timeout_s=60):
        """Turn in place to a compass heading; return once aligned, or raise on timeout.

        Commands zero forward thrust; this does not hold geographic position in drift.
        heading_deg is a clockwise compass heading (0 north, 90 east);
        tolerance_deg is the accepted heading error. timeout_s is seconds.
        Returns completed control status or raises on failure/timeout.
        Requires position/heading feedback; unavailable in no-GPS mode.
        """
        return self._movement("turn_to", heading_deg=heading_deg,
                              tolerance_deg=tolerance_deg, timeout_s=timeout_s)

    def go_to(self, destination, *, speed_mps=1, arrival_radius_m=1, timeout_s=180):
        """Follow a direct segment to destination (Waypoint), then request neutral.

        speed_mps is metres/second; arrival_radius_m is the accepted distance in
        metres. timeout_s is the failure limit in seconds. Blocks and returns
        completed control status; raises on failure or timeout. Uses MANUAL
        feedback control and requires GPS; unavailable in no-GPS mode.
        """
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
        """Send one normalized target and return the backend acknowledgement.

        Supply throttle and steering for kind='axes', or left and right for
        kind='thrusters'. Both fields are required, each in [-1, 1]. Positive
        throttle/left/right requests forward; positive steering turns clockwise.
        valid_for_s is the command lifetime in seconds (0.05–1, default 0.25).
        Refresh before expiry; a stalled client cannot keep old commands alive.
        """
        return self.client._call("set",id=self.id,target=target,valid_for_s=valid_for_s)

    def play(self, keyframes):
        """Block while the backend executes a smooth finite input profile.

        keyframes is a list of (seconds, target_dict) pairs starting at zero,
        with strictly increasing times. Targets use the same fields/ranges as
        set(). The backend smoothly interpolates values and retains the final
        target until the profile lease expires or another command replaces it.
        End with zeros to request neutral at completion. Returns None on
        completion; raises RuntimeError if control is lost or the profile replaced.
        """
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
        """Replace this scope's intent with a Plan; return an acknowledgement.

        Nonblocking. Refresh within valid_for_s seconds (0.05–5, default 0.5) to
        retain control; expiry requests neutral. Requires an axes control scope,
        GPS and geographic validation.
        """
        return self.client._call("follow",id=self.id,plan=plan.to_dict(),valid_for_s=valid_for_s)

    def pulse(self, duration_s, **target):
        """Hold an input on the backend clock, then request neutral; block until done.

        duration_s is positive seconds (at most 86400, below 60 in no-GPS mode).
        target uses set() fields/ranges. The backend transmits on its control tick
        and neutralizes at the deadline even if this client's polling stalls or
        disconnects. No 250 ms client-refresh lease is used for a finite pulse.
        Stop, disarming, link loss and existing vehicle checks can still abort it.
        Returns None after confirmed completion; raises with the backend reason
        on failure. Interrupting the wait requests cancellation of this pulse.
        """
        info = self.client._call("pulse",id=self.id,duration_s=duration_s,target=target)
        try:
            while True:
                control = self.client.state()["sdk"]["control"]
                if not control or control["id"] != self.id or control["status"] != "active":
                    raise RuntimeError("Input pulse stopped: " + (control or {}).get("reason","control unavailable"))
                if control.get("profile_id") != info["profile_id"]:
                    raise RuntimeError("Input pulse replaced by another request")
                if control.get("profile_complete"):
                    return
                time.sleep(.05)
        except BaseException as failure:
            try:
                self.client._call("control_stop",id=self.id,profile_id=info["profile_id"])
            except BaseException as cleanup:
                raise BaseExceptionGroup("Pulse wait and cancellation both failed",[failure,cleanup]) from None
            raise

    def hold(self, seconds, **target):
        """Block while refreshing a set() target, then request zero on all its axes.

        seconds must be positive and at most 86400 (one day). target uses the
        fields and normalized ranges documented by set(). Refreshes about every
        50 ms; duration is approximate, subject to network and scheduling delay.
        Requests neutral in cleanup, including on exceptions; command expiry
        remains the fallback if communication fails. Returns None on success.
        """
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
        """Request neutral and release this scope; return the backend acknowledgement."""
        return self.client._call("release",id=self.id)


class Routes:
    """Read and edit waypoint routes in the existing map editor and route store."""
    def __init__(self,client): self.client = client

    def get(self,name=None):
        """Return a tuple of Waypoints from a saved name or the current map draft."""
        return tuple(Waypoint.from_dict(p) for p in self.client._call("route_get",name=name)["waypoints"])

    def set(self,waypoints):
        """Replace the map draft with an iterable of Waypoints; do not move the boat."""
        return self.client._call("route_set",waypoints=[asdict(p) for p in waypoints])

    def save(self,name,waypoints=None):
        """Save Waypoints under name; omit waypoints to save the current map draft."""
        data = {"name":name}
        if waypoints is not None: data["waypoints"] = [asdict(p) for p in waypoints]
        return self.client._call("route_save",**data)

    def load(self,name):
        """Load a saved route into the map draft and return its tuple of Waypoints."""
        points = self.get(name)
        self.set(points)
        return points

    def names(self):
        """Return the names of saved routes."""
        return self.client._call("route_list")["names"]

    def wait_for_points(self,count,timeout=None):
        """Wait for exactly count map points and return their tuple of Waypoints.

        timeout is seconds, or None for no limit. Raises TimeoutError on expiry.
        Reads the existing editor without issuing control or navigation commands.
        """
        deadline = time.monotonic()+timeout if timeout is not None else float("inf")
        while time.monotonic() < deadline:
            points = self.get()
            if len(points) == count: return points
            time.sleep(0.2)
        raise TimeoutError("Waiting for waypoints in the existing map editor")


class MissionControl:
    """Synchronous local backend client; use as a context manager to close it.

    url is the backend's http://127.0.0.1:<port> endpoint; token authenticates
    requests. mode selects 'simulation' (default) or explicit 'live' execution.
    The client never arms the vehicle automatically. routes exposes the map
    draft and saved routes. Batch execution manages recordings for experiments.
    """
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
        """Connect using a published local endpoint descriptor and return a client.

        endpoint_file is an optional path; otherwise use the configured discovery
        file. mode selects simulation or live; it must match a reserved backend.
        Reads the backend's no-GPS test setting. Raises if discovery or connection
        fails; does not start a backend or arm the vehicle.
        """
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

    def _prepare_control(self, mode=None):
        self._wait_for_neutral()

    def state(self):
        """Return vehicle/SDK state without renewing the UI's manual-control lease."""
        return self._request("/sdk/state")

    def area(self):
        """Return configured operating-area geometry and geographic constraints."""
        return self._call("area")

    def runs(self):
        """Return indexed recordings and their status metadata."""
        return self._call("runs")["runs"]

    def validate(self,plan):
        """Check a Plan against the operating area; return validation details, no motion."""
        return self._call("validate",plan=plan.to_dict())

    def read_parameters(self,timeout=5):
        """Request and return the vehicle's RC/motor mapping and calibration values.

        timeout is seconds; raises TimeoutError if required parameters are missing.
        Uses still-fresh cached values and paces/retries requests for missing or
        expired values. A timeout names the missing parameters. Reads only,
        without changing calibration, mode or arming.
        Optional diagnostics only: control never calls this method and does not
        use these calibration values to transform raw MANUAL_CONTROL inputs.
        """
        values = self._call("parameters")
        deadline = time.monotonic()+timeout
        requested = {}
        while not set(PARAMETERS) <= values.keys():
            now = time.monotonic()
            missing = [key for key in PARAMETERS if key not in values]
            if now >= deadline:
                raise TimeoutError("Vehicle did not return control parameters: " + ", ".join(missing))
            # Pace requests and retry missing replies, preserving valid cached
            # values. Bursting/re-clearing every parameter on each pulse loses
            # replies on the vehicle link and can prevent the sweep finishing.
            key = min(missing, key=lambda key:requested.get(key, -float("inf")))
            if now-requested.get(key, -float("inf")) >= 0.5:
                self._call("parameters",request=True,names=[key])
                requested[key] = now
            time.sleep(0.05)
            values = self._call("parameters")
        return values

    def set_manual(self, *, timeout_s=10):
        """Explicitly select MANUAL and wait for autopilot heartbeat confirmation.

        Live only; simulation is a no-op. timeout_s is the confirmation limit in
        seconds. Releases active SDK control and leaves joystick driving disabled.
        Does not arm, reset a kill switch or bypass autopilot checks. Returns None;
        raises RuntimeError on rejection/link loss or TimeoutError if unconfirmed.
        """
        return self._vehicle_action("set_manual", "mode", "MANUAL", timeout_s)

    def arm(self, *, timeout_s=10):
        """Explicitly request normal arming and wait for confirmed armed telemetry.

        Live mode requires confirmed MANUAL and a fresh link. Uses normal arming
        with pre-arm checks, never force-arm. timeout_s is seconds. Simulation is
        a no-op so experiment setup cannot arm real hardware during simulation.
        Returns None; raises on rejection/link loss or confirmation timeout.
        """
        return self._vehicle_action("arm", "armed", True, timeout_s)

    def disarm(self, *, timeout_s=10):
        """Release control, request disarming and wait for heartbeat confirmation.

        timeout_s is seconds. Allowed with an engaged kill switch. Simulation is
        a no-op. Returns None; raises on failed request, link loss or timeout.
        Use armed_manual() for managed cleanup without boilerplate in experiments.
        """
        return self._vehicle_action("disarm", "armed", False, timeout_s)

    @contextmanager
    def armed_manual(self, *, timeout_s=10):
        """Yield this client after confirmed MANUAL selection and normal arming.

        Requests disarming on exit, including body errors or arming timeouts.
        Does not suppress exceptions: an experiment error propagates unchanged
        when disarming succeeds. If disarming also fails, both errors propagate
        in a BaseExceptionGroup so cleanup cannot hide the original problem.
        timeout_s is the per-action confirmation limit in seconds; simulation
        sends no vehicle actions. Does not reset the kill switch.

        Example:
            with mc.armed_manual():
                mc.run_thruster('left', throttle_pct=20, duration_s=10)
        """
        self.set_manual(timeout_s=timeout_s)
        failure = None
        try:
            self.arm(timeout_s=timeout_s)
            yield self
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                self.disarm(timeout_s=timeout_s)
            except BaseException as cleanup:
                if failure is not None:
                    raise BaseExceptionGroup("Experiment and disarming both failed", [failure, cleanup]) from None
                raise

    def _vehicle_action(self, action, field, expected, timeout_s):
        timeout_s = number(timeout_s, 0.05, 180, "confirmation timeout")
        if self.default_mode == "simulation":
            return
        self._call("vehicle_action", command=action)
        deadline = time.monotonic()+timeout_s
        while True:
            state = self.state()
            if not state.get("connected"):
                raise RuntimeError("Vehicle disconnected while confirming " + action)
            if state.get("vehicle_confirmed", {}).get(field) == expected:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Vehicle did not confirm " + action)
            time.sleep(0.05)

    @contextmanager
    def control(self,mode=None,*,kind="axes",start=None,end=None,heading_deg=0):
        """Yield exclusive Control ownership; request neutral and release on exit.

        mode defaults to this client's mode. kind is 'axes' (throttle/steering)
        or 'thrusters' (left/right). 'motor_test' is reserved for isolated finite
        live --no-gps bench pulses; use test_thruster() for setup and cleanup.
        start optionally seeds simulation position;
        heading_deg seeds simulation's clockwise compass heading (0 north).
        Live control always uses vehicle telemetry. Optional end is a Waypoint
        for corridor validation; neither start nor end requests navigation.
        Live mode requires confirmed MANUAL/arming and a fresh heartbeat. Sends
        raw normalized MANUAL_CONTROL inputs without RC calibration lookup or
        app-side PWM conversion. Expects standard Rover throttle/steering mixing
        (PILOT_STEER_TYPE=0); autopilot configuration still affects motor outputs.
        No-GPS bypass must be explicitly selected when starting the backend.
        """
        self._prepare_control(mode)
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
        """Yield a Run recording scope and finalize its logs on context exit.

        name labels the run; output_dir optionally overrides the recording root.
        metadata must be JSON serializable. Failures preserve partial logs with
        their reason. Do not nest inside batch experiments: the runner records
        each experiment automatically. Exiting waits for recording files to close.
        """
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
        """Start a validated Plan and return a nonblocking Run handle.

        name labels execution; mode defaults to the client's selected mode.
        output_dir overrides the log root; metadata is JSON serializable.
        Uses an enclosing recording when present. Call wait_until_finished()
        on the handle to wait, or cancel() to stop. Requires GPS feedback.
        """
        data = dict(name=name,plan=plan.to_dict(),mode=mode or self.default_mode,metadata=metadata or {})
        if output_dir is not None: data["output_dir"] = str(output_dir)
        self._prepare_control(mode)
        return Run(self,self._call("execute",**data))

    def test_thruster(self, side, *, throttle_pct, duration_s):
        """Run an isolated Rover bench test using DO_MOTOR_TEST, not mixed axes.

        Requires a live --no-gps backend. Selects MANUAL, confirms disarming,
        then asks the firmware to arm/test only motor 3 (left) or 4 (right).
        Percentage must be a nonzero whole number in [-100, 100]; duration is
        positive and at most 30 seconds. Firmware times the test even if this
        client/backend disappears, and disarms when it ends. Rover temporarily
        suspends its GCS/throttle/crash failsafes during native motor tests;
        the backend still monitors the link and handles stop/kill/cancellation.
        Confirms disarming on exit. Does not change saved firmware parameters.
        """
        if side not in ("left", "right"):
            raise ValueError("Thruster side must be 'left' or 'right'")
        percent = number(throttle_pct, -100, 100, "motor test percentage")
        duration = number(duration_s, 0, 30, "motor test duration")
        if not percent or percent != round(percent):
            raise ValueError("Motor test percentage must be a nonzero whole number")
        if not duration:
            raise ValueError("Motor test duration must be positive")
        if self.default_mode != "live" or not self.testing_without_gps:
            raise ValueError("Isolated motor tests require live --no-gps lab mode")
        self.set_manual()
        self.disarm()
        try:
            with self.control(kind="motor_test") as control:
                control.pulse(duration, left=percent / 100 if side == "left" else 0,
                              right=percent / 100 if side == "right" else 0)
        except BaseException as failure:
            try:
                self.disarm()
            except BaseException as cleanup:
                raise BaseExceptionGroup("Motor test and disarm both failed", [failure, cleanup]) from None
            raise
        else:
            self.disarm()

    def run_thruster(self, side, *, throttle_pct, duration_s, mode=None, start=None):
        """Run one thruster at a throttle percentage; keep the other neutral.

        Args:
            side: 'left' or 'right', according to the vehicle motor wiring/mapping.
            throttle_pct: Requested input percentage from -100 (full reverse)
                through 0 (neutral) to 100 (full forward). 20 means 20% forward
                input, not measured electrical power, physical thrust or speed.
            duration_s: Positive hold time in seconds, at most 86400. In no-GPS
                mode choose below 60, leaving command/cleanup overhead.
            mode: 'simulation' or 'live'; defaults to this client's mode.
            start: Optional simulation-only Waypoint. Never requests navigation.

        Blocks until the pulse and cleanup finish; returns None. Both sides are
        requested neutral afterward, including on interruption. The backend times the pulse
        and sends neutral at its deadline even if this client stalls. Timing is
        approximate to the backend control tick. Live calls use raw MANUAL_CONTROL inputs without
        app-side RC calibration, and retain MANUAL, arming and link checks.
        Lab tests without location require --no-gps at backend startup.
        Raises ValueError for invalid side/percentage/duration before control
        acquisition. Backend rejections raise RuntimeError; HTTP errors propagate.
        See run_thrusters() for simultaneous inputs and communication failures.

        One-side-neutral describes the requested input mix; firmware settings
        still determine actual motor output. Expects PILOT_STEER_TYPE=0.

        Example: mc.run_thruster('left', throttle_pct=20, duration_s=10)
        """
        if side not in ("left", "right"):
            raise ValueError("Thruster side must be 'left' or 'right'")
        return self.run_thrusters(left_throttle_pct=throttle_pct if side == "left" else 0,
                                 right_throttle_pct=throttle_pct if side == "right" else 0,
                                 duration_s=duration_s, mode=mode, start=start)

    def run_thrusters(self, *, left_throttle_pct, right_throttle_pct, duration_s,
                      mode=None, start=None):
        """Run both thrusters at independent percentages, then neutralize/release.

        Args:
            left_throttle_pct: Left input percentage in [-100, 100].
            right_throttle_pct: Right input percentage in [-100, 100]. Both
                percentages are required; use zero for a side to stay neutral.
            duration_s: Positive hold time in seconds, at most 86400. In no-GPS
                mode choose below 60, allowing command/cleanup overhead.
            mode: 'simulation' or 'live'; defaults to this client's mode.
            start: Optional simulation-only Waypoint; never navigates the boat.

        Percentages mean requested throttle input: -100 full reverse, 0 neutral,
        100 full forward, not measured electrical power, thrust or speed.
        Blocks until finished and returns None. The backend transmits the target
        every control tick and requests neutral at the duration's deadline,
        independently of client refresh/polling delays. If this client disconnects,
        the finite pulse still ends on the backend clock. Stop/disarm and vehicle
        checks can abort it early. Timing is approximate to the control tick;
        motor response is not guaranteed by a successful transport write.

        Live calls send raw MANUAL_CONTROL steering/throttle without app-side
        calibration, parameter reads or PWM overrides. Expects standard Rover
        mixing (PILOT_STEER_TYPE=0); firmware-side settings can still affect the
        output. Requires confirmed MANUAL/arming and a fresh heartbeat.
        Lab operation without location requires --no-gps at backend startup,
        disabling geographic checks/navigation. Otherwise GPS and lake clearance
        checks apply. Does not hold position or actively brake. Uses an enclosing
        batch recording, if any; does not start a recording.
        Raises ValueError on invalid percentages/duration before acquisition, or
        RuntimeError on backend rejection/control failure; HTTP errors propagate.

        Example: mc.run_thrusters(left_throttle_pct=20, right_throttle_pct=-30,
                                 duration_s=10)
        """
        left = number(left_throttle_pct, -100, 100, "left throttle percentage") / 100
        right = number(right_throttle_pct, -100, 100, "right throttle percentage") / 100
        duration_s = number(duration_s, 0, 86400, "thruster duration")
        if duration_s == 0:
            raise ValueError("Thruster duration must be positive")
        if self.testing_without_gps and duration_s >= 60:
            raise ValueError("Thruster duration must be less than 60 seconds in no-GPS mode")
        with self.control(mode=mode, kind="thrusters", start=start) as control:
            control.pulse(duration_s, left=left, right=right)

    def navigate(self, destination, *, speed_mps=1, heading_deg=None, timeout_s=180):
        """Block on an AUTO transfer to destination (Waypoint); return final status.

        speed_mps is transfer speed in metres/second. Optional heading_deg aligns
        to a clockwise compass heading on arrival (0 north, 90 east). timeout_s
        is the failure limit in seconds. Uses the client's selected mode; live
        execution verifies AUTO transfer and restores confirmed MANUAL afterward.
        Requires GPS; unavailable in no-GPS mode. Releases ownership on exit and
        raises RuntimeError on transfer failure or ownership changes.
        """
        self._prepare_control()
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
        """Engage the global stop/kill path and request neutral; return acknowledgement.

        Stops active missions and SDK execution. Reset the kill switch in the UI
        before acquiring control again; this method does not reset it or arm.
        """
        return self._request("/command",{"action":"stop"})

    def close(self):
        """Release tracked control scopes and close the client's HTTP session."""
        for control in list(self._controls):
            try: control.release()
            except requests.RequestException: pass
        self.http.close()

    def __enter__(self): return self
    def __exit__(self,*_): self.close()
