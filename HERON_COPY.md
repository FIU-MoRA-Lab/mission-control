# Heron code provenance

Copied from `/Users/user/Documents/heron/backend/heron` on October 11, 2026.
The source checkout contained uncommitted work; the copy includes the files as
they existed in that working tree. The sibling Heron repository is not a runtime
dependency and was not modified.

The copied code is organized by responsibility in Mission Control:

| Original Heron module | Mission Control location |
| --- | --- |
| `mission/mission_core.py` | `backend/controller/vehicle_control.py` |
| `mission/mission_logic.py` | `backend/controller/mission_session.py`, `backend/mapping/projection.py` |
| `mission/controls.py` | `backend/input/joystick.py` |
| `mission/backend.py` | `backend/controller/runtime.py`, `backend/bridge/http_server.py`, `validation.py`, `standalone.py` |
| `mission/map_tiles.py` | `backend/mapping/tiles.py` |
| `mission/safety_geometry.py` | `backend/mapping/lake.py` |
| `mission/log_explorer.py` | `backend/recordings/mcap_reader.py` |
| `utils/logger.py` | `backend/logger/` |
| `utils/network_setup.py` | `backend/mavlink/network.py` |
| Lake overlay and sample missions | `assets/fiu_lake.geojson`, `assets/missions/` |

FIU imagery and the synthetic mission MCAP were also copied. Controller/API and
MCAP logger tests are retained in `tests/` with updated imports.

`ControllerRuntime.state(refresh_ui=False)` lets Rerun read telemetry without
renewing the UI/manual-input lease. Existing HTTP state reads retain the default
behavior. Module imports and asset lookups resolve files inside this checkout.
The FIU tile renderer now validates GeoTIFF georeferencing/GeoJSON coverage and
uses exact Web Mercator row latitudes, supporting direct geographic GPS overlays.
These mapping changes do not alter the copied MAVLink/controller behavior.

Mission Control's new Rerun integration lives in `backend/rerun/`, its managed
composition in `backend/bridge/runtime.py`, and its native presentation in
`frontend/`. The original Textual, SolidJS/MapLibre, and Tauri UIs were not copied.
