# Programmatic access

The synchronous SDK uses the existing backend and UI. Batch execution starts its
own backend automatically; no separate app startup or connection file is needed.

```bash
uv run mission-control run experiments/ --mode simulation
uv run mission-control run experiments/ --mode simulation --ui
```

The second command attaches the existing UI for live monitoring. Closing that UI
detaches it; the batch continues. When the batch ends, it closes its backend and
attached UI. The first UI run may build the native viewer from current sources.
The layout and controls are unchanged.

Run one file, choose a log root, or list the order without starting a backend:

```bash
uv run mission-control run experiments/direct_control.py --mode simulation
uv run mission-control run experiments/ --output-dir ./logs/batches
uv run mission-control run experiments/ --list
```

Each file defines an ordinary synchronous function:

```python
def run(mc):
    from backend.sdk import Waypoint
    with mc.control(kind="thrusters", start=Waypoint(25.7581, -80.37365)) as control:
        control.hold(3, left=0.2, right=-0.2)
```

For several experiments in one file, expose an ordered mapping instead:

```python
def straight(mc):
    with mc.control(start=start) as control:
        control.hold(5, throttle=0.2, steering=0)

def turn(mc):
    with mc.control(start=start) as control:
        control.hold(5, throttle=0.2, steering=0.1)

EXPERIMENTS = {"straight": straight, "turn": turn}
```

Supply `start` in your code or get coordinates from a saved route. The runner
recursively discovers sorted Python files (ignoring names starting with `_`),
then uses mapping insertion order within each file. Helpers without either entry
point are ignored. `__main__` blocks do not execute. Keep imports free of movement
side effects. No special experiment classes or perception framework are required.

One backend is shared, but every function gets its own recording scope. Do not
add another `mc.record()` inside a batch function; the runner manages that scope.
`mc.run(name, plan)` works inside a batch, sharing the function's recording. Its
handle waits for path completion rather than waiting for the enclosing recording
to close. Async path executions still in progress when a function returns are
awaited before advancing. Direct controls are neutralized and released between
functions; a live batch also waits for the neutral transmission period.

Each batch writes `results.json`, `backend.log`, and separate run directories
under `logs/batches/<UTC timestamp + unique suffix>/` (or the configured log
root). The backend session recordings are kept in that batch's `session/`
subfolder; experiment recordings remain under its `runs/` subfolder. Every run includes its source
file and SHA-256 hash, metadata, area snapshot, MCAP/JSONL logs and any path
snapshots. Failure or Ctrl+C preserves partial recordings and stops the batch;
there is no automatic continue-on-failure. A folder-level `batch.json` can
configure a verified AUTO home transfer after successful cases.

**Live batches are explicit:**

```bash
uv run mission-control run my_experiments/ --mode live --ui --connection udpin:0.0.0.0:14550
```

The runner waits for connection, MANUAL mode and arming, then reads parameters.
Use the existing UI or your vehicle's operator controls to establish that state.
The runner does not change configuration, modes or arming. `mc.control()` and
`mc.run()` inherit the selected batch mode; a batch backend rejects a conflicting
mode requested by a script. A home transfer can be configured in `batch.json`; other repositioning and
mapping changes between live experiments must be explicit. The maneuver suite uses two-paddle thruster inputs consistently; live mappings
still need to match the vehicle as described below.

For ad hoc scripts that attach to an already running app, start
`uv run mission-control --demo` first, then use `MissionControl.connect()` as
shown below. The standalone example commands remain available with
`uv run python -m experiments.direct_control` and
`uv run python -m experiments.route_from_map`.

```python
from backend.sdk import MissionControl

with MissionControl.connect() as mc:
    print(mc.state())                 # Does not renew the joystick/UI lease.
    points = mc.routes.get()          # Current clickable map route.
    mc.routes.save("test-line")       # Persist a named coordinate draft.
    points = mc.routes.get("test-line")
    print(mc.area())                 # Lake geometry and revision.
```

The private discovery file is `.mission-control/connection.json` in the data
root, with owner-only permissions. `MISSION_CONTROL_ENDPOINT_FILE` selects an
explicit descriptor when more than one backend is running. The backend owns the
vehicle connection; scripts never open a second MAVLink transport. Discovery is
currently local to the same machine and checkout/data root.

## Controls

Controls are general normalized demands, not physical thrust in newtons, motor
RPM, electrical power, or guaranteed speed. `-1` is reverse/left, `0` is neutral,
and `1` is forward/right, depending on the axis. A control scope exclusively owns
output until release, operator takeover, failure, or command expiry.

```python
with MissionControl.connect() as mc:
    start = mc.routes.get()[0]
    with mc.record("my-run", metadata={"condition": "example"}) as run:
        with mc.control(start=start) as control:  # Defaults to simulation.
            control.hold(5, throttle=0.2, steering=0.0)
    print(run.directory)
```

