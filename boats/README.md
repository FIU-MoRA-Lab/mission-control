# Boat integrations

Each boat owns its controller, API implementation, protocol handling, input policy,
SDK, experiments, telemetry fields, recording adapters, and visualization defaults.
There is no shared vehicle-control interface to maintain across boats.

- [`heron/`](heron/README.md): implemented Heron integration.
- [`surveyor/`](surveyor/README.md): SeaRobotics Surveyor integration pending.

One app instance owns one boat and one backend. Backend HTTP ports and viewer
receiver ports are allocated independently. Future simultaneous Heron/Surveyor
connections will use two windows, with no boat switching inside a window. Connection
autodetection and automatic window creation are pending the Surveyor protocol.

Shared map rendering, basemap assets, geographic projection and tile handling live
outside these folders. Each boat owns its missions, locations, safety configuration,
field names, layout defaults and eventually its separately saved layout customizations.
There is no cross-instance map overlay yet.
