"""Decode vehicle telemetry independently of control commands and file writers."""
import math
import time
from pymavlink import mavutil

class VehicleTelemetry:
    def __init__(self, controller):
        self.controller = controller

    def run(self):
        controller = self.controller
        controller.log(f"[MAVLink] Connecting to {controller.connection_str}...")
        while controller.running and not controller.connected:
            try:
                controller.master = controller.transport.connect()
                while controller.running:
                    msg = controller.transport.recv_match(blocking=True, timeout=3.0)
                    if not msg:
                        break
                    if msg.get_type() != "HEARTBEAT":
                        continue
                    # UDP may first deliver a companion-computer/GCS heartbeat.
                    # Only identify the autopilot as connected after a vehicle heartbeat.
                    if not controller.master.probably_vehicle_heartbeat(msg):
                        continue
                    controller.autopilot_type = getattr(msg, "autopilot", None)
                    controller.vehicle_type = getattr(msg, "type", None)
                    controller.autopilot_component = msg.get_srcComponent()
                    controller.connected = True
                    controller._last_vehicle_heartbeat = time.monotonic()
                    controller.is_armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                    controller._confirmed_armed = controller.is_armed
                    controller.log(
                        f"[MAVLink] Connected to vehicle System {controller.master.target_system} "
                        f"(heartbeat component {msg.get_srcComponent()}) "
                        f"(autopilot {controller.autopilot_type}, type {controller.vehicle_type})"
                    )
                    break
                if not controller.connected:
                    controller.master.close()
                    controller.master = None
                    time.sleep(1.0)
            except Exception:
                controller.connected = False
                if controller.master:
                    try:
                        controller.master.close()
                    except Exception:
                        pass
                controller.master = None
                time.sleep(1.0)

        try:
            while controller.running and controller.master:
                try:
                    msg = controller.transport.recv_match(blocking=True, timeout=0.5)
                    if not msg:
                        controller.check_vehicle_link()
                        continue


                    msg_type = msg.get_type()

                    if msg_type == 'HEARTBEAT':
                        if not controller.master.probably_vehicle_heartbeat(msg):
                            continue
                        component = msg.get_srcComponent()
                        if component == mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1:
                            if controller.autopilot_component not in (None, component):
                                controller.log(
                                    f"[MAVLink] Using autopilot heartbeat component {component} "
                                    f"instead of {controller.autopilot_component}."
                                )
                            controller.autopilot_component = component
                            controller.autopilot_type = getattr(msg, "autopilot", controller.autopilot_type)
                            controller.vehicle_type = getattr(msg, "type", controller.vehicle_type)
                        elif component != controller.autopilot_component:
                            continue
                        controller._last_vehicle_heartbeat = time.monotonic()
                        controller.connected = True
                        controller.is_armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                        controller._confirmed_armed = controller.is_armed
                        controller.mode_str = mavutil.mode_string_v10(msg)
                        if controller.autopilot_type == mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA:
                            rover_modes = {
                                mode_id: mode_name
                                for mode_id, mode_name in mavutil.mode_mapping_rover.items()
                            }
                            if controller.vehicle_type == 0 or controller.mode_str.startswith("Mode("):
                                controller.mode_str = rover_modes.get(msg.custom_mode, controller.mode_str)
                        controller._confirmed_mode = controller.mode_str

                    elif msg_type in ('GLOBAL_POSITION_INT', 'GPS_RAW_INT', 'GPS2_RAW'):
                        # Header and live map share the autopilot's estimated position.
                        # GPS receiver fixes remain recorded, but never replace it.
                        if msg_type == 'GLOBAL_POSITION_INT':
                            controller.lat = msg.lat / 1e7
                            controller.lon = msg.lon / 1e7
                            controller._last_position_at = time.monotonic()
                            if hasattr(msg, 'vx') and hasattr(msg, 'vy'):
                                controller.groundspeed = math.hypot(msg.vx, msg.vy) / 100
                                controller._last_speed_at = time.monotonic()
                        if hasattr(msg, 'alt'):
                            controller.alt = msg.alt / 1000.0
                        if hasattr(msg, 'hdg') and msg.hdg != 65535:
                            controller.heading = msg.hdg / 100.0
                            controller._last_heading_at = time.monotonic()
                        controller.satellites = getattr(msg, 'satellites_visible', getattr(msg, 'satellites', controller.satellites))
                        controller.fix_type = getattr(msg, 'fix_type', controller.fix_type)

                    elif msg_type == 'ATTITUDE':
                        controller.yaw = msg.yaw
                        controller._last_heading_at = time.monotonic()
                        controller.heading = math.degrees(msg.yaw) % 360.0

                    elif msg_type == 'VFR_HUD':
                        controller.groundspeed = getattr(msg, 'groundspeed', 0.0)
                        controller._last_speed_at = time.monotonic()
                        controller.heading = getattr(msg, 'heading', controller.heading)
                        controller._last_heading_at = time.monotonic()

                    elif msg_type == 'PARAM_VALUE':
                        key = msg.param_id
                        if isinstance(key, bytes):
                            key = key.decode('ascii', errors='replace')
                        controller._control_parameters[key.rstrip('\x00')] = (float(msg.param_value), time.monotonic())

                    elif msg_type == 'BATTERY_STATUS':
                        if hasattr(msg, 'voltages') and len(msg.voltages) > 0 and msg.voltages[0] != 65535:
                            controller.battery_v = msg.voltages[0] / 1000.0
                        controller.battery_pct = getattr(msg, 'battery_remaining', -1)

                    elif msg_type == 'SYS_STATUS':
                        if controller.battery_v == 0.0 and hasattr(msg, 'voltage_battery'):
                            controller.battery_v = msg.voltage_battery / 1000.0
                        if controller.battery_pct == -1 and hasattr(msg, 'battery_remaining'):
                            controller.battery_pct = msg.battery_remaining

                    elif msg_type == 'MISSION_CURRENT':
                        controller._mission_current_seq = getattr(msg, 'seq', 0)
                        controller._mission_current_at = time.monotonic()
                        controller.current_wp_seq = max(0, controller._mission_current_seq - controller._mission_waypoint_offset)

                    elif msg_type == 'STATUSTEXT':
                        text = msg.text
                        if isinstance(text, bytes):
                            text = text.decode("utf-8", errors="replace")
                        text = text.rstrip("\x00")
                        if text:
                            controller.set_status_msg(f"[Vehicle] {text}", error=msg.severity <= 4)

                    elif msg_type in ('MISSION_REQUEST', 'MISSION_REQUEST_INT', 'MISSION_ACK'):
                        controller._mission_req_queue.put(msg)

                except Exception as exc:
                    controller.receive_errors += 1
                    now = time.monotonic()
                    if now - controller._last_receive_error >= 2:
                        controller._last_receive_error = now
                        controller.set_status_msg(f"[MAVLink] Receive error: {exc}", error=True)
        finally:
            # The owning runtime closes the recorder after all producers stop.
            pass
