"""Field catalog combining normalized vehicle telemetry and the installed MAVLink dialect."""
import rerun as rr
from backend.rerun.paths import field_path, geographic_path
from pymavlink.dialects.v20 import ardupilotmega as mavlink

# These paths match RerunTelemetry.state, rather than exposing protocol-scaled values.
TELEMETRY = {
    "speed": ("Ground speed", "m/s", "Motion", "VFR_HUD.groundspeed / GLOBAL_POSITION_INT.vx, vy"),
    "heading": ("Heading", "°", "Motion", "GLOBAL_POSITION_INT.hdg / VFR_HUD.heading / ATTITUDE.yaw"),
    "alt": ("Altitude", "m", "Position", "GLOBAL_POSITION_INT.alt / GPS_RAW_INT.alt / GPS2_RAW.alt"),
    "battery_v": ("Battery voltage", "V", "Power", "BATTERY_STATUS.voltages / SYS_STATUS.voltage_battery"),
    "battery_pct": ("Battery remaining", "%", "Power", "BATTERY_STATUS.battery_remaining / SYS_STATUS.battery_remaining"),
    "satellites": ("Satellites", "", "GPS", "GPS_RAW_INT.satellites_visible / GPS2_RAW.satellites_visible"),
    "fix": ("GPS fix", "", "GPS", "GPS_RAW_INT.fix_type / GPS2_RAW.fix_type"),
    "current_wp": ("Current waypoint", "", "Mission", "MISSION_CURRENT.seq"),
    "manual/throttle": ("Requested throttle", "", "Controls", "Input request → MANUAL_CONTROL.x; RC_CHANNELS_OVERRIDE.chan3_raw"),
    "manual/yaw": ("Requested steering", "", "Controls", "Input request → MANUAL_CONTROL.r; RC_CHANNELS_OVERRIDE.chan1_raw"),
}
for _axis in ("left_x", "left_y", "right_x", "right_y"):
    TELEMETRY[f"joystick/{_axis}"] = (
        f"Joystick {_axis.replace('_', ' ')}", "normalized", "Controls",
        "Local HID stick position before driving deadzone/gain; -1..1, X right / Y up",
    )
DEFAULT_MAVLINK_FIELDS = {
    "RC_CHANNELS": {"chan3_raw", "chan1_raw"},
    "SERVO_OUTPUT_RAW": {"servo1_raw", "servo3_raw"},
}
DEFAULT_FIELDS = ["/telemetry/speed", *[
    field_path("/mavlink/" + message, field)
    for message, fields in DEFAULT_MAVLINK_FIELDS.items() for field in sorted(fields)
]]


def default_fields(catalog):
    return [field["id"] for field in catalog if field.get("default", False)]


MESSAGES = {message.msgname: message for message in mavlink.mavlink_map.values()}


def field_help(message_type, field):
    base = field.split("[")[0]
    if message_type == "GLOBAL_POSITION_INT":
        return "Autopilot estimated position and motion; latitude/longitude drive the live header and map."
    if message_type in {"GPS_RAW_INT", "GPS2_RAW"}:
        return "GPS receiver fix and quality. Compare with the autopilot estimate to diagnose positioning differences."
    if message_type in {"AHRS2", "AHRS3", "HOME_POSITION"}:
        return "Additional estimate or home reference; does not drive the live vehicle position."
    if message_type == "ATTITUDE":
        return "Vehicle orientation: yaw is where the boat points, which can differ from its direction of travel. Angles are radians."
    if message_type == "VFR_HUD":
        return "Autopilot summary readings; speed and heading overlap with other messages and may arrive at different rates."
    if message_type in {"MANUAL_CONTROL", "RC_CHANNELS_OVERRIDE"}:
        return "Control input fields. Sent packets show transport output, not confirmation of vehicle motion."
    if message_type == "HEARTBEAT":
        return "Vehicle presence, operating mode, armed flags and overall state. Mode flags are a bitmask."
    if message_type in {"BATTERY_STATUS", "SYS_STATUS"}:
        return "Battery or subsystem health. Special protocol values may mean unavailable; raw values retain those codes."
    if message_type.startswith("MISSION_"):
        return "Mission progress or upload protocol; sequence numbers identify mission items."
    return f"Original {message_type}.{base} value in protocol units."


