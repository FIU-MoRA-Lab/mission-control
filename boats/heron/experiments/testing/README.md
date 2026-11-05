# Isolated lab thruster test

`01_thrusters.py` currently tests only the **right thruster at 99% for 20 seconds**.
It uses `mc.test_thruster()` and Rover's `MAV_CMD_DO_MOTOR_TEST`, selecting the
right motor directly (test order 4; left is 3). It does not encode a one-motor
request as steering plus throttle.

```python
mc.test_thruster("right", throttle_pct=99, duration_s=20)
mc.test_thruster("left", throttle_pct=40, duration_s=20)
```

Requires a live `--no-gps` backend. Percentages must be nonzero whole numbers
from -100 to 100; duration must be positive and at most 30 seconds, Rover's
native test limit. Each call selects MANUAL, confirms disarming before the test,
then lets the firmware arm for the test. It checks the command acknowledgement,
monitors the vehicle link, stops on cancellation/kill, and confirms disarming
on exit. Do not wrap these calls in `armed_manual()`.

The firmware owns the timeout, including if the client or backend disappears.
Rover temporarily suspends its GCS, throttle and crash failsafes during a native
motor test and restores them when it stops. The app does not change saved
firmware parameters. The command uses configured motor functions and output
limits; verify the physical left/right wiring. A successful acknowledgement
is not measured motor RPM or proof that an ESC's neutral calibration is correct.

```bash
uv run mission-control --boat heron run boats/heron/experiments/testing/ --mode live --no-gps --ui \
  --connection udpin:0.0.0.0:14550
```

`--no-gps` disables geographic protection and navigation. A latched kill switch
must still be reset explicitly in the UI. Running this experiment requests motor
operation; the native test uses normal firmware motor-test checks.

The generic `run_thruster()` / `run_thrusters()` APIs remain available for mixed
steering/throttle control and simulation. Their unused-side zero is an input
intent, not an independent motor-output clamp. In the November 4 right-only 99%
recording (`20261104T225647_44786536`), the left output held about 1499 µs versus
1500 µs neutral, with a startup sample at 1479 µs. RC calibration, dead zones,
integer rounding and throttle slew can prevent exact cancellation in the mixer.
Use `test_thruster()` for an isolated bench test instead.
