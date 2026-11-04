# Lab thruster function check

Use this experiment in the lab to observe whether each thruster responds to a
controlled throttle sweep. It needs no location, GPS, route, or navigation setup.

The SDK provides a blocking call for independent left/right inputs:

```python
mc.run_thruster("left", throttle_pct=20, duration_s=3)   # Left only.
mc.run_thruster("right", throttle_pct=20, duration_s=3)  # Right only.
mc.run_thrusters(left_throttle_pct=30, right_throttle_pct=-20, duration_s=5)
```

`throttle_pct` is requested input from -100% (full reverse) to 100% (full forward),
not measured electrical power, thrust or speed. Zero is neutral. The
single-thruster call keeps the other side neutral.
Each call maintains the requested inputs, then requests neutral and releases
control. The SDK handles validation and cleanup; the backend owns pulse timing
and command transmission, so delays in the client cannot expire a pulse after
250 ms. The backend requests neutral at the deadline even if the client stalls
or disconnects. Stop/disarm and vehicle checks can still abort a pulse.
Timing is approximate to the backend control tick.

`01_thrusters.py` sweeps **20%, 40%, 60%, 80%, 100%**, holding each input for
**20 seconds**: left 20%, right 20%, left 40%, right 40%, continuing through 100%.
These are discrete, equally spaced throttle levels, with a constant hold at each
level. The other side stays neutral during each pulse, and both return to neutral
between calls and at the end. Total commanded pulse time is **200 seconds**, plus neutral
cleanup and communication overhead. Each pulse uses a separate control scope,
so the overall sweep can exceed the no-GPS mode's 60-second per-scope limit.

Edit `range(20, 101, 20)` to change the percentage range/step, or `duration_s = 20`
to change the hold duration. Progress prints the side, percentage and hold time.
Observe the motor response; the script does not automatically determine whether
a thruster is functioning. An error stops the sweep before any further pulses.

The experiment uses `with mc.armed_manual():` to select MANUAL and arm, waiting
for heartbeat confirmation, and request disarming on exit, including if a pulse
or arming confirmation fails. Exceptions propagate; cleanup failures are reported
alongside experiment failures. Normal autopilot pre-arm checks remain
enabled. Running this command therefore requests arming; no prior UI arming is
needed. A latched kill switch must still be reset explicitly in the UI.

```bash
uv run mission-control run experiments/testing/ --mode live --no-gps --ui \
  --connection udpin:0.0.0.0:14550
```

Live calls inherit the client's live mode and retain MANUAL, arming and heartbeat
checks. The SDK sends raw normalized `MANUAL_CONTROL` input on Rover's steering
and throttle axes (`y` and `z`), without RC calibration reads, PWM conversion or
RC overrides. It assumes standard Rover throttle/steering mixing
(`PILOT_STEER_TYPE=0`). Autopilot-side calibration, mixing, ESC settings and motor
limits still affect physical response; the app does not bypass them or change
firmware parameters. Requested neutral is not measured motor thrust.

`--no-gps` disables geographic protection and navigation. Each pulse must be
shorter than 60 seconds, allowing command/cleanup overhead. Without that flag,
GPS and lake clearance checks also apply. Neutral does not actively brake.
