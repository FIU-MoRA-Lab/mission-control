# SeaRobotics Surveyor

Integration pending. `uv run mission-control --boat surveyor` reports this status
and exits before launching a window or opening a vehicle connection.

When protocol and sample logs are available, implement Surveyor's backend, SDK,
experiments, configuration, recording adapters, fields and visualization defaults
here. Do not reuse Heron's controller or assume Surveyor uses MAVLink.

Reuse the shared map renderer and basemap infrastructure. Keep Surveyor's missions,
locations, settings and saved layouts separate. Running alongside Heron will use a
separate backend and app window; no shared control interface is required.
