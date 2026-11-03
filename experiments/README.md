# Lake experiments

Run the suite with one managed backend and separate recordings:

```bash
uv run mission-control run experiments/ --mode simulation --ui
uv run mission-control run experiments/ --mode live --ui --connection udpin:0.0.0.0:14550
```

Omit `--ui` for headless execution. Use `--list` to inspect execution order.

## Actual route and finish conditions

`batch.json` defines a west–east corridor north of every fountain exclusion:

- Home/west: **25.75830, −80.37380**.
- East: **25.75830, −80.37355**.
- Eastward starting heading: **90°**.
- AUTO transfer speed: **1 m/s**.

The complete segment lies inside the shoreline's inner padding and outside the
fountain padding. It is validated against the currently loaded geometry.

| Case | Experiment motion | Finish condition |
|---|---|---|
| Acceleration/deceleration | Follow home → midpoint → east; position-based throttle rises 0→100%, then falls 100→0% | Arrive within 1 m of east |
| Straight 20%, 50%, 80% | Follow home → east at the selected throttle demand, with steering corrections | Arrive within 1 m of east |
| Three constant-differential turns | Smooth entry/exit, mean demand 30%, differentials 10%, 20%, 40% | Timed maneuver, then AUTO directly home |
| Zigzag | Smooth alternating left/right differential | Timed maneuver, then AUTO to east |
| Spin | Opposite ±20% demand | Timed maneuver, then AUTO directly home |

The east waypoint is now an actual destination. Short timer expiry is **not**
success for the straight cases. Their duration depends on motion: a slower run
takes longer to reach east. The simulation's 20% run takes about a minute for this
25 m segment. The acceleration route has an explicit 5% launch minimum to leave
rest, then uses its position-based profile. Path tracking remains subject to the
600 s default plan timeout, telemetry and operating-area checks.

Circle turns and spins return directly to the starting/home waypoint after their
maneuver; they do not visit east. Straight cases reach east during the experiment.
Zigzag retains its separate AUTO transfer to east before returning home. All cases
align east at home before the next case. Initial home setup runs before the first
case. A failed experiment or transfer stops the batch without further movement.

A file can set `VISIT_END = False` to bypass the optional outbound endpoint
transfer while retaining the shared home return. The circle and spin files use
this setting; the runner does not special-case their names.

Simulation preserves pose between phases and scopes: no teleporting to a new
start. On the live boat, transit uses an uploaded ArduRover AUTO waypoint mission,
then confirmed MANUAL and bounded differential thrust for heading alignment.
A batch home is not a change to the autopilot's RTL home.

## Inputs and control

These are normalized throttle demands, not measured thrust, power or guaranteed
speed. Straight cases use a path follower to reach their endpoint; steering may
introduce a left/right differential. No speed-feedback controller is imposed.

Turn/zigzag/spin inputs use smooth finite profiles evaluated in the backend on
every nominal 50 Hz tick. Steady/turn/spin entry and exit transitions take 0.5 s;
zigzag reverses the differential without inserted neutral gaps. Position-based
straight profiles are also evaluated on every backend tick. Values remain
floating point until final integer PWM encoding. Digital control necessarily has
an update rate and finite actuator resolution.

## Live requirements

The runner waits for connection, MANUAL mode and arming. It never arms or changes
vehicle parameters automatically. The suite uses the existing verified mapping:

- `PILOT_STEER_TYPE=0` (standard throttle/steering) or `1` (two paddles).
  The adapter converts left/right requests to mean throttle and differential
  steering in mode 0; mode 1 uses native paddle inputs. Mode 0 reverse steering
  is compensated for the default pilot reversing behavior.
- `SERVO1_FUNCTION=73`, `SERVO3_FUNCTION=74`.
- `RCMAP_ROLL=1`, `RCMAP_THROTTLE=3`.
- Valid reported RC inputs 1/3 min/trim/max and reversal settings. Calibration
  is read from the boat, not assumed to be 1000/1500/2000.

A fresh parameter snapshot is required on acquisition. The active adapter binds
that mapping and rejects reported changes; a long straight run does not fail
merely because sixty seconds passed. Physical wiring, output polarity and reverse
capability still need verification. No real boat was used for these tests.

AUTO transfer segments are validated as well as experiment paths. A direct segment
that crosses an exclusion is rejected; this is not obstacle-aware replanning.
Neutral does not actively brake the boat. Configure clearance for its footprint,
localization uncertainty, drift and coasting.

