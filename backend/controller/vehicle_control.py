"""Vehicle actions and mission control; transport/capture live under backend.mavlink."""
import csv
import json
import math
import queue
import threading
import time
from pathlib import Path
from pymavlink import mavutil
from backend.mavlink.transport import MavlinkTransport
from backend.paths import ASSETS

SAMPLE_MISSION_FILE = ASSETS / "missions" / "sample.json"
SAVED_MISSION_FILE = Path.cwd() / "mission_saved.geojson"


class VehicleController:
    """Vehicle actions and mission state; transport owns connection, capture and decoding."""

    def __init__(self, connection_str: str = "udpin:0.0.0.0:14550", *, recorder=None):
        self.connection_str = connection_str
        self.connected = False
        self.running = True
        self._last_vehicle_heartbeat = None
        self.recording_error = ""
        self.receive_errors = 0
        self._last_receive_error = 0.0
        self.last_manual_send = None
        self.autopilot_type = None
        self.vehicle_type = None
        self.autopilot_component = None

        # Telemetry State
        self.is_armed = False
        self._confirmed_armed = False
        self._confirmed_mode = None
        self._last_position_at = 0.0
        self._last_heading_at = 0.0
        self._last_speed_at = 0.0
        self._control_parameters = {}
        self._mission_current_seq = None
        self._mission_current_at = 0.0
        self.mode_str = "DISCONNECTED"
        self.lat = 0.0
        self.lon = 0.0
        self.alt = 0.0
        self.heading = 0.0        # deg (0-360)
        self.yaw = 0.0            # rad
        self.groundspeed = 0.0    # m/s
        self.satellites = 0
        self.fix_type = 0         # 0=No GPS, 3=3D Fix, 4=DGPS, etc.
        self.battery_v = 0.0      # Volts
        self.battery_pct = -1     # %
        self.current_wp_seq = 0   # Current active waypoint index executing

        # Active Waypoints List
        self.waypoints: list[dict] = []
        self.mission_speed = 1.0  # Target ground speed in m/s, included in the mission.
        self._mission_waypoint_offset = 0
        self._wp_lock = threading.Lock()
        self._log_queue: list[str] = []

        # Thread-safe MAVLink mission queue & upload state
        self._mission_req_queue = queue.Queue()
        self.is_uploading_mission = False
        self.mission_uploaded = False
        self.is_starting_mission = False
        self.mission_started = False
        self.simulating_mission = False
        self._sim_thread = None

        # Status notification feedback for UI
        self.last_status_msg = ""
        self.status_is_error = False
        self.status_timestamp = 0.0

        self.transport = MavlinkTransport(connection_str, recorder=recorder, record=True)
        self._mav_send_lock = self.transport.send_lock
        self.transport.bind_controller(self)
        self.thread = self.transport.thread

    @property
    def master(self):
        return self.transport.master

    @master.setter
    def master(self, master):
        self.transport.master = master

    @property
    def recorder(self):
        return self.transport.recorder

    def start(self):
        self.transport.start()

    @property
    def quit_is_armed(self):
        # A sent DISARM command isn't proof that the motors have disarmed.
        # Retain the last vehicle heartbeat until it confirms disarming.
        return self.is_armed or self._confirmed_armed

    def log(self, text: str):
        print(text)
        self._log_queue.append(text)

    def set_status_msg(self, text: str, error: bool = False):
        self.log(text)
        self.last_status_msg = text
        self.status_is_error = error
        self.status_timestamp = time.time()

    # -----------------------------------------------------------------------
    # MAVLink Actions & Async Controls
    # -----------------------------------------------------------------------

    def emergency_stop(self):
        """Immediately aborts autonomous mission, sets MANUAL mode, and zeroes thrusters."""
        self.simulating_mission = False
        self.set_status_msg("[SAFETY] EMERGENCY STOP requested; sending neutral controls.", error=True)
        if not self.master or not self.connected:
            self.mode_str = "MANUAL"
            self.set_status_msg("[SAFETY] No active vehicle link; neutral controls could not be sent.", error=True)
            return

        # Keep MAVLink writes and the short neutral burst off the GUI thread.
        def stop_worker():
            mode_set = self.set_mode("MANUAL")
            for _ in range(5):
                self.send_manual_control(0, 0)
                time.sleep(0.02)
            if not mode_set:
                self.set_status_msg(
                    "[SAFETY] Neutral controls sent, but MANUAL mode could not be selected.",
                    error=True,
                )
        threading.Thread(target=stop_worker, name="heron-emergency-stop", daemon=True).start()

    def check_vehicle_link(self, now=None):
        now = time.monotonic() if now is None else now
        if self.connected and self._last_vehicle_heartbeat is not None and now - self._last_vehicle_heartbeat > 3:
            self.connected = False
            self.set_status_msg("[MAVLink] Vehicle heartbeat expired; manual input paused.", error=True)
        return self.connected

    def send_manual_control(self, throttle: int, yaw: int):
        """Send manual control command (throttle: -1000..1000, yaw: -1000..1000)."""
        throttle = max(-1000, min(1000, int(throttle)))
        yaw = max(-1000, min(1000, int(yaw)))
        self.check_vehicle_link()
        roll_input, throttle_input = yaw, throttle
        if self._control_parameters.get("PILOT_STEER_TYPE", (0,))[0] == 1:
            # Keep the existing joystick's throttle/steering meaning when the
            # vehicle is configured for native left/right paddle inputs.
            left, right = throttle + yaw, throttle - yaw
            scale = max(1.0, abs(left) / 1000, abs(right) / 1000)
            roll_input, throttle_input = round(left / scale), round(right / scale)
        pwm_steer = int(1500 + roll_input * 0.5)
        pwm_throttle = int(1500 + throttle_input * 0.5)
        output = {"time": time.monotonic(), "throttle": throttle, "yaw": yaw,
                                 "chan1_raw": pwm_steer, "chan3_raw": pwm_throttle,
                                 "sent": False, "error": "Vehicle link unavailable"}
        if self.master and self.connected:
            master = self.master
            sys_id = master.target_system if getattr(master, 'target_system', 0) > 0 else 1
            try:
                with self._mav_send_lock:
                    if not self.running and (throttle or yaw):
                        output["error"] = "Ground station is shutting down"
                        self.last_manual_send = output
                        return False
                    self.transport.send("manual_control", sys_id, throttle_input, 0, 0, roll_input, 0)
                    # Use the same target component selected by the legacy TUI
                    # after its heartbeat. Some vehicle bridges do not route
                    # component-0 RC overrides to the autopilot.
                    comp_id = master.target_component
                    self.transport.send("rc_channels_override", 
                        sys_id, comp_id, pwm_steer, 0, pwm_throttle, 0, 0, 0, 0, 0
                    )
                output.update(sent=True, error="")
                self.last_manual_send = output
                return True
            except Exception as exc:
                output["error"] = str(exc)
                self.last_status_msg = f"[MAVLink] Manual control send failed: {exc}"
                self.status_is_error = True
        self.last_manual_send = output
        return False

    def set_mode(self, mode_name: str) -> bool:
        """Switch vehicle flight mode (e.g. MANUAL, AUTO, GUIDED, HOLD)."""
        if not self.running:
            return False
        mode_name = mode_name.upper()
        if not self.master or not self.connected:
            self.mode_str = mode_name
            self.log(f"[GCS] Set simulated mode to {mode_name}")
            return True

        try:
            mode_mapping = self.master.mode_mapping()
        except Exception as exc:
            mode_mapping = None
            self.set_status_msg(f"[MAVLink] Could not read vehicle mode mapping: {exc}", error=True)
        if (not mode_mapping
                and self.autopilot_type == mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA):
            # Heron is an ArduRover surface vehicle. Pymavlink has no table
            # for some generic/legacy HEARTBEAT vehicle types, so use Rover's
            # standard mode table when the autopilot is known to be ArduPilot.
            mode_mapping = {
                mode_name: mode_id
                for mode_id, mode_name in mavutil.mode_mapping_rover.items()
            }
            self.log(
                f"[MAVLink] Using ArduRover mode table for unsupported vehicle type "
                f"{self.vehicle_type}."
            )
        if not mode_mapping:
            self.set_status_msg(
                f"[MAVLink] Cannot select {mode_name}: vehicle mode mapping is unavailable.",
                error=True,
            )
            return False
        if mode_name not in mode_mapping:
            self.set_status_msg(f"[MAVLink] Unknown mode: {mode_name}", error=True)
            return False

        mode_id = mode_mapping[mode_name]
        sys_id = self.master.target_system if getattr(self.master, 'target_system', 0) > 0 else 1
        try:
            with self._mav_send_lock:
                if not self.running:
                    return False
                self.transport.send("set_mode", 
                    sys_id,
                    mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    mode_id
                )
        except Exception as exc:
            self.set_status_msg(f"[MAVLink] Could not send {mode_name} mode command: {exc}", error=True)
            return False
        self.log(f"[MAVLink] Mode set command sent: {mode_name}")
        self.mode_str = mode_name
        return True

    def set_arm(self, arm: bool) -> bool:
        """Arm or Disarm vehicle motors."""
        if not self.running:
            return False
        if not self.master or not self.connected:
            with self._mav_send_lock:
                if not self.running:
                    return False
                self.is_armed = arm
            self.set_status_msg(f"[GCS] Vehicle {'ARMED' if arm else 'DISARMED'} (Simulated)", error=False)
            return True

        sys_id = self.master.target_system if getattr(self.master, 'target_system', 0) > 0 else 1
        # The existing terminal controller broadcasts commands with component 0.
        comp_id = 0
        cmd = mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
        param1 = 1.0 if arm else 0.0
        param2 = 21196.0 if arm else 0.0
        try:
            with self._mav_send_lock:
                if not self.running:
                    return False
                self.transport.send("command_long", 
                    sys_id, comp_id, cmd, 0, param1, param2, 0, 0, 0, 0, 0
                )
                self.is_armed = arm
        except Exception as exc:
            self.set_status_msg(f"[MAVLink] Could not send {'ARM' if arm else 'DISARM'} command: {exc}", error=True)
            return False
        self.log(f"[MAVLink] {'ARM' if arm else 'DISARM'} command sent.")
        return True

    def upload_mission_async(self):
        """Spawns non-blocking thread to upload mission so UI never freezes."""
        if self.is_uploading_mission:
            self.set_status_msg("[MAVLink] Mission upload already in progress...", error=True)
            return

        self.is_uploading_mission = True
        t = threading.Thread(target=self._upload_mission_worker, daemon=True)
        t.start()

    def set_mission_speed(self, speed: float):
        if self.is_armed or self.mission_started or self.is_uploading_mission or self.is_starting_mission:
            raise ValueError("Mission speed is locked while armed or uploading/starting a mission")
        speed = self.validate_mission_speed(speed)
        with self._wp_lock:
            if speed != self.mission_speed:
                self.mission_speed = speed
                self.mission_uploaded = False

    @staticmethod
    def validate_mission_speed(speed):
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0.1 <= speed <= 10.0:
            raise ValueError("Mission speed must be between 0.1 and 10.0 m/s")
        return float(speed)

    def start_mission_async(self):
        """Upload the current plan if needed, then start it or simulate offline."""
        if self.is_starting_mission:
            self.set_status_msg("[MAVLink] Mission start already in progress...", error=True)
            return
        self.is_starting_mission = True
        threading.Thread(target=self._start_mission_worker, daemon=True).start()

    def _start_mission_worker(self):
        self.is_starting_mission = True
        try:
            if not self.running:
                return
            with self._wp_lock:
                wps = list(self.waypoints)
            if not wps:
                self.set_status_msg("[MAVLink] No waypoints to start.", error=True)
                return
            if not self.mission_uploaded:
                if self.is_uploading_mission:
                    self.set_status_msg("[MAVLink] Mission upload is still in progress.", error=True)
                    return
                if not self.upload_mission():
                    return
            if not self.running:
                return
            if not self.master or not self.connected:
                self.set_status_msg("[SIM] Starting simulated waypoint mission.", error=False)
                self.start_simulated_mission(wps)
                return
            # Start the uploaded plan at its speed command, regardless of the
            # autopilot's previous mission/resume index. Item zero is home.
            sys_id = self.master.target_system or 1
            comp_id = self.autopilot_component or self.master.target_component or 1
            selected_at = time.monotonic()
            self.transport.send("mission_set_current", sys_id, comp_id, 1)
            if not self.set_mode("AUTO"):
                self.set_status_msg("[MAVLink] Mission is uploaded, but AUTO mode could not be selected.", error=True)
                return
            if not self.set_arm(True):
                self.set_status_msg("[MAVLink] AUTO selected, but vehicle could not be armed.", error=True)
                return
            if not self._wait_for_mission_start(selected_at):
                self.set_status_msg(
                    "[MAVLink] Mission start unconfirmed: waiting for vehicle AUTO, armed state, "
                    "and active waypoint telemetry. Check vehicle warnings and GPS/EKF status.", error=True)
                return
            self.mission_started = True
            self.set_status_msg("[MAVLink] Mission started: vehicle confirmed AUTO, armed, and an active waypoint.", error=False)
        except Exception as exc:
            self.set_status_msg(f"[MAVLink] Mission start failed: {exc}", error=True)
        finally:
            self.is_starting_mission = False

    def _wait_for_mission_start(self, selected_at, timeout=8.0):
        deadline = time.monotonic() + timeout
        while self.running and self.check_vehicle_link():
            if (self._confirmed_mode == "AUTO" and self._confirmed_armed
                    and self._mission_current_at >= selected_at
                    and self._mission_current_seq is not None and self._mission_current_seq >= 2):
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        return False

    def _upload_mission_worker(self):
        self.is_uploading_mission = True
        try:
            self.upload_mission()
        finally:
            self.is_uploading_mission = False

    def upload_mission(self) -> bool:
        self.mission_uploaded = False
        with self._wp_lock:
            wps = list(self.waypoints)
            speed = self.mission_speed

        if not wps:
            self.set_status_msg("[MAVLink] No waypoints to upload! Add waypoints on map first.", error=True)
            return False

        # Fallback to simulated autonomous mission if not connected to live vehicle
        if not self.master or not self.connected:
            self.mission_uploaded = True
            self.set_status_msg(
                f"[SIM] {len(wps)} waypoints staged. Press START MISSION to simulate navigation.",
                error=False,
            )
            return True

        self.set_status_msg(f"[MAVLink] Uploading {len(wps)} waypoints to ArduRover FCU...", error=False)

        try:
            # Clear old queued mission requests
            while not self._mission_req_queue.empty():
                try:
                    self._mission_req_queue.get_nowait()
                except queue.Empty:
                    break

            sys_id = self.master.target_system if getattr(self.master, 'target_system', 0) > 0 else 1
            comp_id = self.master.target_component if getattr(self.master, 'target_component', 0) > 0 else 1

            # Step 1: Clear existing mission items on vehicle
            with self._mav_send_lock:
                self.transport.send("mission_clear_all", sys_id, comp_id)
            time.sleep(0.15)

            total_count = len(wps) + 2  # Home, speed command, then waypoints.

            # Step 2: Send MISSION_COUNT with retry loop (up to 4 attempts)
            req_msg = None
            for attempt in range(1, 5):
                if not self.running:
                    return False
                self.log(f"[MAVLink] Sending MISSION_COUNT={total_count} (Attempt {attempt}/4)...")
                with self._mav_send_lock:
                    self.transport.send("mission_count", sys_id, comp_id, total_count)

                try:
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline:
                        req_msg = self._mission_req_queue.get(timeout=max(0.001, deadline - time.monotonic()))
                        # Clearing the old mission can produce its own accepted ACK.
                        if req_msg.get_type() == 'MISSION_ACK' and getattr(req_msg, 'type', 0) == 0:
                            req_msg = None
                            continue
                        break
                    if req_msg is not None:
                        break
                except queue.Empty:
                    continue

            if not req_msg:
                self.set_status_msg("[MAVLink] Upload failed: Vehicle ignored MISSION_COUNT (Timeout)", error=True)
                return False

            # Step 3: Handle sequence item requests & MISSION_ACK
            items_sent = set()
            start_time = time.time()

            while self.running and time.time() - start_time < 15.0:
                if req_msg is None:
                    try:
                        req_msg = self._mission_req_queue.get(timeout=3.0)
                    except queue.Empty:
                        self.set_status_msg("[MAVLink] Upload failed: Timed out waiting for item request", error=True)
                        return False

                msg = req_msg
                req_msg = None
                msg_type = msg.get_type()

                if msg_type == 'MISSION_ACK':
                    ack_type = getattr(msg, 'type', 0)
                    if ack_type == 0 and len(items_sent) == total_count:
                        self.mission_uploaded = True
                        self._mission_waypoint_offset = 1
                        self.set_status_msg(
                            f"[MAVLink] Mission uploaded ({len(wps)} waypoints, {speed:.1f} m/s). Press START MISSION to begin.",
                            error=False,
                        )
                        return True
                    else:
                        reason = ("Vehicle acknowledged an incomplete upload" if ack_type == 0
                                  else f"Vehicle rejected mission (ACK type: {ack_type})")
                        self.set_status_msg(f"[MAVLink] Upload failed: {reason}", error=True)
                        return False

                elif msg_type in ('MISSION_REQUEST', 'MISSION_REQUEST_INT'):
                    seq = getattr(msg, 'seq', 0)
                    item_message = "mission_item_int" if msg_type == 'MISSION_REQUEST_INT' else "mission_item"
                    coordinate_scale = 1e7 if msg_type == 'MISSION_REQUEST_INT' else 1.0
                    coordinate = lambda value: int(value * coordinate_scale) if coordinate_scale == 1e7 else float(value)
                    if seq == 0:
                        lat0 = coordinate(self.lat if self.lat != 0 else wps[0]['lat'])
                        lon0 = coordinate(self.lon if self.lon != 0 else wps[0]['lon'])
                        with self._mav_send_lock:
                            self.transport.send(item_message,
                                sys_id, comp_id,
                                0,
                                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                                mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                                0, 1, 0, 0, 0, 0,
                                lat0, lon0, 0.0
                            )
                        items_sent.add(0)
                        self.log(f"[MAVLink] Sent Home Item (seq 0): ({lat0/coordinate_scale:.6f}, {lon0/coordinate_scale:.6f})")
                    elif seq == 1:
                        with self._mav_send_lock:
                            self.transport.send(item_message,
                                sys_id, comp_id, seq,
                                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                                mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
                                0, 1, 1, speed, -1, 0, 0, 0, 0)
                        items_sent.add(seq)
                        self.log(f"[MAVLink] Sent mission speed: {speed:.1f} m/s")
                    elif 2 <= seq <= len(wps) + 1:
                        wp = wps[seq - 2]
                        with self._mav_send_lock:
                            self.transport.send(item_message,
                                sys_id, comp_id,
                                seq,
                                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                                mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                                0, 1, 0, 0, 0, 0,
                                coordinate(wp['lat']),
                                coordinate(wp['lon']),
                                float(wp.get('alt', 2.0))
                            )
                        items_sent.add(seq)
                        self.log(f"[MAVLink] Sent WP {seq - 1}/{len(wps)}: ({wp['lat']:.6f}, {wp['lon']:.6f})")

            if not self.running:
                return False
            self.set_status_msg("[MAVLink] Upload failed: No final mission acknowledgment", error=True)
            return False

        except Exception as e:
            self.set_status_msg(f"[MAVLink] Mission upload error: {e}", error=True)

        return False

    def start_simulated_mission(self, wps: list[dict]):
        """Spawns background simulation loop to step vehicle through waypoints when disconnected."""
        with self._mav_send_lock:
            if not self.running:
                return
            self.simulating_mission = True
            self.mission_started = True
            self.is_armed = True
            self.mode_str = "AUTO"
            self.current_wp_seq = 1

        if self.lat == 0.0 or self.lon == 0.0:
            self.lat = wps[0]['lat'] - 0.0001
            self.lon = wps[0]['lon'] - 0.0001

        self._sim_thread = threading.Thread(target=self._simulated_mission_loop, args=(list(wps),), daemon=True)
        self._sim_thread.start()

    def _simulated_mission_loop(self, wps: list[dict]):
        for idx, wp in enumerate(wps):
            if not self.simulating_mission or not self.running:
                break

            self.current_wp_seq = idx + 1
            target_lat, target_lon = wp['lat'], wp['lon']

            while self.simulating_mission and self.running:
                d_lat = target_lat - self.lat
                d_lon = target_lon - self.lon
                north = d_lat * 111_320.0
                east = d_lon * 111_320.0 * math.cos(math.radians(self.lat))
                distance = math.hypot(north, east)

                # Arrived at waypoint (within ~3 meters)
                if distance < 3.0:
                    self.log(f"[SIM] Reached Waypoint {idx + 1}/{len(wps)}")
                    time.sleep(0.5)
                    break

                bearing = math.degrees(math.atan2(east, north)) % 360.0
                self.heading = bearing
                self.groundspeed = self.mission_speed

                fraction = min(self.mission_speed * 0.08 / distance, 1.0)
                self.lat += d_lat * fraction
                self.lon += d_lon * fraction

                time.sleep(0.08)

        if self.simulating_mission:
            self.groundspeed = 0.0
            self.mode_str = "HOLD"
            self.simulating_mission = False
            self.set_status_msg("[SIM] Simulated Mission COMPLETED! Mode set to HOLD.", error=False)

    # -----------------------------------------------------------------------
    # Waypoint List Management & File I/O
    # -----------------------------------------------------------------------

    def add_waypoint(self, lat: float, lon: float, alt: float = 2.0):
        with self._wp_lock:
            self.mission_uploaded = False
            wp_id = len(self.waypoints) + 1
            self.waypoints.append({"id": wp_id, "lat": round(lat, 7), "lon": round(lon, 7), "alt": round(alt, 1)})
        self.log(f"[WAYPOINT] Added WP {wp_id}: ({lat:.6f}, {lon:.6f})")

    def remove_waypoint(self, index: int):
        with self._wp_lock:
            if 0 <= index < len(self.waypoints):
                self.mission_uploaded = False
                self.waypoints.pop(index)
                for idx, wp in enumerate(self.waypoints):
                    wp["id"] = idx + 1
        self.log(f"[WAYPOINT] Removed WP {index + 1}")

    def clear_waypoints(self):
        if self.is_armed or self.mission_started:
            self.set_status_msg(
                "[SAFETY] Clear Route is available only while disarmed and before mission start.",
                error=True,
            )
            return
        with self._wp_lock:
            self.mission_uploaded = False
            self.waypoints.clear()
        self.log("[WAYPOINT] Cleared all waypoints.")

    def close_waypoint_loop(self):
        """Append the starting waypoint to explicitly close a traced boundary."""
        if self.is_armed or self.mission_started:
            self.set_status_msg(
                "[SAFETY] Close Loop is available only while disarmed and before mission start.",
                error=True,
            )
            return False
        with self._wp_lock:
            if len(self.waypoints) < 3:
                return False
            first, last = self.waypoints[0], self.waypoints[-1]
            if first["lat"] == last["lat"] and first["lon"] == last["lon"]:
                return False
            self.mission_uploaded = False
            closed = dict(first)
            closed["id"] = len(self.waypoints) + 1
            self.waypoints.append(closed)
        self.log("[WAYPOINT] Closed route back to its starting point.")
        return True

    def update_waypoint_pos(self, index: int, lat: float, lon: float):
        with self._wp_lock:
            if 0 <= index < len(self.waypoints):
                self.mission_uploaded = False
                self.waypoints[index]["lat"] = round(lat, 7)
                self.waypoints[index]["lon"] = round(lon, 7)

    def load_waypoints_file(self, filepath: str) -> bool:
        path = Path(filepath)
        if not path.exists():
            self.log(f"[Waypoints] File not found: {filepath}")
            return False

        try:
            new_wps = []
            new_speed = self.mission_speed
            if path.suffix.lower() in ('.json', '.geojson'):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and "mission_speed_m_s" in data:
                    new_speed = self.validate_mission_speed(data["mission_speed_m_s"])
                if isinstance(data, dict) and data.get("type") in {
                    "FeatureCollection", "Feature", "LineString", "MultiLineString",
                    "Polygon", "MultiPolygon", "Point", "MultiPoint", "GeometryCollection",
                }:
                    new_wps = self._waypoints_from_geojson(data)
                else:
                    items = data.get("waypoints", data) if isinstance(data, dict) else data
                    for idx, item in enumerate(items):
                        new_wps.append({
                            "id": idx + 1,
                            "lat": float(item["lat"]),
                            "lon": float(item["lon"]),
                            "alt": float(item.get("alt", 2.0))
                        })
            elif path.suffix.lower() == '.csv':
                with open(path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for idx, row in enumerate(reader):
                        new_wps.append({
                            "id": idx + 1,
                            "lat": float(row["lat"] if "lat" in row else row["latitude"]),
                            "lon": float(row["lon"] if "lon" in row else row["longitude"]),
                            "alt": float(row.get("alt", row.get("altitude", 2.0)))
                        })

            if new_wps:
                for idx, waypoint in enumerate(new_wps):
                    if (not all(math.isfinite(waypoint[key]) for key in ("lat", "lon", "alt"))
                            or not -90 <= waypoint["lat"] <= 90
                            or not -180 <= waypoint["lon"] <= 180):
                        raise ValueError("Mission contains invalid waypoint coordinates")
                    waypoint["id"] = idx + 1
                with self._wp_lock:
                    self.mission_uploaded = False
                    self.waypoints = new_wps
                    self.mission_speed = new_speed
                self.log(f"[Waypoints] Loaded {len(new_wps)} waypoints from {filepath}")
                return True

        except Exception as e:
            self.log(f"[Waypoints] Error loading file: {e}")

        return False

    @staticmethod
    def _waypoints_from_geojson(data):
        """Read route lines, area rings, or point collections as mission waypoints."""
        if data.get("type") == "FeatureCollection":
            features = data.get("features", [])
        elif data.get("type") == "Feature":
            features = [data]
        else:
            features = [{"type": "Feature", "properties": {}, "geometry": data}]

        path_features = []
        point_features = []
        for feature in features:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties") or {}
            role = str(properties.get("role", "")).lower()
            if role in {"fountain", "obstacle", "exclusion"}:
                continue
            geometry = feature.get("geometry") or {}
            geometry_type = geometry.get("type")
            if geometry_type in {"LineString", "MultiLineString", "Polygon", "MultiPolygon"}:
                path_features.append((geometry, properties))
            elif geometry_type in {"Point", "MultiPoint"}:
                point_features.append((geometry, properties))
            elif geometry_type == "GeometryCollection":
                for item in geometry.get("geometries", []):
                    if item.get("type") in {"LineString", "Polygon"}:
                        path_features.append((item, properties))
                    elif item.get("type") == "Point":
                        point_features.append((item, properties))

        selected = path_features[:1] if path_features else point_features
        coordinates = []
        properties = {}
        if selected:
            geometry, properties = selected[0]
            geometry_type = geometry["type"]
            values = geometry.get("coordinates", [])
            if geometry_type == "LineString":
                coordinates = values
            elif geometry_type == "MultiLineString":
                coordinates = [coord for line in values for coord in line]
            elif geometry_type == "Polygon":
                coordinates = values[0] if values else []
            elif geometry_type == "MultiPolygon":
                coordinates = values[0][0] if values and values[0] else []
            elif geometry_type == "Point":
                coordinates = [values]
            elif geometry_type == "MultiPoint":
                coordinates = values
        for geometry, feature_properties in selected[1:]:
            if geometry["type"] == "Point":
                coordinates.append(geometry["coordinates"])
            elif geometry["type"] == "MultiPoint":
                coordinates.extend(geometry["coordinates"])
        altitudes = properties.get("altitudes", [])
        waypoints = []
        for idx, coordinate in enumerate(coordinates):
            if len(coordinate) < 2:
                continue
            alt = coordinate[2] if len(coordinate) > 2 else (
                altitudes[idx] if idx < len(altitudes) else properties.get("alt", 2.0)
            )
            waypoints.append({
                "id": idx + 1,
                "lat": float(coordinate[1]),
                "lon": float(coordinate[0]),
                "alt": float(alt),
            })
        return waypoints

    def save_waypoints_file(self, filepath: str) -> bool:
        path = Path(filepath)
        if path.suffix.lower() != ".geojson":
            path = path.with_suffix(".geojson")
        try:
            with self._wp_lock:
                wps = list(self.waypoints)
                speed = self.mission_speed

            coordinates = [
                [float(waypoint["lon"]), float(waypoint["lat"]), float(waypoint.get("alt", 2.0))]
                for waypoint in wps
            ]
            if len(coordinates) == 1:
                geometry = {"type": "Point", "coordinates": coordinates[0]}
            elif len(coordinates) >= 4 and coordinates[0][:2] == coordinates[-1][:2]:
                geometry = {"type": "Polygon", "coordinates": [coordinates]}
            elif coordinates:
                geometry = {"type": "LineString", "coordinates": coordinates}
            else:
                geometry = None
            data = {
                "type": "FeatureCollection",
                "mission_speed_m_s": speed,
                "features": ([{
                    "type": "Feature",
                    "properties": {"name": "Mission route", "role": "route"},
                    "geometry": geometry,
                }] if geometry else []),
            }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)

            self.log(f"[Waypoints] Saved {len(wps)} waypoints to {path}")
            return True
        except Exception as e:
            self.log(f"[Waypoints] Save error: {e}")
            return False


# ---------------------------------------------------------------------------
