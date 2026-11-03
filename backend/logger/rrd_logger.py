"""Headless RRD writer; recording never requires a viewer, map or controller."""
from pathlib import Path
from datetime import timedelta
import rerun as rr
import pyarrow as pa
import numpy as np
from backend.paths import partial_path, publish_file
from backend.mavlink.telemetry_values import telemetry_fields
from backend.rerun.paths import field_path, topic_path
from .typed_fields import log_decoded_fields
from backend.rerun.diagnostics import RecordDiagnostics, chart_fields


def log_record(stream, record, *, started_ns, diagnostics=None):
    # Reset inherited clocks so an input record never acquires a vehicle timeline.
    stream.reset_time()
    stream.set_time("mission", duration=np.timedelta64(record.receive_timestamp_ns - started_ns, "ns"))
    stream.set_time("receive_time", timestamp=np.datetime64(record.receive_timestamp_ns, "ns"))
    if record.timestamp_ns is not None:
        if record.clock == "vehicle_boot":
            source = f"vehicle_boot_{record.payload['sys_id']}_{record.payload['comp_id']}"
            stream.set_time(source, duration=np.timedelta64(record.timestamp_ns, "ns"))
        elif record.clock == "vehicle_unix":
            stream.set_time("vehicle_time", timestamp=np.datetime64(record.timestamp_ns, "ns"))
        else:
            stream.set_time("ground_station_time", timestamp=np.datetime64(record.timestamp_ns, "ns"))
    log_decoded_fields(stream, record.topic, record.payload["msg_type"], record.payload["data"],
                       schemas=diagnostics.schemas if diagnostics is not None else None)
    for field, value in chart_fields(record.payload["data"]).items():
        path = field_path(record.topic, field)
        stream.log(path, rr.Scalars(value))
    metadata = {}
    for name, value in record.payload.items():
        if name == "data":
            continue
        dtype = (pa.int64() if name.endswith("_ns") or name in {"sys_id", "comp_id"}
                 else pa.float64() if name == "timestamp" else pa.string())
        metadata[name] = pa.array([value], type=dtype)
    stream.log(f"/records{topic_path(record.topic)}", rr.AnyValues(**metadata))
    if record.payload["direction"] == "rx":
        for metric, value in telemetry_fields(record.payload["msg_type"], record.payload["data"]).items():
            stream.log(f"telemetry/{metric}", rr.Scalars(value))
    if record.payload["direction"] == "input":
        for axis in ("left_x", "left_y", "right_x", "right_y"):
            value = record.payload["data"].get(axis)
            if isinstance(value, (int,float)):
                stream.log(f"telemetry/joystick/{axis}", rr.Scalars(value))
    if diagnostics is not None:
        diagnostics.log(stream, topic=record.topic, message_type=record.payload["msg_type"],
                        data=record.payload["data"], direction=record.payload["direction"],
                        sys_id=record.payload["sys_id"], comp_id=record.payload["comp_id"])


class RrdLogger:
    def __init__(self, path):
        self.output_path = Path(path).with_suffix(".rrd")
        self.active_path = partial_path(self.output_path)
        self.stream = None
        self.started_ns = None
        self.closed = False
        self.diagnostics = RecordDiagnostics()

    def open(self):
        import time
        if self.stream: return
        if self.closed: raise RuntimeError("Recorder already closed")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.started_ns = time.time_ns()
        self.stream = rr.RecordingStream("mission-control-live", batcher_config=rr.ChunkBatcherConfig(
            flush_tick=timedelta(seconds=1), flush_num_bytes=4 * 1024 * 1024))
        self.stream.set_sinks(rr.FileSink(str(self.active_path), write_footer=True))

    def write(self, record):
        if not self.stream: raise RuntimeError("RRD writer is not open")
        log_record(self.stream, record, started_ns=self.started_ns, diagnostics=self.diagnostics)

    def flush(self):
        if self.stream: self.stream.flush(timeout_sec=2)

    def periodic_flush(self):
        # The disk stream batches on its own timer/byte threshold. Explicit
        # flush() still drains it for callers and shutdown.
        pass

    def close(self):
        if self.closed: return
        self.closed = True
        if self.stream:
            self.flush()
            self.stream.disconnect()
            publish_file(self.active_path, self.output_path)
