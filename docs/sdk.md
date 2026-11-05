# Programmatic access

The synchronous SDK uses the existing backend and UI. By default, batch execution
starts its own backend automatically; no separate app startup is needed.

To run experiments from a console while Mission Control is already open:

```bash
uv run mission-control --boat heron run boats/heron/experiments/motion/ --attach --mode simulation
uv run mission-control --boat heron run boats/heron/experiments/motion/02_straight_runs.py --attach --mode live
```

`--attach` uses SDK discovery to reuse the running app's backend, vehicle
connection, map and session recording. It starts no backend or window. The
command exits after completion, failure or Ctrl+C; experiment controls are
released and the app remains available. Per-case recordings and `results.json`
are saved under the usual batch directory; the shared session recording stays
in the app's existing session directory. Live execution requires `--mode live`.
The app must remain open for the duration of the experiment.

Use `--only NAME` to select cases, or `--endpoint-file /path/to/connection.json`
with `--attach` to choose a specific backend. A missing or unreachable backend
fails without starting a replacement. Finish existing SDK control or recordings
first. `--ui`, `--connection` and `--mission-file` configure a new session and
cannot be combined with `--attach`. No-GPS testing inherits the running backend's
setting; `--no-gps` with `--attach` verifies that it was already enabled at startup
and cannot enable it on an ordinary app.

To start a separate backend instead:

```bash
uv run mission-control --boat heron run boats/heron/experiments/motion/ --mode simulation
uv run mission-control --boat heron run boats/heron/experiments/motion/ --mode simulation --ui
```

The second command attaches the existing UI for live monitoring. Closing that UI
detaches it; the batch continues. After completion, failure, or Ctrl+C, the runner
releases SDK control and keeps the backend and attached UI alive until you close
the window. You can continue controlling the boat through the UI or gamepad;
re-arm in the UI if the experiment disarmed on exit. Ctrl+C while waiting for
the window does not close it. Without an attached UI, the backend closes when
the batch ends. The first UI run may build the native viewer from current sources.
The layout and controls are unchanged.

Run one file, choose a log root, or list the order without starting a backend:

```bash
uv run mission-control --boat heron run boats/heron/experiments/motion/02_straight_runs.py --mode simulation
uv run mission-control --boat heron run boats/heron/experiments/motion/ --output-dir ./logs/batches
uv run mission-control --boat heron run boats/heron/experiments/motion/ --list
```

Use `boats/heron/experiments/testing/` for [independent timed thruster tests](../boats/heron/experiments/testing/README.md).
Choose a subfolder to run only that suite; `boats/heron/experiments/` recursively includes both.

Each file defines an ordinary synchronous function:

```python
def run(mc):
    from boats.heron.backend.sdk import Waypoint
    with mc.control(start=Waypoint(25.7581, -80.37365)) as control:
        control.move(speed_mps=0.6, heading_deg=90, duration_s=5)
```

For several experiments in one file, expose an ordered mapping instead:

```python
def straight(mc):
    with mc.control(start=start) as control:
        control.move(speed_mps=0.5, heading_deg=90, duration_s=5)

def turn(mc):
    with mc.control(start=start) as control:
        control.move(speed_mps=0.5, turn_rate_deg_s=12, duration_s=5)

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
file and SHA-256 hash, metadata, area snapshot, MCAP/JSONL/RRD logs and any path
snapshots. Failure or Ctrl+C preserves partial recordings and stops the batch;
there is no automatic continue-on-failure. The runner never issues navigation
commands or reads movement policy from a configuration file. Initial positioning,
endpoint visits and return movements must be explicit in each experiment.

**Live batches are explicit:**

```bash
uv run mission-control --boat heron run my_experiments/ --mode live --ui --connection udpin:0.0.0.0:14550
```

The runner waits for connection and, unless `--no-gps` is selected, GPS readiness.
Each bundled experiment uses `with mc.armed_manual():` to select MANUAL, arm,
run its work, and request disarming on exit. Live execution requests arming when
that scope starts; it does not wait for prior UI arming. Setup waits for vehicle
heartbeat confirmation. Simulation sends no vehicle actions. Errors propagate;
if both the experiment and disarming fail, both errors are reported together.
The runner itself does not select modes or arm. `mc.control()` and
`mc.run()` inherit the selected batch mode; a batch backend rejects a conflicting
mode requested by a script. Repositioning and mapping changes must be explicit
in the experiment code. The numbered acceleration/straight experiments use position-based thrust paths;
other cases use speed/heading goals. Live mappings
still need to match the vehicle as described below.

For ad hoc scripts that attach to an already running app, start
`uv run mission-control --boat heron --demo` first, then use `MissionControl.connect()` as
shown below. The numbered scripts in `boats/heron/experiments/motion/` are the runnable examples.

```python
from boats.heron.backend.sdk import MissionControl

