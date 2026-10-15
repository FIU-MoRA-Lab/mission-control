# Heron vs. Mission Control

Comparison of the local working trees on October 13, 2026, including uncommitted changes.

## Recommendation

Pursue **Mission Control's Rerun version** as the primary project. The application combines vehicle control, mission planning, sensor visualization, and replay. Rerun supplies synchronized timelines, plots, inspection, recording selection, and saved layouts that would otherwise require custom development.

Choose the **Heron Tauri version** if the primary goal becomes a tightly designed operator interface with extensive styling and custom map interactions. Heron also has more developed packaging and launch setup.

Keep Heron available as a reference while validating Rerun through a complete plan–operate–record–replay workflow and a large sensor recording. If those work comfortably, consolidate on one interface.

## Architecture and capabilities

| Area | Heron | Mission Control |
| --- | --- | --- |
| Interface | SolidJS/TypeScript in a Tauri webview | Native Rust/egui with embedded Rerun |
| Native shell | Tauri manages the window and Python process | eframe/Rerun hosts the viewer and custom controls |
| Maps | MapLibre with deck.gl reading overlays | Rerun geographic view with offline FIU tiles |
| Vehicle control | Python MAVLink controller | Copied Heron controller, reorganized and extended |
| Command boundary | Web UI invokes Tauri; Rust communicates with Python over authenticated HTTP | Rust worker communicates with Python over authenticated HTTP |
| Visualization boundary | Custom UI consumes backend state and recording data | Separate Rerun gRPC connection carries visualization data |
| Replay | Custom MCAP explorer, filtering, and playback | MCAP conversion into Rerun timelines, plots, inspection, and spatial layers |
| Recording | MCAP and JSONL | MCAP, JSONL, and RRD; unfinished files use `.part` names until finalized |
| Backend organization | Fewer, larger modules | Separate controller, bridge, mapping, recording, and Rerun modules |
| Distribution | Tauri bundle scripts and launch bootstrap | Runs from a checkout; no standalone installer yet |
| Terminal interface | Legacy Textual controller retained | Original Textual interface not copied |

Both applications keep MAVLink and manual-control execution in Python. Mission Control does not import or launch the sibling Heron checkout; its controller is an independent copy.

Mission Control currently uses Rerun's **native viewer**, rather than its browser/WebAssembly viewer. Selecting Rerun here means choosing a native Rust presentation stack.

## Application-code size

These are physical source-line counts, including blank lines and comments. They exclude installed dependencies, vendored code, generated files, build outputs, lockfiles, imagery, and logs. Counts cover application source, source helpers, and tests rather than total repository disk size.

| Code | Heron | Mission Control |
| --- | ---: | ---: |
| Python application code | 4,239 | 3,675 |
| Rust application code | 556 | 1,947 |
| Web UI and JavaScript helpers | 946 | 0 |
| Tests | 827 | 1,069 |
| **Total** | **6,568** | **6,691** |

Mission Control is approximately **2% larger** by this measure. The totals include Heron's legacy terminal controller and synthetic-data generator, and Mission Control's experimental custom map. They measure maintained source volume, not equivalent functionality or runtime cost.

Heron's largest Python vehicle module is 835 lines; Mission Control's equivalent is 885 lines. Mission Control improves the surrounding separation of responsibilities, but both still have a substantial central vehicle module.

## Remaining vendored code

Mission Control still contains `frontend/vendor/re_view_map`, a pinned adaptation of Rerun 0.38.1. Its eight Rust files total **1,371 physical lines**, excluded from the application counts above.

`frontend/Cargo.toml` still uses a local Cargo patch for this crate. The fork provides authenticated local FIU tiles and route-editing hooks while retaining Rerun's geographic visualizers and map behavior.

The replacement in `frontend/custom_map/` is opt-in through `MISSION_CONTROL_CUSTOM_MAP=1`. It uses Rerun view/query APIs and the `walkers` map library. The existing fork remains the default.

The prototype's documented gaps include entity picking and selection highlights, annotation-derived colors, persisted blueprint zoom, automatic fit-to-data, and map zoom settings in the selection panel. Large sensor histories still need performance evaluation. See [the prototype evaluation notes](custom-map-prototype.md).

Removing vendored source reduces repository maintenance, but Rerun's dependency graph and version coupling remain. Mission Control pins the Python SDK and Rust viewer to 0.38.1 and uses version-specific extension interfaces.

## Controller and recording divergence

Mission Control has evolved beyond a presentation-only rewrite:

- Vehicle heartbeat expiry pauses manual input and blocks further nonzero sends when the link becomes stale.
- Sent-control diagnostics expose attempted output fields and local transport results.
- Recording failures are isolated from control-packet transmission and telemetry processing.
- Session recording produces MCAP, JSONL, and RRD files, with `.part` names until successful finalization.
- The Rerun observer reads state without renewing the manual-input lease.

These changes are evidence of divergence between the independent controller copies. Future fixes need deliberate synchronization or a shared controller package. Passing tests does not establish real-vehicle behavior or reception of commands.

## Tradeoffs

| Priority | Better fit | Reason |
| --- | --- | --- |
| Telemetry investigation and replay | Mission Control | Rerun supplies synchronized plots, inspection, timelines, and layouts |
| UI styling and custom operator workflows | Heron | SolidJS, CSS, and MapLibre offer direct control over presentation |
| Existing desktop packaging | Heron | Bundle scripts and launch bootstrap are already present |
| Backend responsibility boundaries | Mission Control | Transport, control, recording, mapping, and visualization are separated |
| Reducing custom analysis-feature development | Mission Control | More analysis behavior comes from Rerun |
| Reducing dependence on Rerun extension APIs | Heron | Its interface does not embed the Rerun viewer |

This is an architecture assessment, not a measured performance ranking. Repository size alone does not establish startup speed, memory use, rendering throughput, build time, or usability.

## Verification

Both Python test suites passed during this comparison:

| Repository | Command | Result |
| --- | --- | --- |
| Heron | `.venv/bin/python -m unittest discover -s backend/tests` | 47 tests passed |
| Mission Control | `.venv/bin/python -m unittest discover -s tests` | 49 tests passed |

Initial sandboxed runs were blocked by local socket and cache permissions. The successful runs were repeated outside that sandbox. Desktop frontends were not rebuilt or benchmarked for this comparison, and no real vehicle was used.

## Suggested next steps

1. Validate the Rerun custom map's waypoint editing, replay behavior, and large-history performance before replacing the fork.
2. Make live operation straightforward: keep vehicle state, stop controls, and mission editing easy to access while inspecting recordings.
3. Add desktop packaging and a dependable launch path to Mission Control.
4. Consolidate controller fixes so the two copies do not continue diverging.
5. Complete a plan–operate–record–replay evaluation before retiring the Tauri interface.

## Repository references

- [Mission Control architecture](architecture.md)
- [Custom map prototype](custom-map-prototype.md)
- [Copied controller provenance](../HERON_COPY.md)
- [Mission Control frontend](../frontend/README.md)
- [Heron README](../../heron/README.md)
- [Heron desktop guide](../../heron/desktop/README.md)