`set()` sends one target with a default 250 ms lifetime (50 ms–1 s allowed).
Refresh it in your own loop. `hold()` is just a set/sleep utility that refreshes
at 20 Hz and requests neutral afterward. It does not define a new experiment type.
Nothing automatically keeps an old nonzero command alive if your algorithm stalls.
A control scope with no command expires after two seconds.

```python
with mc.control(kind="thrusters", start=start) as control:
    control.hold(3, left=0.2, right=-0.2)

# Your own controller, perception system, or data source can produce these values:
with mc.control(start=start) as control:
    for observation in observations:
        throttle, steering = algorithm(observation, mc.state())
        control.set(throttle=throttle, steering=steering, valid_for_s=0.25)
```

These snippets illustrate the API; `observations`, `algorithm`, and `start` are
supplied by the caller. No perception framework is imposed.

**Live execution is explicit:**

```python
mc.read_parameters()
with mc.control(mode="live", kind="axes") as control:
    control.hold(2, throttle=0.2, steering=0.0)

# Or, for a verified two-paddle configuration:
with mc.control(mode="live", kind="thrusters") as control:
    control.hold(2, left=0.2, right=-0.2)
```

Select MANUAL and arm through the existing UI before live acquisition. The SDK
never arms, changes modes, or changes parameters automatically. Live mode never
falls back to simulation. Start coordinates only initialize simulation; live
control always uses vehicle telemetry.

The live adapter verifies `SERVO1_FUNCTION=73`, `SERVO3_FUNCTION=74`,
`RCMAP_ROLL=1`, and `RCMAP_THROTTLE=3`. Both `PILOT_STEER_TYPE=0` and `1` are
supported. For standard mode 0, left/right demands are converted to mean throttle
and differential steering, compensating the pilot's reverse steering convention.
For mode 1, left/right inputs use the native two-paddle mapping.

The adapter reads RC1/RC3 min, trim, max and reversal from the vehicle. Each range
must satisfy `800 <= min < trim < max <= 2200`. PWM uses the appropriate positive
or negative span around trim; neutral uses the reported trim. No calibration or
pilot-mode parameters are changed. Physical wiring, reverse-capable ESC operation,
and autopilot shaping/limits still determine the actual motor response.

A fresh parameter snapshot is required at acquisition. The active adapter binds
the verified mapping and rejects reported changes; elapsed time alone does not
expire that mapping during a long execution. Position and heading must be no
more than one second old; the existing heartbeat check is also enforced. The UI's
Stop, joystick-enable, mode-change, and mission commands revoke SDK ownership.

## Paths

Path following is a utility above the same bounded output interface. It runs in
the backend, independently of the script. The first implementation is a small
lookahead/heading controller; it is not a calibrated dynamics controller.

```python
from backend.sdk import Plan, Leg, Linear, Constant

A, B, C = mc.routes.get("test-line")
plan = Plan(
    legs=[Leg(A, B, Linear(0, 1)), Leg(B, C, Linear(1, 0))],
    launch_throttle=0.05,
)
print(mc.validate(plan))
run = mc.run("ramp", plan)             # mode="simulation" by default.
# run = mc.run("ramp", plan, mode="live")
run.wait_until_finished()
```

A linear profile follows position along its active segment. Starting at zero
would leave a stationary boat at zero forever. Explicit `launch_throttle=0.05`
provides a 5% minimum on the first leg until its profile reaches 5%. There is no
implicit launch default. Constant-throttle paths do not need launch assistance.
Zero throttle means neutral, not active braking or position holding. Arrival uses
`arrival_radius_m` (default 1 m); the final action is neutral. A live path starts
only if the boat is already within that radius of the first waypoint.

Every run validates all segments against the source shoreline, its configured
padding, and fountain exclusions. Endpoint-only checks or sampled checks are not
used. Validation fails closed if geometry is unavailable. During execution, stale
telemetry, excess tracking error, an area change, expiry, and insufficient
short-horizon clearance revoke control and request neutral. This is a boundary
intervention mechanism, not a guarantee of containment under drift or coasting.
Set lake padding to account for the boat footprint and uncertainty. Turning and
reverse behavior require vehicle testing before relying on path tracking.

`mc.routes.set(points)` changes the existing map draft while idle/disarmed;
`save(name)` persists it, `load(name)` restores it into the map, and `names()` lists
drafts. `wait_for_points(count, timeout=...)` can wait for edits in the existing
map, without adding a new picking dialog. Runs snapshot their plan and operating
area, so subsequent draft edits do not change the saved run.

## Recordings

Each `mc.record()` scope or `mc.run()` creates a unique directory:

```text
runs/<name>/<UTC timestamp + unique suffix>/
    manifest.json
    area.geojson
    plan.json          # For path execution.
    recording.mcap
    recording.jsonl
```

