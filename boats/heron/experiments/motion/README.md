# Lake experiments

Run the numbered suite with one managed backend and separate recordings:

```bash
uv run mission-control --boat heron run boats/heron/experiments/motion/ --mode simulation --ui
uv run mission-control --boat heron run boats/heron/experiments/motion/ --mode live --ui --connection udpin:0.0.0.0:14550
uv run mission-control --boat heron run boats/heron/experiments/motion/ --list
```

Omit `--ui` for headless execution. Select a file or use `--only NAME` to select a
case. The suite retains ten cases in five numbered files. The old standalone
`direct_control.py` and `route_from_map.py` examples have been removed.

## Movement goals

Acceleration and straight runs specify thrust input along the start-to-end path.
They do not use a speed setpoint or a timed speed ramp. Turns, zigzag and spin
retain their movement commands. The backend owns execution and steering.

| File | Cases | Requested movement |
|---|---|---|
| `01_acceleration.py` | One | Start → midpoint: thrust input rises linearly 0→100%; midpoint → end: falls linearly 100→0%, based on distance |
| `02_straight_runs.py` | Four | Start → end at constant 25%, 50%, 75% or 100% thrust input |
| `03_constant_turns.py` | Three | Travel at 0.6 m/s for 5 s, turning clockwise at 6, 12 or 24°/s |
| `04_zigzag.py` | One | Four 3 s moves at 0.6 m/s, alternating headings 70° and 110° |
| `05_spin.py` | One | Turn to heading 270° without requesting forward motion |

Thrust percentages are normalized requested inputs, not guaranteed physical
force or speed. Steering corrections can introduce a left/right differential.
For acceleration, the requested profile is 0% at start, 50% at one-quarter of the
route, 100% at midpoint, 50% at three-quarters, and 0% at end. Internal waypoint
transitions do not use the endpoint arrival radius to shortcut the midpoint.
The explicitly enabled 5% launch assist overrides the initial profile while its
value is below 5%, so the stationary boat can get moving. It ends when the
position-based profile reaches 5%; it is not reapplied during deceleration.

Arrival uses a 1 m endpoint tolerance and requests neutral. The profile tends
toward 0% as the boat approaches the endpoint; entering the arrival tolerance
ends execution and commands 0%. Deceleration reduces forward input; it does not
apply reverse thrust or active braking. There is no fixed experiment duration
for these paths; the plan's 600 s timeout is a failure limit.

Each timed movement in the other files ends with a neutral request; zigzag has a
neutral boundary between legs. Heading turns do not hold position against drift.
The simulator has no inertia and cannot validate physical stopping distance or
live controller tuning.

## Geographic setup and finish conditions

`corridor.json` defines a west–east corridor north of the fountain exclusions:

- Home/west: **25.75830, −80.37380**.
- East: **25.75830, −80.37355**.
- Starting heading: **90°**.
- AUTO transfer speed: **1 m/s**.

The backend validates geographic clearance before and during movement. Every experiment explicitly navigates to START and aligns east before its
maneuver. Acceleration and all four straight cases reach END through their path
plans, then explicitly navigate to START. Zigzag explicitly navigates to END
after its timed moves, then explicitly returns to START. Turns and spin return
directly to START. The return calls include eastward heading alignment.
A failed movement or transfer stops the batch without further movement.

Simulation preserves pose between scopes and transfers. Explicit `mc.navigate()` calls
use the existing verified AUTO transfer in live mode, then restore confirmed
MANUAL. Movement goals themselves run the backend feedback controller in MANUAL.

The batch runner never positions the boat, visits an endpoint or returns it home.
It discovers and runs experiments, records them, enforces readiness and releases
control on exit or failure. `_common.py` contains only coordinates and settings;
it has no movement helpers. Every navigation call is visible in the numbered
experiment that requests it.

## Live execution

The runner waits for connection and GPS readiness. Each experiment uses
`with mc.armed_manual():` to select MANUAL, arm, execute, and request disarming
on exit or failure. Setup waits for heartbeat confirmation in live mode and is
a no-op in simulation. Errors propagate; if cleanup also fails, both errors are
reported together.

Live SDK execution sends raw normalized MAVLink `MANUAL_CONTROL` steering and
throttle (`y` and `z`), with no RC calibration queries, app-side PWM conversion,
or RC overrides. It expects standard Rover throttle/steering mixing
(`PILOT_STEER_TYPE=0`). Autopilot-side calibration, mixing, motor limits and
wiring still affect physical outputs; no firmware parameters are changed.

Movement goals require fresh position, heading and ground speed. The numbered
suite cannot run with `--no-gps`: it fails rather than interpreting a speed as a
throttle percentage. Raw `set()`, `hold()` and `play()` remain available in the SDK
for explicit motor tests. Initial speed/heading gains have been tested in
simulation only; live response still needs validation on the boat.

Geographic checks stop on unsafe clearance; they do not steer around exclusions
or guarantee containment against drift or coasting. Neutral does not actively
brake. Reverse travel and geographic position holding are not movement goals in
this version.

## Recordings

Each batch writes under the shared log root (including `MISSION_CONTROL_LOG_DIR`
/ `HERON_LOG_DIR` overrides), or the explicit `--output-dir`:

```text
logs/batches/<unique batch>/
    results.json
    backend.log
    session/
    runs/<experiment>/<unique run>/
        manifest.json
        recording.mcap
        recording.jsonl
        recording.rrd
        source.py
```

Movement requests, feedback targets and actual motor commands are recorded with
the existing telemetry. Failures preserve partial recordings and their reasons.
Each per-run RRD also includes camera frames received during that run when the
camera is streaming; image bytes are not copied to MCAP/JSONL. The shared session
RRD remains available for the complete batch.
Closing the attached UI detaches it; the batch continues. A failed batch with
`--ui` leaves the connection open for inspection; close it before rerunning.
