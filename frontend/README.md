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
| `../boats/heron/frontend/app.rs` | Heron state, input, emergency-stop shortcut and lifecycle |
| `../boats/heron/frontend/ui/` | Heron telemetry, camera, recordings, plots and window layout |
| `ui/map_controls.rs` | Shared waypoint editor and lake controls |
| `../boats/heron/frontend/bridge/` | Heron protocol, API client and Python child-process worker |

`main.rs` and `ui/mod.rs` include Heron modules through explicit Rust paths.
Map rendering stays shared; additional boat integrations will supply their own
controls, fields and layouts. Each instance owns one boat. Surveyor is pending.

From the repository root:

```sh
cargo check --manifest-path frontend/Cargo.toml --locked
cargo fmt --manifest-path frontend/Cargo.toml --check
uv run mission-control --boat heron --demo
```

`target/` contains generated builds and is ignored. Python SDK data producers
and view blueprints live in `../boats/heron/visualization/`; those are separate from these
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