The files contain bounded copies of received/sent MAVLink traffic, joystick data,
and SDK commands/state/events. `/input/CONTROL` fields appear in the existing
plot catalog. The current route and simulated position use the existing map and
telemetry stream. Simulated motion is a kinematic preview, not evidence of actual
thruster response. The global UI/session recording continues as before. Camera
imagery is not copied into scoped MCAP files by this version.

Files use the recorder's existing `.part` publication behavior and are published
after closing. Failed/cancelled runs preserve partial data and reasons. Recording
errors are reported in the manifest. `mc.runs()` lists saved manifests; after a
backend crash, runs belonging to a dead process are marked interrupted when read.
An interrupted `.part` file remains partial and may require MCAP recovery.

`output_dir=` on `record()` or `run()` chooses a root on the **backend machine**.
Repeated names create distinct directories. Closing the SDK connection does not
stop a backend-owned path run. Direct controls expire when updates stop. In ordinary app-owned mode, closing the UI terminates its backend and requests
neutral. In batch mode, the runner owns that lifetime and closing the UI only
detaches it.

To inspect a scoped run later, open its `recording.mcap` using the existing
recording loader. The live viewer remains the existing session recording.

## Code boundaries

- `backend/sdk/`: synchronous transport, discovery, and small caller conveniences.
- `backend/control/planning.py`, `navigation.py`: pure path data and guidance.
- `backend/control/service.py`: ownership, expiry, execution coordination.
- `backend/control/adapter.py`: normalized demands to MAVLink or simulation.
- `backend/mapping/constraints.py`: metric geometry and continuous clearance checks.
- `backend/control/recording.py`, `storage.py`: scoped logs and durable manifests.

The scripts under `experiments/` demonstrate these primitives; no maneuver-specific
classes are part of the core API.

For online replanning, an axes scope can atomically replace its path:

```python
with mc.control(start=start) as control:
    for observation in observations:
        plan = planner(observation, mc.state())
        control.follow(plan, valid_for_s=0.5)
```

Each replacement is validated before installation. Rejection preserves the old
intent until its existing expiry; the algorithm can catch the error and replan.
A subsequent `set()` replaces path following with direct controls. Completing a
short-lived path commands neutral while waiting for the next input; a missing
update expires the scope. These streaming paths are distinct from `mc.run()`,
which owns a complete saved execution and its recording.


## Backend profiles and AUTO transfers

`control.play([(0, target), (5, peak), (10, neutral)])` sends a finite profile to
the backend. It uses smoothstep interpolation on every nominal 50 Hz control tick,
preserves fractional requested values, and waits for completion. The final value
is held briefly to permit a subsequent profile; a missing follow-up still expires
control. Cancelling the scope requests neutral immediately. `set()` remains the
low-latency interface for a caller-provided feedback controller.

`mc.navigate(point, speed_mps=1, heading_deg=90)` validates the current-to-target
segment. In live mode it uploads an AUTO waypoint mission without arming, verifies
mode transitions, restores MANUAL and optionally aligns the arrival heading with
bounded differential thrust. Simulation travels through the same geometry and
preserves pose. Home transfer data is included in the enclosing recording.

A folder-level `batch.json` containing `home`, `heading_deg`, and `speed_mps`
enables initial home setup and post-success return for that folder's cases. The
bundled maneuvers define a northern west–east safe corridor there. Existing UI
controls are unchanged. Return paths must themselves be valid; this feature does
not add obstacle-aware route planning around exclusions.


When a folder's `batch.json` contains both `home` and `end`, the runner ensures the
end waypoint is reached before returning home. Straight experiment scripts use
`mc.run()` with an actual home-to-end plan. Files default to a distinct AUTO transfer to `end` when needed, followed by home.
A file may declare `VISIT_END = False` to return directly home; the bundled circle
and spin experiments do this.
Transfer phases are recorded separately from the experiment's input sequence.

For individual files, pass that file instead of the directory. `--only NAME`
selects a named function from its `EXPERIMENTS` mapping and can be repeated.

`--mode live --no-gps` is an explicit motor-testing mode. It disables geographic
protection and all waypoint/AUTO navigation, keeps heartbeat/arming/MANUAL/mapping
checks, and limits each control scope to 60 seconds. The backend refuses navigation
APIs in this mode. `mc.testing_without_gps` lets a script select a finite input-only
variant. The bundled acceleration and straight cases supply these variants. The
runner skips all configured east/home transfers, and logs mark the GPS bypass.
No firmware arming checks or failsafe parameters are changed.


Batch output uses the shared `log_directory()` root, including
`MISSION_CONTROL_LOG_DIR` / `HERON_LOG_DIR` overrides. Each batch owns both its
`session/` backend recordings and `runs/` per-case recordings. `--output-dir`
remains an explicit override for the batch root. Existing batch folders are not
moved or rewritten.
