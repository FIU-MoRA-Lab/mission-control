"""Clock-aware records shared by every writer; no controller or UI dependencies."""
from dataclasses import dataclass
from datetime import datetime, timezone
from array import array
import math
import time

@dataclass(frozen=True)
class Record:
    topic: str
    payload: dict
    receive_timestamp_ns: int
    timestamp_ns: int | None

    @property
    def clock(self):
        return self.payload["clock"]


def iso(ns):
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def normalize(value):
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace").rstrip("\x00")
    if isinstance(value, (list, tuple, array)):
        return [normalize(v) for v in value]
    return value


def numeric_fields(value, prefix=""):
    fields = {}
    if isinstance(value, dict):
        for name, item in value.items():
            fields.update(numeric_fields(item, f"{prefix}.{name}" if prefix else name))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            fields.update(numeric_fields(item, f"{prefix}[{index}]"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        fields[prefix] = value
    return fields


def onboard_time(data):
    # MAVLink time_usec can be Unix time or boot time: the specification says
    # to distinguish by magnitude. Values >= 10^14 us are treated as Unix time.
    # The original field/value is retained so this interpretation is auditable.
    for field, scale, clock in (("time_unix_usec", 1000, "vehicle_unix"),
                                ("time_boot_ms", 1_000_000, "vehicle_boot"),
                                ("time_boot_us", 1000, "vehicle_boot"),
                                ("time_usec", 1000, None)):
        value = data.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            if field == "time_unix_usec" and value == 0:
                continue  # Explicitly unavailable UTC in SYSTEM_TIME.
            return value * scale, clock or ("vehicle_unix" if value >= 100_000_000_000_000 else "vehicle_boot"), field
    return None, "unavailable", None


def mavlink_record(message, *, direction="rx", receive_timestamp_ns=None):
    if direction not in {"rx", "tx"}:
        raise ValueError("Message direction must be rx or tx")
    received = time.time_ns() if receive_timestamp_ns is None else receive_timestamp_ns
    data = message.to_dict()
    data.pop("mavpackettype", None)
    data = normalize(data)
    onboard_ns, onboard_clock, field = onboard_time(data)
    primary, clock = (onboard_ns, onboard_clock) if direction == "rx" else (received, "ground_station")
    name = message.get_type()
    header = getattr(message, "_header", None)
    payload = {
        "timestamp_ns": primary, "timestamp": primary / 1e9 if primary is not None else None,
        "timestamp_iso": iso(primary) if clock in {"vehicle_unix", "ground_station"} else None,
        "clock": clock, "timestamp_field": field if direction == "rx" else None,
        "onboard_timestamp_ns": onboard_ns, "onboard_clock": onboard_clock,
        "receive_timestamp_ns": received, "receive_timestamp_iso": iso(received),
        "msg_type": name,
        "sys_id": header.srcSystem if header else message.get_srcSystem(),
        "comp_id": header.srcComponent if header else message.get_srcComponent(),
        "direction": direction, "data": data,
    }
    return Record(f"/mavlink/{name}" if direction == "rx" else f"/commands/{name}", payload, received, primary)


def input_record(data, *, timestamp_ns=None):
    stamp = time.time_ns() if timestamp_ns is None else timestamp_ns
    payload = {"timestamp_ns": stamp, "timestamp": stamp / 1e9, "timestamp_iso": iso(stamp),
               "clock": "ground_station", "timestamp_field": None,
               "onboard_timestamp_ns": None, "onboard_clock": "unavailable",
               "receive_timestamp_ns": stamp, "receive_timestamp_iso": iso(stamp),
               "msg_type": "JOYSTICK", "sys_id": 0, "comp_id": 0,
               "direction": "input", "data": normalize(data)}
    return Record("/input/JOYSTICK", payload, stamp, stamp)


def field_schema(value):
    if isinstance(value, bool): return {"type": "boolean"}
    if isinstance(value, int): return {"type": "integer"}
    if isinstance(value, float): return {"anyOf": [{"type": "number"}, {"type": "null"}, {"type": "object", "properties": {"$nonfinite": {"enum": ["nan", "+inf", "-inf"]}}, "required": ["$nonfinite"], "additionalProperties": False}]}
    if isinstance(value, str): return {"type": "string"}
    if isinstance(value, list): return {"type": "array", "items": {}}
    return {}


# JSON has no nonfinite numeric literals. Tagged values retain their distinction
# from null while keeping MCAP/JSONL valid JSON.
def json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"$nonfinite": "nan" if math.isnan(value) else "+inf" if value > 0 else "-inf"}
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def restore_nonfinite(value):
    if isinstance(value, dict):
        if set(value) == {"$nonfinite"} and value["$nonfinite"] in ("nan", "+inf", "-inf"):
            return float(value["$nonfinite"])
        return {key: restore_nonfinite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [restore_nonfinite(item) for item in value]
    return value
