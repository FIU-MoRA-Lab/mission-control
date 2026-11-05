# Experiments

- [`motion/`](motion/README.md): the ten existing acceleration, straight, turn,
  zigzag and spin cases, plus their shared corridor settings.
- [`testing/`](testing/README.md): lab function checks with brief, independent
  left/right thruster inputs and no GPS or navigation.

Choose a subfolder to run a suite:

```bash
uv run mission-control --boat heron run boats/heron/experiments/motion/ --mode simulation --ui
uv run mission-control --boat heron run boats/heron/experiments/testing/ --mode live --no-gps --ui
uv run mission-control --boat heron run boats/heron/experiments/ --list
```

With Mission Control already open, run from another terminal using `--attach`:

```bash
uv run mission-control --boat heron run boats/heron/experiments/motion/ --attach --mode simulation
uv run mission-control --boat heron run boats/heron/experiments/motion/ --attach --mode live
```

This reuses the app's backend and leaves the app open when the console command
finishes or is interrupted. Per-case recordings are still saved separately;
the shared session recording belongs to the running app. No-GPS testing requires
that the running backend already has no-GPS testing enabled.

The runner discovers folders recursively. Running `boats/heron/experiments/` executes both
suites; select `motion/` to run just the original ten cases.

With `--ui`, the backend and window stay open after completion, failure, or
Ctrl+C, with SDK control released so you can use the UI or gamepad. Re-arm in
the UI if the experiment disarmed on exit. Close the window to finish the runner.

Motion experiments use `with mc.armed_manual():` to select MANUAL, arm,
run, and request disarming on exit or failure. Errors propagate. These SDK calls
wait for heartbeat confirmation in live mode and are no-ops in simulation. Live
runs request arming when started; normal autopilot pre-arm checks remain enabled.
Programmatic control sends raw normalized MAVLink input with no app-side RC
calibration or PWM overrides. Autopilot input processing still applies.
The lab thruster experiment uses `test_thruster()` instead: a firmware-timed
individual motor test that starts disarmed and disarms on exit, bypassing the
steering/throttle mixer. See its README for native motor-test behavior.

Each experiment saves `recording.mcap`, `recording.jsonl` and `recording.rrd` in
its run directory, including partial recordings on failure. The per-run RRD
includes telemetry, control events and camera frames received during that run
when the camera is streaming. Frames are timestamped by GCS receipt time for
comparison with telemetry; this is not sensor-level clock synchronization.
The batch also saves a shared RRD under `session/` for the whole sequence.