with MissionControl.connect() as mc:
    print(mc.state())                 # Does not renew the joystick/UI lease.
    points = mc.routes.get()          # Current clickable map route.
    mc.routes.save("test-line")       # Persist a named coordinate draft.
    points = mc.routes.get("test-line")
    print(mc.area())                 # Lake geometry and revision.
```

The private discovery file is `boats/heron/.mission-control/connection.json`
beneath the shared data root, with owner-only permissions. `MISSION_CONTROL_ENDPOINT_FILE` selects an
explicit descriptor when more than one backend is running. The backend owns the
vehicle connection; scripts never open a second MAVLink transport. Discovery is
currently local to the same machine and checkout/data root.

## Movement commands

A control scope exclusively owns vehicle output. Use movement goals in physical
units; the backend computes motor inputs from measured ground speed and heading.

```python
with mc.control(start=start) as control:
    control.move(speed_mps=1.2, heading_deg=90, duration_s=5)
    control.turn_to(heading_deg=180)
    control.go_to(destination, speed_mps=0.8)
    control.stop()
```

Each call blocks until its action finishes. `move()` requires `duration_s`; the
backend owns timing, and requests neutral at the end without needing another
client message. Omitting `heading_deg` holds the measured starting heading.
`turn_to()` turns without forward thrust, waits until heading is within
`tolerance_deg=3` for 0.3 s, then requests neutral. This does not hold geographic
position against drift. `go_to()` validates the direct segment, steers toward the
destination and requests neutral within `arrival_radius_m=1`. It does not plan
around obstacles. Goal timeouts (`timeout_s=60` for turns, `180` for navigation)
fail and neutralize the scope instead of reporting success.

For curved travel, supply `turn_rate_deg_s` instead of `heading_deg`. Positive
rates rotate the heading reference clockwise; the feedback controller follows it.
For acceleration/deceleration, supply `acceleration_mps2`: the speed reference
increases from zero and decreases toward zero within the total duration. Short
moves may not reach the requested peak. These are requested speed-reference
limits, not guaranteed physical acceleration.

```python
control.move(speed_mps=0.6, turn_rate_deg_s=12, duration_s=5)
control.move(speed_mps=1, acceleration_mps2=0.2, heading_deg=90, duration_s=10)
```

Current limits are forward speed 0–2 m/s, duration 0.05–600 s, headings 0–360°,
and turn rates ±60°/s. Speed control uses bounded PI feedback; heading uses
proportional steering. Live movement requires fresh estimated position, heading,
and ground speed (from `GLOBAL_POSITION_INT` velocity or `VFR_HUD`). Initial
controller gains have been checked in the kinematic simulator, not tuned on the
physical boat. Requested speed/turn rate can exceed vehicle capability.

A replacement command cancels the previous intent. Rejected requests leave it
intact. Movement IDs prevent a cancelled caller from stopping a newer movement.
Ctrl+C during a wait requests cancellation. Backend execution is finite even if
the client disappears. Completed movements leave a two-second neutral window for
the next command, after which ownership expires. `stop()` cancels the current
movement and immediately requests neutral; neutral is not active braking.

No-GPS motor-testing mode rejects these goals instead of substituting throttle
percentages for speed. Existing raw-control methods below remain available for
motor calibration and caller-provided controllers.

## Raw controls

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
mc.run_thruster("left", throttle_pct=20, duration_s=3)   # Requests right neutral.
mc.run_thruster("right", throttle_pct=20, duration_s=3)  # Requests left neutral.
mc.run_thrusters(left_throttle_pct=30, right_throttle_pct=-20, duration_s=5)
```