def category(message_type, topic):
    if topic.startswith("/commands/"):
        return "03 · Sent controls"
    if message_type in {"GLOBAL_POSITION_INT", "GPS_RAW_INT", "GPS2_RAW", "ATTITUDE", "VFR_HUD", "AHRS2", "AHRS3", "HOME_POSITION"}:
        return "02 · Position and motion diagnostics"
    if message_type in {"HEARTBEAT", "SYS_STATUS", "BATTERY_STATUS", "STATUSTEXT"}:
        return "04 · Vehicle health"
    if message_type.startswith("MISSION_") or message_type in {"MANUAL_CONTROL", "RC_CHANNELS", "RC_CHANNELS_OVERRIDE", "SERVO_OUTPUT_RAW"}:
        return "03 · Controls and mission"
    return "05 · Other raw data" if message_type in MESSAGES else "06 · Custom sensors"


def related_group(message_type, field, fallback):
    if (message_type == "MANUAL_CONTROL" and field == "x") or (message_type in {"RC_CHANNELS", "RC_CHANNELS_OVERRIDE"} and field == "chan3_raw") or (message_type == "VFR_HUD" and field == "throttle"):
        return "01 · Controls · Throttle"
    if (message_type == "MANUAL_CONTROL" and field == "r") or (message_type in {"RC_CHANNELS", "RC_CHANNELS_OVERRIDE"} and field == "chan1_raw"):
        return "02 · Controls · Steering"
    if message_type in {"MANUAL_CONTROL", "RC_CHANNELS", "RC_CHANNELS_OVERRIDE"}:
        return "03 · Controls · Other axes and RC channels"
    if message_type == "SERVO_OUTPUT_RAW":
        if field in {"servo1_raw", "servo3_raw"}:
            return "04 · Controls · Thruster outputs"
        return "12 · Other servo outputs and metadata"
    if field in {"lat", "lon", "lng", "latitude", "longitude"}:
        return "05 · Position · Latitude and longitude"
    if field in {"hdg", "heading", "yaw", "cog"}:
        return "06 · Motion · Heading and course"
    if field in {"groundspeed", "vel", "vx", "vy", "vz", "airspeed"}:
        return "07 · Motion · Speed and velocity"
    if field in {"alt", "altitude", "relative_alt", "climb"}:
        return "08 · Position · Altitude"
    if message_type in {"GPS_RAW_INT", "GPS2_RAW"}:
        return "09 · Position · GPS quality"
    if message_type in {"BATTERY_STATUS", "SYS_STATUS"} and ("battery" in field or field.startswith("voltages") or field in {"temperature", "current_consumed", "energy_consumed", "id"}):
        return "10 · Power · Battery"
    return "11 · " + fallback.split(" · ", 1)[-1]


CONTROL_HELP = {
    "01 · Controls · Throttle": "Requested forward/reverse input → sent MANUAL_CONTROL.x and RC override channel 3 → received RC telemetry. Throttle and steering both affect thruster outputs 1 and 3.",
    "02 · Controls · Steering": "Requested turn input → sent MANUAL_CONTROL.r and RC override channel 1 → received RC telemetry. Steering changes the balance between thruster outputs 1 and 3; RC input channel 1 is not motor output 1.",
    "03 · Controls · Other axes and RC channels": "Separate RC inputs, override fields and message metadata. Channel 2 is not assigned as throttle or steering by this app.",
    "04 · Controls · Thruster outputs": "Heron's active outputs are servo 1 and servo 3. Both respond to throttle and steering. Left/right assignment is unverified. PWM is an autopilot command, not measured motor speed or thrust.",
    "12 · Other servo outputs and metadata": "Remaining servo channels and message metadata. The user reports the other servo outputs remain zero; all recorded fields stay available here.",
}


