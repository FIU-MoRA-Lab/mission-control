# Mission Control

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
