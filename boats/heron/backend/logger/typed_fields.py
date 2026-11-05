"""Complete decoded fields, alongside the finite scalar chart projection."""
import math
import pyarrow as pa
import rerun as rr
from pymavlink.dialects.v20 import ardupilotmega as mavlink
from boats.heron.visualization.paths import field_path

MESSAGES = {message.msgname: message for message in mavlink.mavlink_map.values()}
TYPES = {"char": pa.string(), "float": pa.float32(), "double": pa.float64()}
# Local control telemetry has no MAVLink field schema. Physical quantities must
# not switch Arrow types when telemetry reports an integer-valued sample.
CONTROL_FLOAT_FIELDS = {"lat", "lon", "heading", "speed", "requested_throttle",
                        "throttle", "steering", "left", "right", "target_speed_mps",
                        "target_heading_deg"}
for width in (8, 16, 32, 64):
    TYPES[f"int{width}_t"] = getattr(pa, f"int{width}")()
    TYPES[f"uint{width}_t"] = getattr(pa, f"uint{width}")()


def log_decoded_fields(stream, topic, message_type, data, *, schemas=None):
    message = MESSAGES.get(message_type)
    for name, value in data.items():
        path = field_path(topic, name)
        protocol_type = None
        arrow_type = None
        if message_type == "CONTROL" and name in CONTROL_FLOAT_FIELDS:
            arrow_type = pa.float64()
        if message and name in message.fieldnames:
            protocol_type = message.fieldtypes[message.fieldnames.index(name)]
            arrow_type = TYPES[protocol_type]
            length = message.array_lengths[message.ordered_fieldnames.index(name)]
            if length and protocol_type != "char":
                arrow_type = pa.list_(arrow_type, length)
        if arrow_type is None and isinstance(value, int) and not isinstance(value, bool):
            # Zero/positive samples must use the same type as later negative
            # joystick values. Preserve uint64 only when int64 cannot hold it.
            arrow_type = pa.int64() if value <= 2**63 - 1 else pa.uint64()
        state = "null" if value is None else "nan" if isinstance(value, float) and math.isnan(value) else "+inf" if value == float("inf") else "-inf" if value == -float("inf") else "present"
        # An explicit state preserves null even for a custom field whose type is
        # unknown on its first sample. Consumers must honor state, not forward-fill.
        components = {"state": pa.array([state], type=pa.string())}
        if arrow_type is not None or value is not None:
            components["value"] = pa.array([value], type=arrow_type)
        stream.log(path, rr.AnyValues(**components))
        if message and protocol_type and (schemas is None or path not in schemas):
            stream.log(path + "/schema", rr.AnyValues(
                protocol_type=pa.array([protocol_type], type=pa.string()),
                unit=pa.array([message.fieldunits_by_name.get(name, "")], type=pa.string()),
                enum=pa.array([message.fieldenums_by_name.get(name, "")], type=pa.string()),
                array_length=pa.array([length], type=pa.int64())), static=True)
            if schemas is not None: schemas.add(path)
