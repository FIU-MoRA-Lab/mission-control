"""Named chart layouts with explicit units and source paths."""
from backend.rerun.paths import field_path
from backend.rerun.diagnostics import CONTROL_ROOT, STATE_ROOT


def raw(message, field, sent=False):
    return field_path(("/commands/" if sent else "/mavlink/") + message, field)


SERVO = [raw("SERVO_OUTPUT_RAW", "servo1_raw"), raw("SERVO_OUTPUT_RAW", "servo3_raw")]
THROTTLE = [f"{CONTROL_ROOT}/{stage}_throttle" for stage in ("requested", "sent", "received")] + [raw("RC_CHANNELS_OVERRIDE", "chan3_raw", True), raw("RC_CHANNELS_OVERRIDE", "chan3_raw"), raw("RC_CHANNELS", "chan3_raw"), *SERVO]
STEERING = [f"{CONTROL_ROOT}/{stage}_steering" for stage in ("requested", "sent", "received")] + [raw("RC_CHANNELS_OVERRIDE", "chan1_raw", True), raw("RC_CHANNELS_OVERRIDE", "chan1_raw"), raw("RC_CHANNELS", "chan1_raw"), *SERVO]
STATE = [f"{STATE_ROOT}/{name}" for name in ("armed", "manual", "auto", "hold", "input_active", "joystick_connected")]
LOCAL = ["/diagnostics/local/" + name for name in ("armed", "manual", "auto", "hold", "connected", "mission_uploaded", "mission_started", "mission_uploading", "mission_starting", "simulation")]
PRESETS = {
    "fields": ("Individual fields", []),
    "controls": ("Control chain", [("Throttle + servos · PWM µs", THROTTLE), ("Steering + servos · PWM µs", STEERING)]),
    "throttle": ("Throttle + servos", [("Throttle + servos · PWM µs", THROTTLE)]),
    "steering": ("Steering + servos", [("Steering + servos · PWM µs", STEERING)]),
    "states": ("Vehicle & mission state", [("Armed, modes & input · 0/1", STATE), ("GCS mode, mission & link · 0/1", LOCAL), ("Current waypoint", ["/telemetry/current_wp", raw("MISSION_CURRENT", "seq")]), ("Vehicle status · MAV_STATE", [f"{STATE_ROOT}/system_status"]), ("Unhealthy enabled sensors · count", [f"{STATE_ROOT}/unhealthy_sensors"])]),
    "state_changes": ("State changes", []),
    "activity": ("Activity & errors", [("Armed, modes & input · 0/1", STATE)]),
}


def canonical_path(field):
    if field.get("kind") == "raw":
        topic, _, name = field["source"].rpartition(".")
        return raw(topic.rsplit("/", 1)[-1], name, field.get("direction") == "sent")
    return field["path"]


def preset_fields(catalog, preset):
    paths = {path for _, fields in PRESETS[preset][1] for path in fields}
    return [field["id"] for field in catalog if canonical_path(field) in paths or
            (preset in {"states", "activity"} and field.get("data_type") == "bool") or
            (preset == "state_changes" and field.get("data_type") in {"bool", "state"} and field.get("kind") != "diagnostic") or
            (preset in {"states", "activity"} and field.get("data_type") == "state") or
            (preset in {"states", "activity"} and field.get("data_type") == "text")]


def chart_presets(catalog):
    return [dict(id=key, label=label, visible_fields=preset_fields(catalog, key))
            for key, (label, _) in PRESETS.items()]
