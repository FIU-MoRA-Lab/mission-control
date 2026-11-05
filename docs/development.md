# Development guide

Native Rerun ground station for the Heron surface vehicle. Rerun provides the
viewer, recording selection, timeline, plots, inspection, and saved blueprints.
Custom Rust/egui panels provide vehicle controls and an offline mission editor.
Python owns MAVLink, mission execution, gamepad input, and the 20 Hz control loop.

The controller is **copied into this repository** under
`boats/heron/backend/controller/`.

Run project commands from the repository root. For setup, see the [installation instructions](../README.md).

## Code organization

The application separates boat integrations from shared infrastructure:

```text
boats/
  heron/
    backend/                 Controller, MAVLink, input, API composition, SDK, logging
    visualization/           Heron fields, Rerun adapters, replay and default layouts
    frontend/                Heron Rust app state, bridge and custom panels
    experiments/             Heron experiment suites and settings
    config/                  Heron locations and safety geometry
    paths.py                 Heron resources and generated-data scope
  surveyor/                  Integration pending; no assumed protocol or fields
backend/
  launch.py                  Boat selection and native app launch
  paths.py                   Shared resource paths and file-publication helpers
  bridge/                    Reusable HTTP transport and request validation
  mapping/                   Projection, imagery, tiles and basemap utilities
frontend/
  main.rs                    Native window entry point; currently wires Heron modules
  viewer.rs                  Embedded Rerun viewer
  ui/map_controls.rs         Shared map interaction widgets
  custom_map/                Opt-in map renderer
  vendor/re_view_map/         Current geographic renderer and route hooks
  Cargo.toml                 Single Rust binary; boat modules use explicit paths
assets/                      Shared basemap imagery
logs/                        Existing recordings; new default Heron logs in heron/
tests/                       Controller, SDK, recording, map and process verification
```

For vehicle behavior, start in `boats/heron/backend/controller/` or `control/`.
For custom vehicle panels, use `boats/heron/frontend/ui/`. For logged fields and
layouts, use `boats/heron/visualization/`. Shared map code remains in `frontend/`
and `backend/mapping/`. See [boat ownership](../boats/README.md).

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
