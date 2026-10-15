# Development guide

Native Rerun ground station for the Heron surface vehicle. Rerun provides the
viewer, recording selection, timeline, plots, inspection, and saved blueprints.
Custom Rust/egui panels provide vehicle controls and an offline mission editor.
Python owns MAVLink, mission execution, gamepad input, and the 20 Hz control loop.

The controller is **copied into this repository** under
`backend/controller/`.

Run project commands from the repository root. For setup, see the [installation instructions](../README.md).

## Code organization

The top-level folders follow the two running processes. Source files live
directly inside these folders.

```text
backend/                     Python process: vehicle commands and data production
  launch.py                  Entry point for uv run mission-control
  paths.py                   Shared checkout/resource paths
  controller/                Vehicle actions, mission facade, runtime ownership
    vehicle_control.py       Arm, modes, stop, manual sends, mission upload/execution
    mission_session.py       Mission editing/action facade
    runtime.py               Commands, input leases, startup and cleanup
  input/                     Input acquisition and manual-driving policy
    joystick.py              Live HID reader, independent display callback
    manual_control.py        Manual axes, neutral handling and 20 Hz command stream
  mavlink/                   Connection, capture hooks and vehicle state decoding
    transport.py             Open/send/receive connection; attach capture observer
    network.py               Explicit vehicle network configuration helper
    telemetry.py             Decode heartbeat, position, health, and mission messages
    telemetry_values.py      Convert raw protocol fields to scalar display units
  logger/                    Standalone writers and passive recorder CLI
    records.py               Source/arrival timestamps and explicit clock domains
    session_recorder.py      Background dispatch; drain/flush/finalize on close
    mcap_logger.py           MCAP writer
    jsonl_logger.py          JSON Lines writer
    rrd_logger.py            Headless RRD writer
    mavlink_capture.py       Record RX/TX traffic independently of vehicle actions
    __main__.py              uv run mission-recorder (works alongside QGC)
  bridge/                    Python side of the frontend/backend boundary
    __main__.py              Managed backend startup and parent-death cleanup
    http_server.py           Authenticated HTTP state/command/shutdown endpoints
    validation.py            Incoming command validation
    runtime.py               Connect controller, recording tools, and Rerun SDK
    standalone.py            Controller-only bridge for independent testing
  rerun/                     Python Rerun SDK adapters, not UI widgets
    blueprint.py             Default viewport layout sent to the viewer
    live.py                  Log live controller snapshots into Rerun
    replay.py                Convert MCAP and filtered reading layers to Rerun
  mapping/                   GeoTIFF metadata, geographic projection, tiles, lake geometry
  recordings/                MCAP reading and geographic replay preparation
frontend/                    Rust native app: Rerun viewer and custom egui UI
  main.rs                    Parse arguments and start the viewer/bridge
  viewer.rs                  Create the embedded native Rerun viewer
  app.rs                     Shared UI state, focus/input handling, and lifecycle
  ui/                        Custom panels: vehicle, map controls, recordings, layout
  bridge/                    Rust side: process worker, HTTP client, channel protocol
  vendor/re_view_map/        Pinned Rerun geographic view with local FIU tiles
  Cargo.toml                 Rust build; main.rs is the explicit binary entry point
assets/                      Shared data: imagery, lake geometry, missions, demo MCAP
tests/                       Controller bridge, MCAP logger, and Rerun integration
```

For button/visual changes, start in `frontend/ui/`. For vehicle behavior, start
in `backend/controller/`. For viewer layout or logged data, use
`backend/rerun/` and `frontend/viewer.rs`. For process communication, use the
`bridge/` folders on both sides.

[Architecture and message flow](architecture.md) explains how these pieces
connect. [Backend guide](../backend/README.md) and [frontend guide](../frontend/README.md)
list the entry points and responsibilities.

## Verify

```sh
uv run python -m unittest discover -s tests -v
cargo check --manifest-path frontend/Cargo.toml --locked
cargo fmt --manifest-path frontend/Cargo.toml --check
```

Tests use temporary directories and loopback sockets, never a real vehicle.
Native gamepad hardware, vehicle acknowledgements, and target-platform
packaging still require validation on the relevant machines. This repository
currently runs from a checkout; it does not yet ship a standalone installer.

See [HERON_COPY.md](../HERON_COPY.md) for the copied controller's provenance.
