# Timed motion experiments

A separate ten-case version of `boats/heron/experiments/motion/`. The originals are unchanged.
All movement uses raw normalized throttle/steering inputs and backend timing.
There is no navigation to a start point, GPS guidance, heading/speed feedback,
or automatic return. Position and orient the boat manually before a case.

```bash
# List cases without starting a backend or moving anything.
uv run mission-control --boat heron run boats/heron/experiments/motion_timed/ --list

# Preview in simulation.
uv run mission-control --boat heron run boats/heron/experiments/motion_timed/ --mode simulation --ui

# Live timed input without GPS.
uv run mission-control --boat heron run boats/heron/experiments/motion_timed/ --mode live --no-gps --ui --connection udpin:0.0.0.0:14550

# Run a single case.
uv run mission-control --boat heron run boats/heron/experiments/motion_timed/ --mode live --no-gps --ui --only timed-straight-25
```

To reuse an existing backend, replace `--ui --connection ...` with `--attach`.
For live no-GPS execution, that backend must already have no-GPS testing enabled.
Run this folder specifically; running `boats/heron/experiments/` recursively also includes
the original GPS suite and lab thruster tests.

| File | Cases | Default input and duration |
|---|---|---|
| `01_acceleration.py` | One | 0→100% throttle over 10 s, then 100→0% over 10 s; zero steering |
| `02_straight_runs.py` | Four | 25%, 50%, 75%, 100% throttle, zero steering; 10 s each |
| `03_constant_turns.py` | Three | 30% throttle with 25%, 50%, 75% clockwise steering; 10 s each |
| `04_zigzag.py` | One | 30% throttle, alternating −25%, +25%, −25%, +25% steering; 10 s per leg (40 s total) |
| `05_spin.py` | One | Zero throttle, 50% clockwise steering; 10 s |

Edit `_common.py` to adjust durations and input levels. Each timed segment is
10 seconds by default; acceleration totals 20 seconds. Acceleration uses the
SDK's smooth eased interpolation between time keyframes, rather than a linear
distance ramp. It requests neutral at completion. Constant inputs use `pulse()`,
which requests neutral at its backend deadline, including between zigzag legs.
There is no fixed pause between cases or zigzag legs.

Each case selects MANUAL, arms normally, releases control with neutral on exit
or failure, and requests disarming. A failed case stops the batch. Backend timing
continues even if client polling stalls; stop/disarm/link checks still apply.
The existing runner saves separate recordings for each case.

Inputs are percentages of requested control, not measured thrust, speed,
turn rate or heading. Zero steering does not correct heading drift. Spin does
not stop at a compass heading, and neutral does not actively brake. Live output
depends on the autopilot's standard Rover mixer (`PILOT_STEER_TYPE=0`), calibration
and wiring; these experiments do not change firmware parameters.

`--no-gps` disables geographic protection and AUTO navigation. It retains
MANUAL, arming and heartbeat checks and limits each control scope to 60 s.
Keep the complete acceleration profile and all four zigzag legs comfortably
below that limit when editing durations. Simulation uses the normal geographic
checks and can validate command execution, but cannot calibrate live input
levels or stopping distances.
