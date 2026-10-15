# Python backend

This directory is also the Python package named `backend`, with source files
organized directly under it by responsibility.

| Directory | Owns |
| --- | --- |
| `controller/` | Vehicle actions, mission state, runtime ownership and safety leases |
| `input/` | Joystick HID reader and manual-control stream |
| `mavlink/` | Transport, capture attachment, telemetry decoding and conversion |
| `logger/` | Independent MCAP/JSONL/RRD writers and passive recorder CLI |
| `bridge/` | HTTP boundary, validation, managed process startup, controller/Rerun composition |
| `rerun/` | SDK logging adapters and default blueprints; the UI is in `../frontend/` |
| `mapping/` | Geographic projection, offline basemap, imagery, and lake safety geometry |
| `recordings/` | MCAP reading, decoding, and replay preparation |

`launch.py` implements `uv run mission-control`. `python -m backend.bridge` is
the managed child started by the native frontend, not a separate GUI.
`bridge/standalone.py` runs the copied controller bridge without Rerun for
independent lifecycle testing. `paths.py` locates shared files in `../assets/`.

See [architecture](../docs/architecture.md) for command and telemetry flows.
