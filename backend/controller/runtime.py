"""Own controller threads, command execution, input expiry, and resource cleanup."""
import json
import queue
import threading
import time
from pathlib import Path
import requests
from backend.input.joystick import JoystickReader
from backend.input.manual_control import ManualControlStreamer
from backend.controller.mission_session import MissionSession
from backend.mapping.lake import LakeSafetyGeometry
from backend.recordings.mcap_reader import load_recording
from backend.bridge.validation import coordinates, number, file_path
from backend.control.service import ControlService

class ControllerRuntime:
    def __init__(self, controller, tiles, *, inputs=True):
        self.controller = controller
        self.session = MissionSession(controller)
        self.tiles = tiles
        self.geometry = LakeSafetyGeometry()
        self.logs = []
        self._commands = threading.Lock()
        self._close_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self._last_ui = time.monotonic()
        self._joystick_positions = dict.fromkeys(("left_x", "left_y", "right_x", "right_y"), 0.0)
        self._joystick_connected = False
        self._last_gamepad = 0.0
        self._manual_active = False
        self._stop = threading.Event()
        self._live_joystick_lock = threading.Lock()
        self._live_joystick = dict(self._joystick_positions, connected=False, error="")
        self._live_joystick_at = 0.0
        self._input_queue = queue.Queue(maxsize=1)
        self.gamepad = JoystickReader(self._input_queue, self._publish_live_joystick) if inputs else None
        self.streamer = ManualControlStreamer(self.session)
        self.worker = threading.Thread(target=self._input_loop, daemon=True)
        self.desktop_location = None
        self.programmatic = ControlService(self)

    def start(self):
        self.controller.start()
        if self.gamepad:
            self.gamepad.start()
        self.streamer.start()
        self.worker.start()
        threading.Thread(target=self._locate_desktop, daemon=True).start()

    def _locate_desktop(self):
        try:
            response = requests.get("https://ipapi.co/json/", timeout=5)
            response.raise_for_status()
            data = response.json()
            lat, lon = coordinates(data)
            self.desktop_location = {"lat": lat, "lon": lon}
        except (requests.RequestException, ValueError, TypeError, KeyError):
            pass

    def _input_loop(self):
        while not self._stop.wait(0.02):
            with self._commands:
                try:
                    self._accept_gamepad_sample(*self._input_queue.get_nowait(), publish_live=False)
                except queue.Empty:
                    pass
                self.expire_inputs()
                self.programmatic.tick()

    def _accept_gamepad_sample(self, throttle, yaw, source, positions=None, raw_axes=None, timestamp_ns=None, *, publish_live=True):
        """Consume only samples from the local HID reader, never UI axis commands."""
        if publish_live:
            self._publish_live_joystick(throttle, yaw, source, positions, raw_axes, timestamp_ns)
        manual = self.session.manual
        if not source.startswith("GAMEPAD"):
            self._last_gamepad = 0.0
            self._joystick_connected = False
            manual.zero(source="NO JOYSTICK")
            self._manual_active = False
            self._log_joystick_sample(source, raw_axes, timestamp_ns)
            return
        self._last_gamepad = time.monotonic()
        self._joystick_connected = True
        if positions is not None:
            self._joystick_positions = dict(positions)
        if self._manual_active and self.controller.mode_str == "MANUAL":
            manual.set_axes(throttle, yaw, source)
        self._log_joystick_sample(source, raw_axes, timestamp_ns)

    def _publish_live_joystick(self, throttle, yaw, source, positions=None, raw_axes=None, timestamp_ns=None):
        # This lock protects only a small latest-value snapshot, never disk or Rerun.
        connected = source.startswith("GAMEPAD")
        with self._live_joystick_lock:
            values = dict(positions or dict.fromkeys(self._joystick_positions, 0.0))
            self._live_joystick = dict(values, connected=connected,
                error=self.gamepad.error if self.gamepad else "")
            self._live_joystick_at = time.monotonic() if connected else 0.0

    def joystick_state(self):
        """Direct HID display state; independent of command and recording locks."""
        with self._live_joystick_lock:
            value = dict(self._live_joystick)
            value["connected"] = value["connected"] and time.monotonic() - self._live_joystick_at <= 0.4
            if self.gamepad:
                value["calibration"] = self.gamepad.calibration.snapshot()
            return value

    def _log_joystick_sample(self, source, raw_axes=None, timestamp_ns=None):
        data = dict(self._joystick_positions, connected=self._joystick_connected,
                    active=self._manual_active and self._joystick_connected, source=source,
                    requested_throttle=self.session.manual.throttle,
                    requested_yaw=self.session.manual.yaw)
        if raw_axes is not None:
            data["raw_axes"] = raw_axes
        try:
            # SessionRecorder only admits to its queue here; all I/O is background.
            self.controller.recorder.log_input(data, timestamp_ns=timestamp_ns)
        except Exception as exc:
            self.controller.recording_error = str(exc)

    def expire_inputs(self, now=None):
        now = time.monotonic() if now is None else now
        m = self.session.manual
        ui_expired = now - self._last_ui > 0.75
        axes_expired = now - self._last_gamepad > 0.4
        if axes_expired and self._joystick_connected:
            self._joystick_connected = False
            self._log_joystick_sample("NO JOYSTICK")
        heartbeat_expired = self.controller._last_vehicle_heartbeat is not None and not self.controller.check_vehicle_link(now)
        if ui_expired or axes_expired or heartbeat_expired or self.controller.mode_str != "MANUAL":
            if m.throttle or m.yaw:
                m.zero()
            if ui_expired or axes_expired or heartbeat_expired:
                self._manual_active = False

    def state(self, *, refresh_ui=True):
        with self._commands:
            if refresh_ui:
                self._last_ui = time.monotonic()
            c = self.controller
            waypoints = [dict(wp) for wp in self.session.waypoints_snapshot()]
            messages = c._log_queue[:]
            del c._log_queue[:len(messages)]
            self.logs = (self.logs + messages)[-100:]
            locked = c.is_armed or c.mission_started or self.programmatic.busy
            editing_locked = locked or c.is_uploading_mission or c.is_starting_mission
            first, last = (waypoints[0], waypoints[-1]) if waypoints else ({}, {})
            snapshot = {
                "connected": c.connected, "armed": c.is_armed, "mode": c.mode_str,
                "vehicle_confirmed": {"armed": c._confirmed_armed, "mode": c._confirmed_mode},
                "lat": c.lat, "lon": c.lon, "alt": c.alt, "heading": c.heading,
                "speed": c.groundspeed, "satellites": c.satellites, "fix": c.fix_type,
                "mission_speed": c.mission_speed,
                "battery_v": c.battery_v, "battery_pct": c.battery_pct,
                "current_wp": c.current_wp_seq, "waypoints": waypoints,
                "uploaded": c.mission_uploaded, "uploading": c.is_uploading_mission,
                "started": c.mission_started, "starting": c.is_starting_mission,
                "simulating": c.simulating_mission,
                "clear_available": not editing_locked,
                "close_available": not editing_locked and len(waypoints) >= 3 and
                    (first["lat"], first["lon"]) != (last["lat"], last["lon"]),
                "padding_enabled": not (locked or c.mission_uploaded or waypoints),
                "padding": self.geometry.safety_padding_m,
                "lake": self.geometry.lake_geojson(),
                "status": c.last_status_msg, "status_error": c.status_is_error,
                "logs": self.logs, "tile_error": self.tiles.last_error,
                "manual": {"throttle": self.session.manual.throttle,
                           "yaw": self.session.manual.yaw,
                           "source": self.session.manual.input_source,
                           "active": self._manual_active,
                           "joystick_connected": time.monotonic() - self._last_gamepad <= 0.4},
                "joystick": self.joystick_state(),
                "control_output": c.last_manual_send,
                "health": {"heartbeat_age": (time.monotonic() - c._last_vehicle_heartbeat if c._last_vehicle_heartbeat is not None else None),
                           "recording_error": c.recording_error or ("; ".join(c.recorder.errors[-3:]) if isinstance(c.recorder.errors, list) else ""), "receive_errors": c.receive_errors,
                           "manual_stream_alive": self.streamer.is_alive()},
                "desktop_location": self.desktop_location,
                "sdk": self.programmatic.state(),
            }
            return self.programmatic.display_state(snapshot)

    def command(self, action, data):
        # File decoding must not block state polling or the manual-input lease.
        if action == "load_mcap":
            return load_recording(file_path(data))
        with self._commands:
            if self._closing:
                raise ValueError("Ground station is shutting down")
            c, s = self.controller, self.session
            if action == "sdk":
                try:
                    return self.programmatic.command(data.get("operation"), data)
                except (KeyError, TypeError) as exc:
                    raise ValueError(f"Invalid SDK request: {exc}") from exc
            if getattr(self,"testing_without_gps",False) and action in {"upload","start"}:
                raise ValueError("Waypoint missions are disabled in no-GPS motor test mode")
            if action in {"stop", "manual", "arm", "disarm", "upload", "start", "calibrate_joystick"} or (action == "manual_active" and data.get("active") is True):
                self.programmatic.takeover("Operator command: " + action)
            if self.programmatic.busy and action in {"clear", "close_loop", "add_waypoint", "move_waypoint", "remove_waypoint", "load", "padding", "mission_speed"}:
                raise ValueError("Release SDK control before changing the route or operating area")
            if (action == "manual" or action == "manual_active" and data.get("active") is True):
                if self.gamepad and self.gamepad.calibration.snapshot()["pending"]:
                    raise ValueError("Wait for joystick calibration to finish before enabling controls")
            if action == "manual_axes":
                raise ValueError("Driving input is joystick-only; UI axis commands are disabled")
            elif action == "calibrate_joystick":
                if not self.gamepad or not self.joystick_state()["connected"]:
                    raise ValueError("Connect a joystick before calibrating")
                self._manual_active = False
                s.manual.zero()
                self.gamepad.calibration.request()
            elif action == "manual_active":
                if not isinstance(data.get("active"), bool):
                    raise ValueError("active must be a boolean")
                self._manual_active = data["active"] and c.mode_str == "MANUAL"
                if not self._manual_active:
                    s.manual.zero()
            elif action == "stop":
                self._manual_active = False
                s.emergency_stop()
            elif action == "arm":
                s.arm()
            elif action == "disarm":
                self._manual_active = False
                s.manual.zero()
                s.disarm()
            elif action == "manual":
                s.set_manual_mode()
                self._manual_active = c.mode_str == "MANUAL"
            elif action in {"upload", "start"}:
                self._manual_active = False
                s.manual.zero()
                if action == "upload":
                    s.upload_mission()
                else:
                    s.start_mission()
            elif action == "mission_speed":
                c.set_mission_speed(number(data.get("meters_per_second"), 0.1, 10.0))
            elif action == "clear":
                if c.is_uploading_mission or c.is_starting_mission:
                    raise ValueError("Wait for mission upload/start to finish before editing")
                s.clear_waypoints()
            elif action == "close_loop":
                if c.is_uploading_mission or c.is_starting_mission:
                    raise ValueError("Wait for mission upload/start to finish before editing")
                c.close_waypoint_loop()
            elif action in {"add_waypoint", "move_waypoint", "remove_waypoint", "load"}:
                if c.is_uploading_mission or c.is_starting_mission:
                    raise ValueError("Wait for mission upload/start to finish before editing")
                if action == "add_waypoint":
                    lat, lon = coordinates(data)
                    c.add_waypoint(lat, lon, number(data.get("alt", 2), -1000, 10000))
                elif action == "load":
                    if not s.load_waypoints(file_path(data)):
                        raise ValueError("Could not load mission file")
                else:
                    index = data.get("index")
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(c.waypoints):
                        raise ValueError("Invalid waypoint index")
                    if action == "move_waypoint":
                        c.update_waypoint_pos(index, *coordinates(data))
                    else:
                        c.remove_waypoint(index)
            elif action == "save":
                if not s.save_waypoints(file_path(data)):
                    raise ValueError("Could not save mission file")
            elif action == "padding":
                if c.is_armed or c.mission_started or c.mission_uploaded or c.waypoints:
                    raise ValueError("Lake padding is locked during missions")
                self.geometry.safety_padding_m = number(data.get("meters"), 0, 10)
            elif action == "export_lake":
                path = Path(file_path(data)).with_suffix(".geojson")
                path.write_text(json.dumps(self.geometry.lake_geojson(), indent=2) + "\n", encoding="utf-8")
                c.set_status_msg(f"Exported lake safety GeoJSON: {path}")
            else:
                raise ValueError("Unknown command")
        return {"ok": True}

    def prepare_quit(self):
        """Pause manual input while the native quit confirmation is visible."""
        with self._commands:
            self._manual_active = False
            self.session.manual.zero()
            return {"armed": self.controller.quit_is_armed}

    def request_shutdown(self, confirmed_armed=False):
        """Recheck arming at the shutdown boundary, after the native dialog."""
        with self._commands:
            with self.controller._mav_send_lock:
                if self.controller.quit_is_armed and not confirmed_armed:
                    return False
                self.controller.running = False
            self._closing = True
            self.programmatic.takeover("Backend shutting down")
            self._manual_active = False
            self.session.manual.zero()
            self.controller.simulating_mission = False
            return True

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.request_shutdown(confirmed_armed=True)
            self._close_resources()

    def _close_resources(self):
        self._stop.set()
        with self._commands:
            self.programmatic.end_recording("interrupted", "Backend shutting down")
        for worker in self.programmatic.auto_workers:
            worker.join(timeout=20)
        for worker in self.programmatic.finalizers:
            worker.join()
        self._manual_active = False
        self.session.manual.zero()
        # Neutral is sent before tearing down the MAVLink transport.
        for _ in range(5):
            self.controller.send_manual_control(0, 0)
            time.sleep(0.02)
        self.streamer.stop()
        if self.gamepad:
            self.gamepad.running = False
        self.controller.running = False
        self.controller.simulating_mission = False
        try:
            self.controller.transport.close()
        except Exception as exc:
            self.controller.log(f"[MAVLink] Transport close failed: {exc}")
        if self.controller.thread.is_alive():
            self.controller.thread.join(timeout=4.5)
        if self.worker.is_alive():
            self.worker.join(timeout=1)
        if self.gamepad and self.gamepad.is_alive():
            self.gamepad.join(timeout=1)
        try:
            self.controller.recorder.close()
        finally:
            self.tiles.close()