`mc.run_thruster()` and `mc.run_thrusters()` validate inputs, acquire control in
the client's selected mode, maintain inputs for the duration, then request neutral
and release control. Both calls block until finished. `throttle_pct` ranges from
-100 (full reverse) to 100 (full forward); zero is neutral. It means requested
input percentage, not measured electrical power, thrust or speed. The
single-thruster call requests the other side neutral; the dual call requires both
`left_throttle_pct` and `right_throttle_pct`. Lower-level `Control.set()` and
`hold()` continue to use normalized inputs in [-1, 1]. Live `control()` scopes
send raw normalized MAVLink input without fetching RC calibration or converting
through min/trim/max, reversal or dead zones. They do not send RC PWM overrides.
Timed thruster calls use `Control.pulse()`: a finite constant target evaluated
and transmitted by the backend. They do not depend on the client's 250 ms
`set()` refresh lease. At the deadline the backend requests neutral, even if
client polling stalls or disconnects. Stop/disarm, link loss and the existing
vehicle checks can abort a pulse early. This is a bounded timed command, not a
guarantee of measured motor response. Lower-level `hold()` remains a client
set/sleep loop with short command expiry.
Use `start=Waypoint(...)` to initialize simulation, or `mode="live"` to explicitly
select live control. In no-GPS mode, each call must be shorter than 60 seconds.
See the [minimal thruster experiment](../boats/heron/experiments/testing/01_thrusters.py).

For an **isolated live bench test**, use
`mc.test_thruster("right", throttle_pct=99, duration_s=20)` with `--no-gps`.
This selects Rover's individual motor-test order (3 left, 4 right), bypassing
steering/throttle mixing. Whole, nonzero percentages in [-100, 100] and durations
in (0, 30] seconds are supported. It selects MANUAL, confirms disarming first,
checks command acceptance, and confirms disarming on exit. Do not enclose it in
`armed_manual()`. The firmware arms for the test, owns its timeout, and disarms
at completion. Rover temporarily suspends its GCS/throttle/crash failsafes while
the native test is active. Backend stop/kill/link checks remain active; a lost
connection leaves the firmware timeout as the bound. See the
[lab test notes](../boats/heron/experiments/testing/README.md).

For lower-level control within a scope:

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
with MissionControl.connect(mode="live") as mc:
    with mc.armed_manual(timeout_s=10):
        mc.run_thruster("left", throttle_pct=20, duration_s=10)
```

`set_manual()`, `arm()` and `disarm()` explicitly request vehicle state changes
and wait for confirmed telemetry, not optimistic UI state or just transmission.
They raise on link loss/rejection or confirmation timeout. `arm()` uses normal
pre-arm checks, not force-arm. The kill switch is never reset automatically.
MANUAL setup leaves joystick driving disabled. A call to `run_thruster()` or
`control()` alone does not select a mode or arm. Live mode never falls back to
simulation. Start coordinates only initialize simulation; live control uses
vehicle telemetry. Lab operation without location requires `--no-gps` at startup.

`armed_manual()` wraps the explicit setup/cleanup calls; it does not suppress
errors. It requests disarming after an experiment error or arming timeout. If
cleanup also fails, a `BaseExceptionGroup` exposes both failures. The individual
`set_manual()`, `arm()` and `disarm()` methods remain available.

The SDK sends `MANUAL_CONTROL.y` as steering and `.z` as throttle, each scaled
from normalized input to [-1000, 1000]. Neutral sends zeros. These are the axes
[ArduRover consumes](https://github.com/ArduPilot/ardupilot/blob/master/Rover/GCS_MAVLink_Rover.cpp).
The app assumes standard Rover throttle/steering mixing (`PILOT_STEER_TYPE=0`);
left/right demands become mean throttle and differential steering, with reverse
steering compensation. It does not query or adapt to two-paddle mode, motor
mapping or RC calibration. `read_parameters()` remains optional diagnostics and
is never called by control, navigation, or path execution.

Raw input is not a direct motor-output command. ArduPilot still applies its own
[manual-input processing](https://github.com/ArduPilot/ardupilot/blob/master/libraries/GCS_MAVLink/GCS_Common.cpp),
including onboard calibration and mixing; output limits, ESC settings and wiring
can affect physical response. The requested neutral side is an input intent,
not a measured guarantee of zero motor thrust. The SDK does not change firmware
parameters to bypass that processing.

Position and heading must be no more than one second old outside no-GPS mode;
the existing heartbeat check is always enforced. The UI's Stop, joystick-enable,
mode-change, and mission commands revoke SDK ownership.

## Paths

Path following is a utility above the same bounded output interface. It runs in
the backend, independently of the script. The first implementation is a small
lookahead/heading controller; it is not a calibrated dynamics controller.

```python
from boats.heron.backend.sdk import Plan, Leg, LinearThrottleRamp, ConstantThrottle

