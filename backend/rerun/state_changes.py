"""Categorical state lanes rendered by Rerun's native StateTimelineView."""
import rerun as rr
from pymavlink import mavutil

ROOT = "/state_changes"


def state_label(name, value):
    if value is None:
        return ""  # Rerun StateChange's documented reset: end the state and show a gap.
    if isinstance(value, bool):
        labels = {
            "armed": ("DISARMED", "ARMED"),
            "connected": ("OFFLINE", "CONNECTED"),
            "active": ("PAUSED", "ENABLED"),
            "input_active": ("PAUSED", "ENABLED"),
            "joystick_connected": ("DISCONNECTED", "CONNECTED"),
            "manual input enabled": ("PAUSED", "ENABLED"),
            "mission uploaded": ("NOT UPLOADED", "UPLOADED"),
            "mission started": ("INACTIVE", "STARTED"),
            "mission uploading": ("IDLE", "UPLOADING"),
            "mission starting": ("IDLE", "STARTING"),
            "simulation": ("IDLE", "SIMULATING"),
        }
        return labels.get(name, ("FALSE", "TRUE"))[int(value)]
    enum_name = {"status": "MAV_STATE", "mission_state": "MAV_MISSION_STATE"}.get(name)
    entry = mavutil.mavlink.enums.get(enum_name, {}).get(value) if enum_name else None
    if entry:
        return entry.name.removeprefix(enum_name + "_")
    return f"Waypoint {value}" if name == "seq" else str(value)


def record_path(identity, field):
    direction, sys_id, comp_id, topic = identity
    source = "vehicle" if direction == "rx" else "input" if direction == "input" else "sent"
    parts = [source, f"{sys_id}_{comp_id}", topic.rsplit("/", 1)[-1], field]
    return ROOT + "/" + "/".join(rr.escape_entity_path_part(part) for part in parts)


def log_change(stream, path, name, value):
    stream.log(path, rr.StateChange(state=state_label(name, value)))


def log_boolean(stream, path, name, value, previous):
    if path not in previous or previous[path] != value:
        log_change(stream, path, name, value)
        previous[path] = value
