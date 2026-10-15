"""Convert standard MAVLink telemetry to the units used by live vehicle plots."""
import math

def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)



def telemetry_fields(message_type, data):
    values = {}
    mappings = {
        "GLOBAL_POSITION_INT": [("alt", "alt", .001, None), ("hdg", "heading", .01, 65535)],
        "GPS_RAW_INT": [("satellites_visible", "satellites", 1, 255), ("fix_type", "fix", 1, None)],
        "VFR_HUD": [("groundspeed", "speed", 1, None), ("heading", "heading", 1, None)],
        "GPS2_RAW": [("satellites_visible", "satellites", 1, 255), ("fix_type", "fix", 1, None), ("alt", "alt", .001, None)],
        "BATTERY_STATUS": [("battery_remaining", "battery_pct", 1, -1)],
        "SYS_STATUS": [("voltage_battery", "battery_v", .001, 65535),
                       ("battery_remaining", "battery_pct", 1, -1)],
        "MISSION_CURRENT": [("seq", "current_wp", 1, None)],
        "MANUAL_CONTROL": [("x", "manual/throttle", 1, None), ("r", "manual/yaw", 1, None)],
    }
    if message_type == "JOYSTICK":
        mappings["JOYSTICK"] = [(axis, f"joystick/{axis}", 1, None) for axis in ("left_x", "left_y", "right_x", "right_y")]
    for field, metric, scale, missing in mappings.get(message_type, ()):
        value = data.get(field)
        if finite(value) and value != missing:
            values[metric] = value * scale
    if message_type == "BATTERY_STATUS":
        voltages = data.get("voltages", [])
        if voltages and finite(voltages[0]) and voltages[0] != 65535:
            values["battery_v"] = voltages[0] / 1000
    if message_type == "GLOBAL_POSITION_INT" and finite(data.get("vx")) and finite(data.get("vy")):
        values["speed"] = math.hypot(data["vx"], data["vy"]) / 100
    return values