## Recordings and script structure

Each batch prints its directory under `logs/batches/<unique batch>/` (respecting
the configured logs root). `results.json` lists outcomes. Each case has an
MCAP/JSONL recording, manifest, source snapshot and area snapshot under `runs/`.
Path plans are also saved. `plan_started`, `auto_transfer_started`, and
`navigation_phase` distinguish experiment execution, travel to east and home
return. Partial data is preserved on failure. Open MCAP files in the existing UI.

Files export `run(mc)` or an ordered `EXPERIMENTS` mapping. Core control, path and
recording APIs remain general. `_common.py` shares coordinates and profile helpers;
`direct_control.py` and `route_from_map.py` are standalone SDK examples and are not
additional batch cases.

## Individual cases and testing without GPS

Run one family by passing its file:

```bash
uv run mission-control run experiments/01_acceleration.py --mode simulation --ui
uv run mission-control run experiments/05_spin.py --mode live --ui
```

Choose just one named case from a multi-case file:

```bash
uv run mission-control run experiments/02_straight_runs.py --only straight-20 --mode live --ui
uv run mission-control run experiments/03_constant_turns.py --only turn-difference-10 --mode simulation --ui
```

`--only` may be repeated. Unknown names fail before a backend starts. Use
`uv run mission-control run experiments/ --list` to see the exact names.

For **live motor testing without GPS**, explicitly add `--no-gps`:

```bash
uv run mission-control run experiments/05_spin.py --mode live --no-gps --ui
uv run mission-control run experiments/02_straight_runs.py --only straight-20 --mode live --no-gps --ui
uv run mission-control run experiments/ --mode live --no-gps --ui
```

This mode disables position/fix readiness requirements, shoreline/fountain checks,
waypoint following, AUTO transfers and automatic home returns. There is **no
geographic protection** in this mode. Records and the console explicitly identify
that the bypass is enabled. It does not substitute a fake GPS position.

MANUAL mode, operator arming, fresh autopilot heartbeats, verified input mapping,
command expiry, source recording and Stop remain active. Each control scope is
limited to 60 seconds; profiles longer than that are rejected. The flag does not
change autopilot arming checks, parameters or failsafe settings.

The acceleration case becomes its original finite 10 s time-based profile. The
20/50/80% straight cases become finite 5 s equal-throttle tests; they do not attempt
to reach east. Turns, zigzag and spin keep their timed input profiles. All nine
cases can therefore run as motor tests without issuing navigation commands.
Normal GPS mode keeps the waypoint finish conditions and home policy above.

## What the geographic guard does

In normal mode, complete planned segments are checked against the source
shoreline's inner clearance and fountain exclusion radii. During direct movement,
current position is checked again; an additional clearance reserve of
`abs(measured_speed) * 0.5 s` stops commands when too close to a shoreline or
fountain. AUTO transfer segments are checked at submission and during travel.

A violation revokes SDK ownership, requests neutral and stops the batch. It does
not steer around the fountain, actively brake, or guarantee containment against
coasting/current/localization errors. This is application-side enforcement, not
an independently configured onboard ArduPilot geofence. Keep those distinctions
in mind when selecting clearance and vehicle-side failsafes.

Startup waits for the autopilot heartbeat to confirm MANUAL and armed state;
merely sending an arm/mode request does not count as readiness. With `--ui`, a
failed batch now leaves the connection and UI open for inspection. SDK execution
is cancelled and does not resume automatically. Close the UI to finish that runner,
then rerun the command when ready. Headless runs still exit on failure.


For example, the boat's reported RC1 1100/1500/1901 and RC3 1100/1500/1900
calibration is supported directly. No `PILOT_STEER_TYPE` change is needed.
Autopilot mixer asymmetry, steering expo, deadbands, slew limits and output limits
still apply; normalized API demands are not guaranteed physical thruster output.


Default batch layout:

```text
logs/batches/<unique batch>/
    results.json
    backend.log
    session/                 # Full backend session MCAP/JSONL/RRD
    runs/
        <experiment>/<unique run>/
            manifest.json
            recording.mcap
            recording.jsonl
            source.py
```

`--output-dir` overrides the batch root when explicitly supplied. Older folders
already saved under `batches/` remain where they were.
