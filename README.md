# Mission Control

Boat-specific code lives in [`boats/`](boats/README.md). Heron is implemented;
SeaRobotics Surveyor integration is pending. Each app instance owns one boat.

```sh
uv run mission-control --boat heron
uv run mission-control --boat heron --demo
```

The map renderer and basemap are shared. Heron owns its controls, fields, layouts,
experiments and configuration. Existing recordings stay in place; new default
session logs go to `logs/heron/`.

## Installation

Run project commands from the repository root. Requires Python 3.13+;
uv installs the project's Python version when needed. The native app also
requires Rust 1.96+ and Cargo (included with Rust).

| Tool / step | Command (macOS / Linux shell) |
| --- | --- |
| Install [uv](https://docs.astral.sh/uv/getting-started/installation/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Install [Rust + Cargo](https://rust-lang.org/tools/install/) — app only | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |
| macOS build tools — app only | `xcode-select --install` |
| Install Python dependencies — app and logger | `uv sync --locked` |
| Build the native app (optional) | `cargo build --manifest-path frontend/Cargo.toml --release --locked` |

Restart your terminal after installing uv/Rust. On Windows, use the linked
installers; Rust may also require Visual Studio C++ Build Tools.
The first app build downloads and compiles Rerun dependencies and can take time.
The launcher uses the release binary when present; otherwise it builds/runs
through Cargo. Rebuild the release binary after changing Rust code.

This repository runs from a checkout; it does not yet ship a standalone installer.

See the [usage guide](docs/usage.md) for running the app and logger, and the
[development guide](docs/development.md) for code organization and verification.

Programmatic control, path execution, map waypoint access, and scoped recordings
are documented in [the Python SDK guide](docs/sdk.md). The SDK uses the existing
backend and UI; simulation is the default and live control is explicit.

Run a folder of Python experiments sequentially with one automatically managed
backend and separate logs per run:

```bash
uv run mission-control --boat heron run boats/heron/experiments/motion/ --mode simulation
uv run mission-control --boat heron run boats/heron/experiments/motion/ --mode simulation --ui
```

If Mission Control is already open, run experiments from another terminal using
its existing backend and vehicle connection:

```bash
uv run mission-control --boat heron run boats/heron/experiments/motion/ --attach --mode simulation
# For an app connected to the vehicle:
uv run mission-control --boat heron run boats/heron/experiments/motion/ --attach --mode live
```

The console command exits when the batch ends; the app stays open. Each case
still gets separate recordings and results. See [the SDK guide](docs/sdk.md)
for selecting cases and connecting to a specific running instance.

Motion cases live in `boats/heron/experiments/motion/`. For independent timed left/right
thruster tests, see [boats/heron/experiments/testing](boats/heron/experiments/testing/README.md).

Each file exports `run(mc)` or an ordered `EXPERIMENTS` mapping. The existing UI is
optional; the batch stops on failure and neutralizes controls between runs.
