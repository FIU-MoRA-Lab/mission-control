"""Sequential Python runner. Own process lifetime, not experiment-specific behavior."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time
import traceback
from uuid import uuid4

from boats.heron.paths import ROOT, log_directory
from boats.heron.backend.sdk import MissionControl
from boats.heron.backend.control.storage import save_json


@dataclass(frozen=True)
class Entry:
    name: str
    path: Path
    function: object


def discover(paths):
    """Sorted files; either run(mc), or an ordered EXPERIMENTS mapping of callables."""
    files = []
    for value in paths:
        path = Path(value).resolve()
        if not path.exists():
            raise ValueError(f"No such path: {path}")
        candidates = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        files.extend(p for p in candidates if not p.name.startswith("_") and p.suffix == ".py")
    entries = []
    for path in dict.fromkeys(files):
        name = "_mission_control_batch_"+hashlib.sha256(str(path).encode()).hexdigest()[:16]
        spec = importlib.util.spec_from_file_location(name,path)
        module = importlib.util.module_from_spec(spec)
        # Register for dataclasses and imports; __main__ blocks do not execute.
        sys.modules[name] = module
        spec.loader.exec_module(module)
        functions = getattr(module,"EXPERIMENTS",None)
        if functions is None:
            callback = getattr(module,"run",None)
            functions = {path.stem:callback} if callback else {}
        if not isinstance(functions,dict):
            raise ValueError(f"{path}: EXPERIMENTS must be a mapping of names to functions")
        for label,callback in functions.items():
            if not isinstance(label,str) or not label.strip() or len(label)>100 or not callable(callback):
                raise ValueError(f"{path}: invalid experiment entry {label!r}")
            if inspect.iscoroutinefunction(callback):
                raise ValueError(f"{path}: use a synchronous function accepting mc")
            inspect.signature(callback).bind(None)
            entries.append(Entry(label,path,callback))
    if not entries:
        raise ValueError("No run(mc) functions or EXPERIMENTS mappings found")
    return entries


def wait_ready(process, timeout):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout,selectors.EVENT_READ)
        if not selector.select(timeout):
            raise TimeoutError("Backend startup timed out; see backend.log")
        line = process.stdout.readline()
    if not line:
        raise RuntimeError("Backend exited during startup; see backend.log")
    try:
        return json.loads(line)
    except ValueError:
        raise RuntimeError("Backend startup protocol failed; see backend.log") from None


@contextmanager
def backend_session(directory, *, mode, connection, mission_file, inputs, timeout, no_gps=False):
    """A private descriptor prevents a batch from connecting to another open app."""
    env = dict(os.environ)
    env["MISSION_CONTROL_ENDPOINT_FILE"] = str(directory/"connection.json")
    env["MISSION_CONTROL_LOG_DIR"] = str(directory/"session")
    args = [sys.executable,"-u","-m","boats.heron.backend.bridge","--execution-mode",mode]
    if mode == "simulation": args.append("--demo")
    else: args.extend(["--connection",connection])
    if mission_file: args.extend(["--mission-file",str(Path(mission_file).resolve())])
    if no_gps: args.append("--no-gps-test")
    if not inputs: args.append("--no-inputs")
    with (directory/"backend.log").open("w") as diagnostics:
        process = subprocess.Popen(args,cwd=ROOT,env=env,stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,stderr=diagnostics,text=True,start_new_session=True)
        client = None
        try:
            ready = wait_ready(process,timeout)
            # Drain the joystick display pipe even without an attached UI.
            def drain():
                for _ in process.stdout:
                    pass
            reader = threading.Thread(target=drain,name="batch-backend-output",daemon=True)
            reader.start()
            client = MissionControl(f"http://127.0.0.1:{ready['port']}",ready["token"],mode=mode)
            client.testing_without_gps = no_gps
            client.state()
            yield client, directory/"connection.json"
        finally:
            if client:
                try: client._call("release")
                except (RuntimeError, OSError): pass
                client.close()
            if process.stdin:
                process.stdin.close()  # EOF is the existing neutralize/flush shutdown path.
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout: process.stdout.close()


def settle(mc):
    """Wait for backend paths; release direct controls; detect swallowed failures."""
    while True:
        state = mc.state()["sdk"]
        if state.get("neutralizing"):
            time.sleep(.05)
            continue
        control = state["control"]
        if not control or control["status"] != "active":
            if control and control["status"] == "failed":
                raise RuntimeError(control["reason"])
            return
        if "run_id" in control:
            time.sleep(0.1)
        else:
            mc._call("release",id=control["id"])


@contextmanager
def attached_session(*, mode, endpoint_file=None, no_gps=False):
    """Borrow an app-owned backend without taking over its process lifetime."""
    with MissionControl.connect(endpoint_file, mode=mode) as mc:
        state = mc.state()
        if mode == "live" and state.get("demo"):
            raise ValueError("Cannot run live experiments against a demo app")
        if no_gps and not mc.testing_without_gps:
            raise ValueError("The running backend was not started with --no-gps-test; --attach cannot change that setting")
        if mc.testing_without_gps and mode != "live":
            raise ValueError("The running backend uses no-GPS motor testing; use --mode live")
        sdk = state["sdk"]
        if ((sdk.get("control") or {}).get("status") == "active" or
                (sdk.get("recording") or {}).get("status") in {"recording", "finishing"}):
            raise ValueError("The running backend already has SDK control or a recording in progress")
        print(f"Attached to Mission Control at {mc.url}. The app will remain open after the batch.", flush=True)
        yield mc, None


def execute(entries, mc, directory):
    summary = {"testing_without_gps":getattr(mc,"testing_without_gps",False),"mode":mc.default_mode,"status":"running","results":[]}
    save_json(directory/"results.json",summary)
    try:
        for index,entry in enumerate(entries,1):
            print(f"[{index}/{len(entries)}] {entry.name}",flush=True)
            source = entry.path.read_bytes()
            metadata = {"source":str(entry.path),"sha256":hashlib.sha256(source).hexdigest(),"batch_directory":str(directory)}
            result = {"name":entry.name,"source":str(entry.path),"status":"running"}
            summary["results"].append(result)
            started = time.monotonic()
            try:
                with mc.record(entry.name,output_dir=directory/"runs",metadata=metadata) as run:
                    result.update(id=run.id,directory=str(run.directory))
                    (run.directory/"source.py").write_bytes(source)
                    try:
                        returned = entry.function(mc)
                        if inspect.isawaitable(returned):
                            if inspect.iscoroutine(returned): returned.close()
                            raise ValueError("Batch functions must be synchronous")
                        settle(mc)
                    finally:
                        mc._call("release")
                status = run.status()
                if status["status"] != "completed":
                    raise RuntimeError(f"{status['status']}: {status['reason']}")
                result["status"] = "completed"
                print(f"  saved {run.directory}",flush=True)
            except BaseException as exc:
                result.update(status="interrupted" if isinstance(exc,KeyboardInterrupt) else "failed",error=str(exc))
                (directory/"failure.txt").write_text(traceback.format_exc())
                raise
            finally:
                result["duration_s"] = round(time.monotonic()-started,3)
                save_json(directory/"results.json",summary)
        summary["status"] = "completed"
    except BaseException as exc:
        summary["status"] = "interrupted" if isinstance(exc,KeyboardInterrupt) else "failed"
        raise
    finally:
        save_json(directory/"results.json",summary)
    return summary


def live_blockers(state, no_gps=False):
    import math
    blockers = []
    confirmed = state.get("vehicle_confirmed")
    mode = confirmed.get("mode") if confirmed is not None else state.get("mode")
    armed = confirmed.get("armed") if confirmed is not None else state.get("armed")
    if not state.get("connected"): blockers.append("vehicle disconnected")
    if mode != "MANUAL": blockers.append(f"confirmed mode is {mode or 'unavailable'} (need MANUAL)")
    if not armed:
        blockers.append("waiting for autopilot to confirm arming" if state.get("armed") else "vehicle disarmed")
    if not no_gps and state.get("fix",0) < 3:
        blockers.append(f"GPS fix unavailable (fix={state.get('fix',0)}, satellites={state.get('satellites',0)})")
    coordinates = (state.get("lat"),state.get("lon"))
    if not no_gps and (not all(isinstance(v,(float,int)) and math.isfinite(v) for v in coordinates) or coordinates == (0,0)):
        blockers.append("vehicle position unavailable")
    return blockers


def wait_for_vehicle(mc,timeout,report=None,no_gps=False,require_control=True):
    print("Waiting for vehicle connection" + (", MANUAL mode and arming." if require_control else
          "; experiment code selects MANUAL and requests arming explicitly."),flush=True)
    deadline = time.monotonic()+timeout
    previous = None
    while time.monotonic() < deadline:
        state = mc.state()
        blockers = live_blockers(state,no_gps=no_gps)
        if not require_control:
            blockers = [issue for issue in blockers if issue == "vehicle disconnected" or
                        issue.startswith("GPS fix") or issue == "vehicle position unavailable"]
        if blockers != previous:
            ready = "vehicle ready" if require_control else "connection ready; starting experiment setup"
            print("Live startup: " + ("; ".join(blockers) if blockers else ready),flush=True)
            if report: report(state,blockers)
            previous = blockers
        if not blockers:
            return
        time.sleep(.2)
    raise TimeoutError("Vehicle not ready: " + "; ".join(previous or ["startup timeout"]))


def wait_for_ui(ui):
    """Keep the backend and manual controls alive until the operator closes the UI."""
    while ui.poll() is None:
        try:
            ui.wait()
        except KeyboardInterrupt:
            print("Use the Mission Control window to quit.", file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run Python files sequentially using one Mission Control backend")
    parser.add_argument("paths",nargs="+",help="Python files or directories containing run(mc) or EXPERIMENTS")
    parser.add_argument("--mode",choices=("simulation","live"),default="simulation")
    parser.add_argument("--ui",action="store_true",help="Attach the existing UI to this batch backend")
    parser.add_argument("--attach",action="store_true",help="Run on an already running Mission Control backend; leave the app open")
    parser.add_argument("--endpoint-file",type=Path,help="Connection descriptor for --attach (default: SDK discovery)")
    parser.add_argument("--no-gps",action="store_true",help="LIVE MOTOR TEST ONLY: disable geographic checks and all waypoint/AUTO navigation")
    parser.add_argument("--only",action="append",help="Run only this named case; repeat to select multiple cases")
    parser.add_argument("--list",action="store_true",help="List discovered functions without starting a backend")
    parser.add_argument("--output-dir",type=Path,help="Root for batch directories (default: <logs>/batches/)")
    parser.add_argument("--mission-file",help="Initial waypoint file; useful for map-derived routes")
    parser.add_argument("--connection",help="MAVLink endpoint for a new backend (default: udpin:0.0.0.0:14550)")
    parser.add_argument("--startup-timeout",type=float,default=180)
    args = parser.parse_args(argv)
    try:
        entries = discover(args.paths)
        if args.endpoint_file and not args.attach: raise ValueError("--endpoint-file requires --attach")
        if args.attach and (args.ui or args.connection or args.mission_file):
            raise ValueError("--attach uses the running app; omit --ui, --connection and --mission-file")
        if args.no_gps and args.mode != "live": raise ValueError("--no-gps is a live motor-testing flag; use --mode live")
        if args.only:
            missing = set(args.only)-{entry.name for entry in entries}
            if missing: raise ValueError("Unknown case(s): "+", ".join(sorted(missing)))
            entries = [entry for entry in entries if entry.name in args.only]
        if args.list:
            for entry in entries: print(f"{entry.name}\t{entry.path}")
            return 0
        if args.startup_timeout <= 0: raise ValueError("Startup timeout must be positive")
    except (ValueError,TypeError,ImportError) as exc:
        parser.error(str(exc))
    root = (args.output_dir or log_directory()/"batches").expanduser().resolve()
    directory = root/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_")+uuid4().hex[:8])
    directory.mkdir(parents=True,exist_ok=False)
    print(f"Batch: {directory}",flush=True)
    ui = None
    try:
        session = (attached_session(mode=args.mode,endpoint_file=args.endpoint_file,no_gps=args.no_gps)
                   if args.attach else backend_session(directory,mode=args.mode,
                       connection=args.connection or "udpin:0.0.0.0:14550",mission_file=args.mission_file,
                       inputs=args.ui,timeout=args.startup_timeout,no_gps=args.no_gps))
        with session as (mc,endpoint):
            if mc.testing_without_gps:
                print("NO-GPS MOTOR TEST: lake/fountain protection and AUTO travel disabled; each control scope is limited to 60 s. MANUAL and arming remain required.",flush=True)
            try:
                if args.ui:
                    from backend.launch import launch_viewer
                    ui = launch_viewer(attach=endpoint)
                    deadline = time.monotonic()+args.startup_timeout
                    while not mc.state().get("viewer_attached"):
                        if ui.poll() is not None: raise RuntimeError("Viewer exited before attachment")
                        if time.monotonic() >= deadline: raise TimeoutError("Viewer attachment timed out")
                        time.sleep(.1)
                if args.mode == "live":
                    def report(state, blockers):
                        save_json(directory/"startup.json",{
                            "stage":"vehicle_readiness","blockers":blockers,
                            "confirmed":state.get("vehicle_confirmed"),
                            "vehicle":{key:state.get(key) for key in ("connected","mode","armed","fix","satellites","lat","lon")},
                        })
                    wait_for_vehicle(mc,args.startup_timeout,report=report,no_gps=mc.testing_without_gps,require_control=False)
                execute(entries,mc,directory)
            except (Exception, KeyboardInterrupt) as exc:
                if ui and ui.poll() is None:
                    mc._call("release")
                    if not (directory/"results.json").exists():
                        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
                        save_json(directory/"results.json",{"mode":args.mode,"status":status,"stage":"startup","error":str(exc),"results":[]})
                        (directory/"failure.txt").write_text(traceback.format_exc())
                    print(f"Batch stopped: {exc}\nUI and manual controls remain available. No experiment will resume automatically. Close the window to finish.\nDetails: {directory}",file=sys.stderr,flush=True)
                    wait_for_ui(ui)
                raise
            else:
                print(f"Completed {len(entries)} runs. Results: {directory/'results.json'}",flush=True)
                if ui and ui.poll() is None:
                    mc._call("release")
                    print("UI and manual controls remain available. Close the window to finish.",flush=True)
                    wait_for_ui(ui)
        return 0
    except KeyboardInterrupt:
        if not (directory/"results.json").exists():
            save_json(directory/"results.json",{"mode":args.mode,"status":"interrupted","stage":"startup","results":[]})
        print(f"Batch interrupted; partial results: {directory}",file=sys.stderr)
        return 130
    except Exception as exc:
        if not (directory/"results.json").exists():
            save_json(directory/"results.json",{"mode":args.mode,"status":"failed","stage":"startup","error":str(exc),"results":[]})
            (directory/"failure.txt").write_text(traceback.format_exc())
        print(f"Batch stopped: {exc}\nDetails: {directory}",file=sys.stderr)
        return 1
    finally:
        if ui:
            try: ui.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ui.terminate()
                ui.wait(timeout=10)


if __name__ == "__main__": raise SystemExit(main())
