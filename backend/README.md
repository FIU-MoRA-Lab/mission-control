# Shared Python infrastructure

| Path | Responsibility |
| --- | --- |
| `launch.py` | `uv run mission-control --boat heron`, explicit `--boat` selection, native build freshness |
| `paths.py` | Checkout root, shared assets, frontend and common file-publication helpers |
| `bridge/http_server.py` | Authenticated loopback HTTP transport; allocated port per backend |
| `bridge/validation.py` | Reusable request validation |
| `mapping/` | Projection, imagery georeferencing, raster tiles and basemap handling |

Heron's controller, MAVLink, SDK, recording tools and managed backend are in
[`../boats/heron/`](../boats/heron/README.md). Heron's Rerun adapters and layouts are
in `boats/heron/visualization/`; its lake constraints live with its own configuration.

```sh
uv run mission-control --boat heron --demo
uv run python -m boats.heron.backend.bridge --demo
uv run python -m boats.heron.backend.bridge.standalone --help
```

The managed bridge is the native frontend's child process, not another GUI.
Surveyor is reserved but not implemented. See [architecture](../docs/architecture.md).
