"""Log live controller snapshots into Rerun without renewing manual-input leases."""
import json
import math
import rerun as rr
from backend.rerun.blueprint import blueprint
from backend.rerun.presets import preset_fields
from backend.rerun.schema import field_catalog
from backend.rerun.diagnostics import FIELDS, diagnostic_catalog
from backend.rerun.state_changes import log_change, log_boolean, ROOT as STATE_CHANGE_ROOT

class RerunTelemetry:
    def __init__(self, stream, basemap):
        self.stream = stream
        self.basemap = basemap
        self.previous_geometry = None
        self.previous_route = None
        self.last_status = None
        self.previous_state = {}
        self.boolean_states = {}
        self.track = []
        self.track_block = 0
        self.previous_point = None
        self.previous_snapshots = {}
        self.stream.log("map/metadata", rr.AnyValues(
            crs=basemap["crs"], bounds=basemap["bounds"], center=basemap["center"],
            source="FIU Vivid Standard GeoTIFF + GeoJSON coverage"), static=True)
        catalog = field_catalog({}) + diagnostic_catalog()
        self.stream.send_blueprint(blueprint(
            catalog=catalog, visible_fields=preset_fields(catalog, "controls"), preset="controls"))
        for name, (label, _, _) in FIELDS.items():
            self.stream.log("/diagnostics/" + name, rr.SeriesLines(names=label, interpolation_mode="StepAfter"), static=True)

    def geometry(self, lake):
        for index, feature in enumerate(lake.get("features", [])):
            geom = feature["geometry"]
            role = feature.get("properties", {}).get("role", "lake")
            color = [65, 220, 160] if role == "safe_area" else [255, 154, 96] if "fountain" in role else [80, 210, 230]
            path = f"lake/{role}/{index}"
            coords = geom.get("coordinates", [])
            if geom["type"] == "Polygon":
                rings = [[[p[1], p[0]] for p in ring] for ring in coords]
                self.stream.log(f"geo/{path}", rr.GeoLineStrings(lat_lon=rings, colors=color))
            elif geom["type"] == "Point":
                self.stream.log(f"geo/{path}", rr.GeoPoints(lat_lon=[coords[1], coords[0]], colors=color))

    def state(self, state, elapsed):
        self.stream.reset_time()
        self.stream.set_time("mission", duration=elapsed)
        # Real vehicle metrics are written from their source Record, retaining
        # onboard timestamps. Only simulation generates state-snapshot metrics.
        if state["simulating"]:
            for name in ("speed", "heading", "alt", "battery_v", "battery_pct", "satellites", "fix", "current_wp"):
                value = state[name]
                if math.isfinite(value):
                    self.stream.log(f"telemetry/{name}", rr.Scalars(value))
        for name in ("throttle", "yaw"):
            self.stream.log(f"telemetry/manual/{name}", rr.Scalars(state["manual"][name]))
            axis = "throttle" if name == "throttle" else "steering"
            self.stream.log(f"diagnostics/controls/requested_{axis}", rr.Scalars(1500 + state["manual"][name] * .5))
        for field, name in (("connected", "connected"), ("uploaded", "mission_uploaded"), ("started", "mission_started"), ("uploading", "mission_uploading"), ("starting", "mission_starting"), ("simulating", "simulation")):
            log_boolean(self.stream, f"/diagnostics/local/{name}", name.replace("_", " "), bool(state.get(field, False)), self.boolean_states)
        for name, value in {"mode": state["mode"], "armed": state["armed"], "connected": state["connected"],
                            "manual input enabled": state["manual"]["active"],
                            "mission uploaded": state.get("uploaded", False), "mission started": state.get("started", False),
                            "mission uploading": state.get("uploading", False), "mission starting": state.get("starting", False),
                            "simulation": state["simulating"]}.items():
            if name not in self.previous_state or self.previous_state[name] != value:
                old = self.previous_state.get(name)
                text = f"GCS {name}: {value}" if name not in self.previous_state else f"GCS {name}: {old} → {value}"
                self.stream.log("events/state_changes/" + name.replace(" ", "_"), rr.TextLog(text))
                log_change(self.stream, STATE_CHANGE_ROOT + "/gcs/" + name.replace(" ", "_"), name, value)
                self.previous_state[name] = value
        for name in ("armed", "manual", "auto", "hold"):
            value = state["armed"] if name == "armed" else state["mode"] == name.upper()
            log_boolean(self.stream, f"/diagnostics/local/{name}", name, bool(value), self.boolean_states)
        snapshots = {
            "commands/input": dict(source=state["manual"]["source"], active=state["manual"]["active"],
                                   requested_throttle=state["manual"]["throttle"], requested_yaw=state["manual"]["yaw"]),
            "vehicle/status": dict(mode=state["mode"], armed=state["armed"],
                                   connected=state["connected"], simulation=state["simulating"]),
        }
        for path, values in snapshots.items():
            if self.previous_snapshots.get(path) != values:
                self.stream.log(path, rr.AnyValues(**values))
                self.previous_snapshots[path] = values
        point = (state["lon"], state["lat"])
        if any(point) and all(math.isfinite(v) for v in point):
            if point != self.previous_point:
                self.stream.log("geo/vehicle", rr.GeoPoints(lat_lon=[point[1], point[0]], colors=[255,45,55], radii=rr.Radius.ui_points(7)))
                # Retain completed blocks at their own paths. Only rewrite the
                # current block, with one shared endpoint for a continuous line.
                if len(self.track) == 128:
                    self.track = [self.track[-1]]
                    self.track_block += 1
                self.track.append(point)
                self.previous_point = point
                if len(self.track) > 1:
                    self.stream.log(f"geo/track/{self.track_block}", rr.GeoLineStrings(lat_lon=[[[p[1],p[0]] for p in self.track]], colors=[100,190,250]))
        route = json.dumps(state["waypoints"], sort_keys=True)
        if route != self.previous_route:
            self.stream.log("geo/route", rr.Clear(recursive=True))
            points = [[wp["lat"], wp["lon"]] for wp in state["waypoints"]]
            if points:
                self.stream.log("geo/route/waypoints", rr.GeoPoints(lat_lon=points, colors=[255,215,65]))
                if len(points)>1:
                    self.stream.log("geo/route/path", rr.GeoLineStrings(lat_lon=[points], colors=[255,215,65]))
            self.previous_route = route
        geometry = json.dumps(state["lake"], sort_keys=True)
        if geometry != self.previous_geometry:
            self.stream.log("geo/lake", rr.Clear(recursive=True))
            self.geometry(state["lake"])
            self.previous_geometry = geometry
        if state["status"] and state["status"] != self.last_status:
            self.stream.log("events/status", rr.TextLog(state["status"], level="ERROR" if state["status_error"] else "INFO"))
            self.last_status = state["status"]
            if state["status_error"]:
                self.stream.log("events/errors/status", rr.TextLog(state["status"], level="ERROR"))
