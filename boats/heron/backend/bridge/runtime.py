"""Managed loopback API using Mission Control's independent copy of Heron."""
from __future__ import annotations

import sys
import os
import threading
import time
import rerun as rr
import numpy as np
from boats.heron.backend.camera import CameraFeed, placeholder
from boats.heron.backend.controller.runtime import ControllerRuntime
from backend.bridge.validation import file_path, number
from boats.heron.backend.recordings.mcap_reader import load_recording
from boats.heron.visualization.live import RerunTelemetry
from boats.heron.visualization.replay import export_mcap
from boats.heron.visualization.rrd import open_rrd
from pathlib import Path
from boats.heron.paths import recording_path
from boats.heron.visualization.viewer_stream import ViewerStream
from boats.heron.visualization.blueprint import blueprint
from boats.heron.visualization.schema import field_catalog
from boats.heron.visualization.diagnostics import chart_fields, catalog_types, diagnostic_catalog
from boats.heron.visualization.presets import chart_presets, PRESETS
from boats.heron.visualization.viewer_catalog import viewer_catalog



class MissionBackend(ControllerRuntime):
    def __init__(self, controller, tiles, url, *, demo=False, cache=None, inputs=True):
        super().__init__(controller, tiles, inputs=inputs)
        self.demo = demo
        self.rerun_url = url
        self.basemap = tiles.metadata()
        mcap_path = controller.recorder.output_path
        self.live_recording = (Path(mcap_path).with_suffix(".rrd") if isinstance(mcap_path, (str, Path))
                               else recording_path("demo" if demo else "live"))
        stream = ViewerStream(self.live_recording, url, disk_writer=controller.recorder.writers.get("rrd"))
        self.telemetry = RerunTelemetry(stream, self.basemap)
        self._telemetry_lock = threading.RLock()
        self._replay_lock = threading.Lock()
        self._sensor_fields = {}
        self._field_types = {}
        self._catalog_signature = None
        self._cached_catalog = []
        self._replay_plots = None
        self._viewer_catalogs = {}
        self._telemetry_closed = False
        self.camera = CameraFeed(self._camera_frame, self._camera_status)
        self._recording = None
        self._recording_path = None
        self._telemetry_worker = threading.Thread(target=self._log_loop, name="rerun-telemetry", daemon=True)
        controller.recorder.subscribe(self._observe_record)

    def _observe_record(self, record):
        """Observe recorded traffic without wrapping controller methods or writers."""
        with self._telemetry_lock:
            if self._telemetry_closed: return
            types = catalog_types(record.payload["data"])
            self._field_types.update({(record.topic, field): dtype for field, dtype in types.items()})
            self._sensor_fields.setdefault(record.topic, set()).update(set(chart_fields(record.payload["data"])) | set(types))
            self.telemetry.stream.record(record)

    def start(self):
        self._started_at = time.monotonic()
        self._started_wall_at = time.time()
        self.controller.recorder.open()
        if self.demo:
            # Read joystick input locally without a vehicle transport or network setup.
            self.controller.lat, self.controller.lon = self.geometry._lake_outline[0]
            self.controller.mode_str = "DEMO"
            if self.gamepad:
                self.gamepad.start()
            self.streamer.start()
            self.worker.start()
        else:
            super().start()
        self._telemetry_worker.start()
        self._camera_status("Camera off")
        if not self.demo and os.environ.get("MISSION_CONTROL_CAMERA_ENABLED", "1") != "0":
            self.camera.configure(enabled=True)

    def _camera_frame(self, contents, received_ns, *, record_run=True):
        # Camera times describe receipt on the GCS, never onboard acquisition.
        with self._telemetry_lock:
            if self._telemetry_closed:
                return
            stream = self.telemetry.stream
            stream.reset_time()
            stream.set_time("mission", duration=max(0, time.monotonic() - self._started_at))
            stream.set_time("receive_time", timestamp=np.datetime64(received_ns, "ns"))
            stream.log("camera/image", rr.EncodedImage(contents=contents, media_type="image/jpeg"))
            programmatic = getattr(self,"programmatic",None)
            recording = programmatic.recording if programmatic is not None else None
            if recording is not None and record_run:
                recording.camera_frame(contents,received_ns)

    def _camera_status(self, message):
        # Keep UI placeholders out of per-run perception imagery.
        self._camera_frame(placeholder(message), time.time_ns(), record_run=False)
        with self._telemetry_lock:
            if not self._telemetry_closed:
                self.telemetry.stream.log("events/camera", rr.TextLog(message))

    def _log_loop(self):
        while not self._stop.wait(.1):
            try:
                # Recording needs controller values, not the UI schema/catalog.
                snapshot = super().state(refresh_ui=False)
                with self._telemetry_lock:
                    self.telemetry.state(snapshot, time.monotonic()-self._started_at)
            except Exception as exc:
                print(f"Rerun telemetry: {exc}", file=sys.stderr)

    def state(self, *, refresh_ui=True):
        with self._telemetry_lock:
            topics = {topic: sorted(fields) for topic, fields in self._sensor_fields.items()}
            signature = (tuple((topic, tuple(fields)) for topic, fields in sorted(topics.items())), tuple(sorted(self._field_types.items())))
            if signature != self._catalog_signature:
                self._cached_catalog = field_catalog(topics, types=self._field_types) + diagnostic_catalog()
                self._catalog_signature = signature
            catalog = self._cached_catalog
        return super().state(refresh_ui=refresh_ui) | {"viewer_attached": self.rerun_url is not None, "demo": self.demo, "camera": self.camera.state(), "live_recording": str(self.live_recording), "live_recording_id": self.telemetry.stream.disk.get_recording_id(), "recording_active": str(self.telemetry.stream.active_path), "session_directory": str(self.live_recording.parent), "sensor_topics": topics, "field_catalog": catalog, "chart_presets": chart_presets(catalog)}

    def command(self, action, data):
        if action == "attach_viewer":
            from urllib.parse import urlparse
            url = data.get("url", "")
            parsed = urlparse(url)
            if parsed.scheme != "rerun+http" or parsed.hostname != "127.0.0.1" or not parsed.port:
                raise ValueError("The viewer must use a loopback Rerun receiver")
            with self._telemetry_lock:
                self.telemetry.stream.attach(url)
                self.rerun_url = url
                self.telemetry = RerunTelemetry(self.telemetry.stream, self.basemap)
            return {"basemap": self.basemap, "demo": self.demo}
        if action == "detach_viewer":
            with self._telemetry_lock:
                self.telemetry.stream.detach()
                self.rerun_url = None
            return {"ok": True}
        if action == "configure_camera":
            return self.camera.configure(enabled=data.get("enabled"), source=data.get("source"))
        if action == "recording_catalog":
            info = viewer_catalog(data)
            with self._replay_lock:
                self._viewer_catalogs[(info["application_id"], info["recording_id"])] = info
            return info
        if action == "configure_plots":
            source = data.get("source", "live")
            if source not in {"live", "replay"}:
                raise ValueError("Choose live or replay plots")
            with self._replay_lock:
                info = self._replay_plots if source == "replay" else None
                if "recording_id" in data or "application_id" in data:
                    target = (data.get("application_id"), data.get("recording_id"))
                    if not all(isinstance(value, str) and value for value in target):
                        raise ValueError("Choose a viewer recording")
                    if info is None or target != (info["application_id"], info["recording_id"]):
                        info = self._viewer_catalogs.get(target)
                        if info is None:
                            raise ValueError("The recording catalog is not ready")
                if source == "replay" and info is None:
                    raise ValueError("Load a recording first")
                with self._telemetry_lock:
                    catalog = info["sensor_topics"] if info else {topic: sorted(fields) for topic, fields in self._sensor_fields.items()}
                schema = info["field_catalog"] if info else field_catalog(catalog, types=self._field_types) + diagnostic_catalog()
                selected = data.get("visible_fields")
                available = {field["id"] for field in schema}
                if not isinstance(selected, list) or any(not isinstance(field, str) or field not in available for field in selected):
                    raise ValueError("Choose catalog fields to show")
                preset = data.get("preset", "fields")
                if not isinstance(preset, str) or preset not in PRESETS:
                    raise ValueError("Choose a chart preset")
                layout = blueprint(catalog=schema, visible_fields=selected, preset=preset)
                if info:
                    stream = rr.RecordingStream(info["application_id"], recording_id=info["recording_id"])
                    try:
                        stream.connect_grpc(self.rerun_url)
                        stream.send_blueprint(layout)
                        stream.flush(timeout_sec=5)
                    finally:
                        stream.disconnect()
                else:
                    with self._telemetry_lock:
                        self.telemetry.stream.send_blueprint(layout)
            return {"ok": True}
        if action == "mode":
            mode = data.get("mode")
            if getattr(self,"testing_without_gps",False) and mode == "AUTO":
                raise ValueError("AUTO mode is disabled in no-GPS motor test mode")
            if mode not in {"MANUAL", "HOLD", "AUTO"}:
                raise ValueError("Unsupported mode")
            with self._commands:
                if self._closing:
                    raise ValueError("Ground station is shutting down")
                self.programmatic.takeover("Operator mode change")
                self._manual_active = False
                self.session.manual.zero()
                if not self.controller.set_mode(mode):
                    raise ValueError(f"Could not select {mode}")
            return {"ok": True}
        if action in {"load_mcap", "load_recording"}:
            self.prepare_quit()  # Pause teleop before opening a replay.
            if not self._replay_lock.acquire(blocking=False):
                raise ValueError("A recording is already loading")
            try:
                path = file_path(data)
                if Path(path).suffix.lower() == ".rrd":
                    result = open_rrd(path, url=self.rerun_url)
                    self._recording, self._recording_path = None, None
                    self._replay_plots = result
                    self._viewer_catalogs[(result["application_id"], result["recording_id"])] = result
                    return result
                if Path(path).suffix.lower() != ".mcap":
                    raise ValueError("Choose an MCAP or RRD recording")
                recording = load_recording(path)
                result = export_mcap(path, recording_path("replay"), self.basemap,
                                     self.geometry.lake_geojson(), url=self.rerun_url, recording=recording)
                self._recording, self._recording_path = recording, path
                self._replay_plots = result
                self._viewer_catalogs[(result["application_id"], result["recording_id"])] = result
                return result
            finally:
                self._replay_lock.release()
        if action == "filter_readings":
            self.prepare_quit()
            if not self._replay_lock.acquire(blocking=False):
                raise ValueError("A recording is already loading")
            try:
                if self._recording is None:
                    raise ValueError("Load a recording first")
                topic, field = data.get("topic"), data.get("field")
                dataset = next((d for d in self._recording["datasets"] if d["topic"] == topic), None)
                metadata = next((f for f in dataset["fields"] if f["name"] == field), None) if dataset else None
                if metadata is None:
                    raise ValueError("Choose an available recording field")
                lower = number(data.get("minimum"), -1e30, 1e30)
                upper = number(data.get("maximum"), -1e30, 1e30)
                if lower > upper:
                    raise ValueError("Minimum must not exceed maximum")
                radius = number(data.get("radius", 3), 1, 10)
                opacity = number(data.get("opacity", 1), .1, 1)
                track_visible = data.get("track_visible", True)
                if not isinstance(track_visible, bool):
                    raise ValueError("Track visibility must be a boolean")
                points = [dict(p, values={field:p["values"][field]}) for p in dataset["points"]
                          if field in p["values"] and lower <= p["values"][field] <= upper]
                selected = dict(dataset, points=points, fields=[metadata], count=len(points))
                filtered = dict(self._recording, datasets=[selected])
                result = export_mcap(self._recording_path, recording_path("filtered"), self.basemap,
                                     self.geometry.lake_geojson(), url=self.rerun_url, recording=filtered,
                                     include_raw=False, radius=radius, opacity=opacity, track_visible=track_visible)
                self._replay_plots = result
                self._viewer_catalogs[(result["application_id"], result["recording_id"])] = result
                result["matched"] = len(points)
                return result
            finally:
                self._replay_lock.release()
        return super().command(action, data)

    def close(self):
        self.camera.close()
        self._stop.set()
        if self._telemetry_worker.is_alive():
            self._telemetry_worker.join(timeout=2)
        try:
            super().close()
        finally:
            self._stop.set()
            if self._telemetry_worker.is_alive():
                self._telemetry_worker.join(timeout=2)
            with self._telemetry_lock:
                if not self._telemetry_closed:
                    self._telemetry_closed = True
                    try:
                        self.telemetry.stream.flush(timeout_sec=2)
                    except RuntimeError as exc:
                        if "gRPC connection severed" not in str(exc):
                            raise
                    finally:
                        self.telemetry.stream.disconnect()
