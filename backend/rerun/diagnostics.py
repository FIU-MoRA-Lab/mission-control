"""Read-only diagnostic projections shared by durable RRDs, live views and MCAP replay."""
import math
import rerun as rr
from pymavlink import mavutil
from backend.logger.records import numeric_fields
from backend.rerun.paths import field_path
from backend.rerun.state_changes import log_change, log_boolean, record_path

CONTROL_ROOT = "/diagnostics/controls"
STATE_ROOT = "/diagnostics/state"
# name: label, units, explanation
FIELDS = {
    "controls/requested_throttle": ("Requested throttle → PWM", "µs", "1500 + requested throttle × 0.5; comparison scale only; SDK sends normalized input, not PWM."),
    "controls/requested_steering": ("Requested steering → PWM", "µs", "1500 + requested steering × 0.5; comparison scale only; SDK sends normalized input, not PWM."),
    "controls/sent_throttle": ("Sent MANUAL_CONTROL.z → PWM", "µs", "1500 + z × 0.5; comparison scale, not vehicle acknowledgment."),
    "controls/sent_steering": ("Sent MANUAL_CONTROL.y → PWM", "µs", "1500 + y × 0.5; comparison scale, not vehicle acknowledgment."),
    "controls/received_throttle": ("Received MANUAL_CONTROL.z → PWM", "µs", "1500 + z × 0.5; received control input."),
    "controls/received_steering": ("Received MANUAL_CONTROL.y → PWM", "µs", "1500 + y × 0.5; received control input."),
    "state/armed": ("Armed", "0/1", "HEARTBEAT MAV_MODE_FLAG_SAFETY_ARMED; autopilot-reported state."),
    "state/manual": ("MANUAL mode", "0/1", "HEARTBEAT custom mode decoded using the vehicle type."),
    "state/auto": ("AUTO mode", "0/1", "HEARTBEAT custom mode decoded using the vehicle type."),
    "state/hold": ("HOLD mode", "0/1", "HEARTBEAT custom mode decoded using the vehicle type."),
    "state/system_status": ("Vehicle system status", "enum", "HEARTBEAT.system_status; MAV_STATE enum."),
    "state/input_active": ("Manual input enabled", "0/1", "Recorded JOYSTICK.active; local input state."),
    "state/joystick_connected": ("Joystick connected", "0/1", "Recorded JOYSTICK.connected; local input state."),
    "state/unhealthy_sensors": ("Unhealthy enabled sensors", "count", "SYS_STATUS present & enabled & ~health bitmask, counted by bit."),
}
for _name in ("armed", "manual", "auto", "hold", "connected", "mission_uploaded", "mission_started", "mission_uploading", "mission_starting", "simulation"):
    FIELDS["local/" + _name] = (_name.replace("_", " ").capitalize(), "0/1", "Ground-station controller state; not a vehicle acknowledgment.")
BOOLEAN_PATHS = {"/diagnostics/" + name for name, (_, unit, _) in FIELDS.items() if unit == "0/1"}


def chart_fields(data):
    """Finite numeric chart samples; booleans use categorical state lanes."""
    return numeric_fields(data)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def control_values(message_type, data, direction):
    if message_type == "JOYSTICK" and direction == "input":
        return {f"{CONTROL_ROOT}/requested_{axis}": 1500 + data[field] * .5
                for field, axis in (("requested_throttle", "throttle"), ("requested_yaw", "steering"))
                if finite(data.get(field)) and -1000 <= data[field] <= 1000}
    if message_type != "MANUAL_CONTROL" or direction not in {"rx", "tx"}:
        return {}
    prefix = "sent" if direction == "tx" else "received"
    # New Rover packets use y/z; keep old x/r recordings readable.
    legacy = not any(data.get(field,0) for field in ("y","z")) and any(data.get(field,0) for field in ("x","r"))
    axes = (("x","throttle"),("r","steering")) if legacy else (("z","throttle"),("y","steering"))
    return {f"{CONTROL_ROOT}/{prefix}_{axis}": 1500 + data[field] * .5
            for field, axis in axes
            if finite(data.get(field)) and -1000 <= data[field] <= 1000}