def field_catalog(topics, *, telemetry=None, units=None, geographic=(), types=None):
    """Catalog observed values and waiting control fields in original protocol units."""
    result = []
    observed = {topic: set(fields) for topic, fields in topics.items()}
    topics = {topic: set(fields) for topic, fields in observed.items()}
    if telemetry is None:
        # Show the control chain before traffic arrives, without inventing samples.
        for topic in ("MANUAL_CONTROL", "/commands/MANUAL_CONTROL"):
            topics.setdefault(topic, set()).update(MESSAGES["MANUAL_CONTROL"].fieldnames)
        for topic in ("RC_CHANNELS_OVERRIDE", "/commands/RC_CHANNELS_OVERRIDE"):
            topics.setdefault(topic, set()).update(MESSAGES[topic.rsplit("/", 1)[-1]].fieldnames)
        for message_type in DEFAULT_MAVLINK_FIELDS:
            if not any(topic.rsplit("/", 1)[-1] == message_type for topic in topics):
                topics[f"/mavlink/{message_type}"] = set(MESSAGES[message_type].fieldnames)
    for metric, (label, unit, group, source) in TELEMETRY.items():
        if telemetry is not None and metric not in telemetry:
            continue
        path = f"/telemetry/{metric}"
        result.append(dict(id=path, path=path, label=label, unit=unit, group=group,
                           source=source, kind="telemetry", data_type="number", default=path in DEFAULT_FIELDS,
                           category="01 · Primary telemetry", tags=["primary", "converted", group.lower()],
                           description=f"{label} in display units. Source: {source}."))
        result[-1]["category"] = {
            "manual/throttle": "01 · Controls · Throttle", "manual/yaw": "02 · Controls · Steering",
            "heading": "06 · Motion · Heading and course", "speed": "07 · Motion · Speed and velocity",
            "alt": "08 · Position · Altitude", "fix": "09 · Position · GPS quality",
            "satellites": "09 · Position · GPS quality", "battery_v": "10 · Power · Battery",
            "battery_pct": "10 · Power · Battery", "current_wp": "11 · Controls and mission",
        }.get(metric, "00 · Controls · Joystick positions")
        result[-1]["stage"] = "input" if metric.startswith("joystick/") else "requested" if metric.startswith("manual/") else "display"
        result[-1]["category_help"] = CONTROL_HELP.get(result[-1]["category"], "")
    units = units or {}
    types = types or {}
    geographic = set(geographic)
    for topic, fields in sorted(topics.items()):
        message_type = topic.rsplit("/", 1)[-1]
        message = MESSAGES.get(message_type)
        for field in sorted(fields):
            path = field_path(topic, field)
            unit = units.get((topic, field), "")
            data_type, enum = "number", None
            base_field = field.split("[")[0]
            if message and base_field in message.fieldnames:
                index = message.fieldnames.index(base_field)
                data_type = message.fieldtypes[index]
                unit = message.fieldunits_by_name.get(base_field, unit)
                enum = message.fieldenums_by_name.get(base_field)
            descriptor = dict(id=path, path=path, label=field.replace("_", " "), unit=unit,
                              group=(f"Sent · {message_type}" if topic.startswith("/commands/") else
                                     f"Raw · {message_type}" if message else f"Sensors · {topic}"),
                              source=f"{topic}.{field}", kind="raw" if message else "sensor",
                              data_type=data_type, default=not topic.startswith(("/commands/", "/input/")) and field in DEFAULT_MAVLINK_FIELDS.get(message_type, set()),
                              category=category(message_type, topic),
                              tags=["sent" if topic.startswith("/commands/") else "raw" if message else "sensor", message_type],
                              description=field_help(message_type, field))
            descriptor["data_type"] = types.get((topic, field), data_type)
            direction = "input" if topic.startswith("/input/") else "sent" if topic.startswith("/commands/") else "received" if message else "sensor"
            descriptor.update(label=f"{message_type}.{field}", direction=direction,
                              available=field in observed.get(topic, set()),
                              category=related_group(message_type, field, descriptor["category"]))
            descriptor["tags"].extend([direction, descriptor["category"].split(" · ")[-1].lower()])
            descriptor["stage"] = direction
            descriptor["category_help"] = CONTROL_HELP.get(descriptor["category"], "")
            if message_type == "RC_CHANNELS" and field in {"chan1_raw", "chan3_raw"}:
                control = "Steering" if field == "chan1_raw" else "Throttle"
                descriptor["display_label"] = f"{control} · {message_type}.{field}"
            if message_type == "SERVO_OUTPUT_RAW" and field in {"servo1_raw", "servo3_raw"}:
                output = 1 if field == "servo1_raw" else 3
                descriptor["display_label"] = f"Thruster output {output} · {message_type}.{field}"
                descriptor["tags"].extend(["thruster", "motor", "throttle", "steering", "pwm"])
                descriptor["description"] = CONTROL_HELP[descriptor["category"]]
            if message_type in {"RC_CHANNELS", "SERVO_OUTPUT_RAW"}:
                descriptor["description"] += " Channel functions depend on ArduPilot RC mapping and SERVOx_FUNCTION settings."
            if direction == "received":
                descriptor["description"] += " Received by this ground station; this does not acknowledge a previously sent command."
            if not descriptor["available"]:
                descriptor["description"] += " Waiting for this message/field; no samples have been observed."
            conversions = {"degE7": "Divide by 10,000,000 for degrees.", "cdeg": "Divide by 100 for degrees.",
                           "mm": "Divide by 1,000 for meters.", "cm/s": "Divide by 100 for meters per second.",
                           "mV": "Divide by 1,000 for volts.", "cA": "Divide by 100 for amps.",
                           "rad": "Multiply by 180/π for degrees."}
            if unit in conversions:
                descriptor["description"] += " " + conversions[unit]
            if enum:
                descriptor["enum"] = enum
            if (topic, field) in geographic:
                descriptor["map_path"] = geographic_path(topic, field)
            result.append(descriptor)
    return result