A, B, C = mc.routes.get("test-line")
plan = Plan(
    legs=[
        Leg(A, B, throttle=LinearThrottleRamp(start=0, end=1)),
        Leg(B, C, throttle=LinearThrottleRamp(start=1, end=0)),
    ],
    launch_throttle=0.05,
)
print(mc.validate(plan))
run = mc.run("ramp", plan)             # mode="simulation" by default.
# run = mc.run("ramp", plan, mode="live")
run.wait_until_finished()
```

`LinearThrottleRamp(start=0, end=1)` increases throttle from 0% to 100% with
distance along a leg; `LinearThrottleRamp(start=1, end=0)` decreases it.
`ConstantThrottle(0.25)` holds 25% throttle throughout a leg. Starting at zero
would leave a stationary boat at zero forever. Explicit `launch_throttle=0.05`
provides a 5% minimum on the first leg until its profile reaches 5%. There is no
implicit launch default. Internal profile knots are reached before switching
legs, so the final arrival tolerance does not truncate an intermediate peak.
Constant-throttle paths do not need launch assistance.
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
    recording.rrd
```

The files contain bounded copies of received/sent MAVLink traffic, joystick data,
and SDK commands/state/events. `/input/CONTROL` fields appear in the existing
plot catalog. The current route and simulated position use the existing map and
telemetry stream. Simulated motion is a kinematic preview, not evidence of actual
thruster response. The shared session RRD continues to cover the whole batch.
Each run's `recording.rrd` also includes received camera JPEG frames at
`/camera/image`, limited to that run's time window. Images use GCS `receive_time`
and the same relative `mission` timeline as that run's telemetry; these are
receipt times, not synchronized sensor-acquisition timestamps. No camera stream
means no captured video frames. UI camera-status placeholders are excluded from
per-run perception imagery. Scoped MCAP/JSONL remain telemetry/event records and
do not contain camera image bytes. The manifest's `rrd_recording` points
to the per-run RRD. Existing runs are not retroactively converted.

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

- `boats/heron/backend/sdk/`: synchronous transport, discovery, and small caller conveniences.
- `boats/heron/backend/control/planning.py`, `navigation.py`: pure path data and guidance.
- `boats/heron/backend/control/service.py`: ownership, expiry, execution coordination.
- `boats/heron/backend/control/adapter.py`: normalized demands to MAVLink or simulation.
- `boats/heron/backend/mapping/constraints.py`: metric geometry and continuous clearance checks.
- `boats/heron/backend/control/recording.py`, `storage.py`: scoped logs and durable manifests.

The scripts under `boats/heron/experiments/motion/` demonstrate these primitives; no maneuver-specific
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

The runner manages process lifetime, readiness, recording, sequential execution
and safety cleanup. It never invokes `navigate()`, chooses a destination or
inserts a return movement. Controller code owns every movement:

```python
def run(mc):
    mc.navigate(START, speed_mps=1, heading_deg=90)
    mc.run("my-path", plan).wait_until_finished()
    mc.navigate(START, speed_mps=1, heading_deg=90)
```

The numbered experiments import coordinates and numeric settings from
`boats/heron/experiments/motion/corridor.json` through `_common.py`; that module defines no movement
helpers. Each experiment contains its own positioning and return commands.
Acceleration and straight cases reach END through their explicit thrust plans.
Zigzag explicitly navigates to END after its timed maneuver and then returns to
START. Turns and spin explicitly return directly to START. `batch.json` and
`VISIT_END` have no control meaning in the runner.

The SDK waits for a previous release's neutral transmission window before
acquiring the next scope, starting a path or accepting a navigation request.
This is safety synchronization; it does not issue movement commands. If an
experiment fails, its subsequent return call is not executed; runner cleanup
releases control and preserves the failed recording.

For individual files, pass that file instead of the directory. `--only NAME`
selects a named function from its `EXPERIMENTS` mapping and can be repeated.

`--mode live --no-gps` is an explicit motor-testing mode. It disables geographic
protection and all waypoint/AUTO navigation, keeps heartbeat/arming/MANUAL
checks, and limits each control scope to 60 seconds. The backend refuses navigation
APIs in this mode. `mc.testing_without_gps` lets a custom motor-test script select a finite raw-input
variant. The numbered movement experiments require feedback and are rejected in
this mode; they do not provide raw-throttle substitutes. Logs mark the GPS bypass.
No firmware arming checks or failsafe parameters are changed.


Batch output uses the shared `log_directory()` root, including
`MISSION_CONTROL_LOG_DIR` / `HERON_LOG_DIR` overrides. Each batch owns both its
`session/` backend recordings and `runs/` per-case recordings. `--output-dir`
remains an explicit override for the batch root. Existing batch folders are not
moved or rewritten.
