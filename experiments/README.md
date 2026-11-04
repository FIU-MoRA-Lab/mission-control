# Experiments

- [`motion/`](motion/README.md): the ten existing acceleration, straight, turn,
  zigzag and spin cases, plus their shared corridor settings.
- [`testing/`](testing/README.md): lab function checks with brief, independent
  left/right thruster inputs and no GPS or navigation.

Choose a subfolder to run a suite:

```bash
uv run mission-control run experiments/motion/ --mode simulation --ui
uv run mission-control run experiments/testing/ --mode live --no-gps --ui
uv run mission-control run experiments/ --list
```

The runner discovers folders recursively. Running `experiments/` executes both
suites; select `motion/` to run just the original ten cases.

Each bundled experiment uses `with mc.armed_manual():` to select MANUAL, arm,
run, and request disarming on exit or failure. Errors propagate. These SDK calls
wait for heartbeat confirmation in live mode and are no-ops in simulation. Live
runs request arming when started; normal autopilot pre-arm checks remain enabled.
Programmatic control sends raw normalized MAVLink input with no app-side RC
calibration or PWM overrides. Autopilot input processing still applies.

Each experiment saves `recording.mcap`, `recording.jsonl` and `recording.rrd` in
its run directory, including partial recordings on failure. The per-run RRD
includes telemetry, control events and camera frames received during that run
when the camera is streaming. Frames are timestamped by GCS receipt time for
comparison with telemetry; this is not sensor-level clock synchronization.
The batch also saves a shared RRD under `session/` for the whole sequence.