class RecordDiagnostics:
    def __init__(self):
        self.previous = {}
        self.paths = set()
        self.styled = set()
        self.boolean_fields = set()
        self.schemas = set()
        self.boolean_states = {}

    def _event(self, stream, path, text, level="INFO"):
        stream.log(path, rr.TextLog(text, level=level))

    def _change(self, stream, key, value, label, *, path=None):
        old = self.previous.get(key)
        if key not in self.previous or old != value:
            text = f"{label}: {value}" if key not in self.previous else f"{label}: {old} → {value}"
            self._event(stream, "/events/state_changes/" + rr.escape_entity_path_part(label), text)
            identity, field = key
            log_change(stream, path or record_path(identity, field), field, value)
            self.previous[key] = value

    def _clear(self, stream, path):
        if path in self.paths:
            if path in BOOLEAN_PATHS:
                log_boolean(stream, path, path.rsplit("/", 1)[-1], None, self.boolean_states)
            else:
                stream.log(path, rr.Scalars([]))

    def log(self, stream, *, topic, message_type, data, direction="rx", sys_id=0, comp_id=0, log_scalars=True):
        values = control_values(message_type, data, direction)
        identity = (direction, sys_id, comp_id, topic)
        label = f"{message_type} [{direction} {sys_id}:{comp_id}]"
        for field, value in data.items():
            key = (identity, field)
            if isinstance(value, bool):
                self.boolean_fields.add(key)
            if key in self.boolean_fields:
                self._change(stream, key, value, f"{label}.{field}", path=field_path(topic, field))
        if direction == "input" and message_type == "JOYSTICK":
            for field, name in (("active", "input_active"), ("connected", "joystick_connected")):
                if isinstance(data.get(field), bool):
                    values[f"{STATE_ROOT}/{name}"] = int(data[field])
                else:
                    self._clear(stream, f"{STATE_ROOT}/{name}")
        if direction == "rx":
            # Ground station and invalid-autopilot heartbeats are not vehicle state.
            if message_type == "HEARTBEAT" and isinstance(data.get("type"), int) and isinstance(data.get("autopilot"), int) and data["type"] != 6 and data["autopilot"] != 8:
                base = data.get("base_mode")
                if isinstance(base, int):
                    armed = bool(base & 128)
                    values[f"{STATE_ROOT}/armed"] = int(armed)
                    self._change(stream, (identity, "armed"), "ARMED" if armed else "DISARMED", label)
                elif (identity, "armed") in self.previous:
                    self._change(stream, (identity, "armed"), None, label)
                    self._clear(stream, f"{STATE_ROOT}/armed")
                mode = data.get("custom_mode")
                if isinstance(mode, int) and isinstance(data.get("type"), int) and data.get("autopilot") == 3 and isinstance(base, int) and base & 1:
                    name = (mavutil.mode_mapping_bynumber(data["type"]) or {}).get(mode, f"custom mode {mode}")
                    for target in ("manual", "auto", "hold"):
                        values[f"{STATE_ROOT}/{target}"] = int(name == target.upper())
                    self._change(stream, (identity, "mode"), name, f"{label} mode")
                elif (identity, "mode") in self.previous:
                    self._change(stream, (identity, "mode"), None, f"{label} mode")
                    for target in ("manual", "auto", "hold"):
                        self._clear(stream, f"{STATE_ROOT}/{target}")
                status = data.get("system_status")
                if isinstance(status, int):
                    values[f"{STATE_ROOT}/system_status"] = status
                    self._change(stream, (identity, "status"), status, f"{label} system status")
                elif (identity, "status") in self.previous:
                    self._change(stream, (identity, "status"), None, f"{label} system status")
                    self._clear(stream, f"{STATE_ROOT}/system_status")
            elif message_type == "STATUSTEXT":
                text = data.get("text")
                severity = data.get("severity")
                if isinstance(text, str) and text:
                    level = "ERROR" if isinstance(severity, int) and severity <= 3 else "WARN" if severity == 4 else "INFO"
                    # Preserve long-message chunks explicitly instead of silently joining gaps.
                    chunk = f" [id {data['id']}, chunk {data.get('chunk_seq', 0)}]" if data.get("id") else ""
                    text = f"{label}{chunk}: {text}"
                    self._event(stream, "/events/messages", text, level)
                    if level != "INFO": self._event(stream, "/events/errors/messages", text, level)
            elif message_type in {"COMMAND_ACK", "MISSION_ACK"}:
                result = data.get("result" if message_type == "COMMAND_ACK" else "type")
                text = f"{label}: command {data.get('command', 'mission')} result {result}"
                failed = isinstance(result, int) and result not in ({0, 5} if message_type == "COMMAND_ACK" else {0})
                self._event(stream, "/events/messages", text, "ERROR" if failed else "INFO")
                if failed: self._event(stream, "/events/errors/ack", text, "ERROR")
            elif message_type == "SYS_STATUS":
                masks = [data.get("onboard_control_sensors_" + field) for field in ("present", "enabled", "health")]
                if all(isinstance(mask, int) for mask in masks):
                    failed = masks[0] & masks[1] & ~masks[2]
                    values[f"{STATE_ROOT}/unhealthy_sensors"] = failed.bit_count()
                    key = (identity, "health")
                    if self.previous.get(key) != failed:
                        if failed: self._event(stream, "/events/errors/health", f"{label}: unhealthy enabled sensor mask 0x{failed:x}", "ERROR")
                        elif key in self.previous: self._event(stream, "/events/messages", f"{label}: enabled sensor health recovered")
                        self.previous[key] = failed
            elif message_type in {"MISSION_CURRENT", "MISSION_ITEM_REACHED"}:
                for field in ("seq", "mission_state", "mission_mode"):
                    if field in data: self._change(stream, (identity, field), data[field], f"{label}.{field}")
        for path, value in values.items():
            if path in BOOLEAN_PATHS:
                log_boolean(stream, path, path.rsplit("/", 1)[-1], bool(value), self.boolean_states)
                continue
            if path not in self.styled:
                spec = FIELDS[path.removeprefix("/diagnostics/")]
                stream.log(path, rr.SeriesLines(names=spec[0], interpolation_mode="StepAfter"), static=True)
                self.styled.add(path)
            if log_scalars:
                stream.log(path, rr.Scalars(value))
        self.paths.update(values)
        return values


def diagnostic_catalog(paths=None):
    return [dict(id="/diagnostics/" + name, path="/diagnostics/" + name, label=label,
                 unit="" if unit == "0/1" else unit, group="Diagnostics", category="00 · Diagnostic overlays" if name.startswith("controls/") else "11 · Vehicle state",
                 kind="diagnostic", source="Recorded control or message projection", data_type="bool" if unit == "0/1" else "number", default=False,
                 tags=["preset", "derived"], description=description, available=paths is None or "/diagnostics/" + name in paths)
            for name, (label, unit, description) in FIELDS.items() if paths is None or "/diagnostics/" + name in paths]


def catalog_types(data):
    """Expose boolean and text components without requiring a scalar projection."""
    return {name: "bool" if isinstance(value, bool) else "text"
            for name, value in data.items() if isinstance(value, (bool, str))}
