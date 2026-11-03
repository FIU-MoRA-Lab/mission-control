# Rust frontend

The native window embeds Rerun's viewer alongside custom egui panels. Rust
source lives directly here; `Cargo.toml` points at `main.rs`, so no additional
`src/` directory is needed.

| File / directory | Owns |
| --- | --- |
| `main.rs` | Arguments, loopback Rerun receiver, bridge startup, and native window |
| `viewer.rs` | Rerun renderer/viewer setup |
| `custom_map/` | Opt-in custom FIU view using public Rerun APIs and standard walkers |
| `vendor/re_view_map/` | Geographic FIU view and hooks for map route tools |
| `app.rs` | Shared state, events, focus handling and emergency-stop shortcut, and shutdown lifecycle |
| `ui/vehicle.rs` | Vehicle telemetry and command widgets |
| `ui/map_controls.rs` | Offline waypoint editor and lake controls |
| `ui/recordings.rs` | MCAP/RRD loading and shared layer controls |
| `ui/layout.rs` | Window panels, status, tabs, and confirmation dialogs |
| `bridge/protocol.rs` | Rust channel messages and latest-axis snapshot |
| `bridge/http.rs` | Authenticated Python API client |
| `bridge/worker.rs` | Python child process, polling, command delivery, and input expiry |

From the repository root:

```sh
cargo check --manifest-path frontend/Cargo.toml --locked
cargo fmt --manifest-path frontend/Cargo.toml --check
uv run mission-control --demo
```

`target/` contains generated builds and is ignored. Python SDK data producers
and view blueprints live in `../backend/rerun/`; those are separate from these
custom widgets. See [architecture](../docs/architecture.md) for the bridge.

Development builds use light optimization and 16 codegen units, with stronger
optimization and one codegen unit for the viewer, spatial view, and application, to keep the
Rerun binary's unwind tables within Apple's compact-unwind offset limit. Panic
unwinding and linker diagnostics remain enabled. Changing these settings
requires a one-time dependency rebuild.

Incremental compilation is disabled to reduce development cache storage. For
regular use, build the release app and remove development artifacts:

```sh
cargo build --manifest-path frontend/Cargo.toml --release --locked
cargo clean --manifest-path frontend/Cargo.toml --profile dev
```

The launcher uses the release binary when present. Rebuild it after Rust source
changes. Development commands recreate `target/debug/` as needed.

See [custom map prototype](../docs/custom-map-prototype.md) for the opt-in run commands and comparison criteria.
