# Heron

Run from the repository root:

```sh
uv run mission-control --boat heron
uv run mission-control --boat heron --demo
uv run mission-control --boat heron run boats/heron/experiments/motion/ --mode simulation
uv run mission-control --boat heron run boats/heron/experiments/motion/ --attach --mode simulation
```

`heron` is the default when `--boat` is omitted. This integration is independent of
the sibling Heron repository; Mission Control does not import code from it.

| Directory | Responsibility |
| --- | --- |
| `backend/controller/`, `backend/control/` | Vehicle actions, missions, control and safety policy |
| `backend/mavlink/`, `backend/input/` | Heron protocol, telemetry and input handling |
| `backend/bridge/` | Managed Heron API startup and controller/viewer composition |
| `backend/sdk/`, `backend/batch.py` | Explicit Heron Python API and experiment runner |
| `backend/logger/`, `backend/recordings/`, `backend/analysis/` | Heron recording formats, decoding and analysis |
| `backend/camera.py`, `backend/camera_service.py` | Heron camera integration and deployment helper |
| `visualization/` | Rerun logging, field catalogs, presets, replay and default blueprints |
| `frontend/` | Heron app state, bridge worker, controls, plots and panel layout |
| `experiments/` | Heron experiment scripts and their settings |
| `config/` | Heron locations and safety geometry |
| `paths.py` | Heron resource and generated-data paths |

Python callers import explicitly:

```python
from boats.heron.backend.sdk import MissionControl, Waypoint
```

The native Rust sources compile into the existing `frontend/Cargo.toml` binary
through explicit module paths. Shared map controls and renderers stay in `frontend/`.
Heron's existing map presets and layout-saving behavior are preserved.

Future default session recordings go to `logs/heron/`. Connection discovery, run
indexes, and other generated state are scoped beneath `boats/heron/.mission-control/`.
`MISSION_CONTROL_DATA_DIR` relocates the data root; Heron state is then scoped under
`<root>/boats/heron/`, with session logs in `<root>/logs/heron/`.
Explicit `MISSION_CONTROL_LOG_DIR`, `HERON_LOG_DIR`, and endpoint-file overrides keep
their exact paths. Use distinct overrides when running independent instances.

Existing logs and saved missions have not been migrated. Load old recordings by
path and import existing mission files explicitly. Boat identity metadata and
recording autodetection are deferred; this refactor keeps existing recording schemas.
