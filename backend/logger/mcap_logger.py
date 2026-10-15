"""Indexed MCAP writer. Accepts Record objects independently of any UI/transport."""
from backend.logger.records import json_safe
import json
from pathlib import Path
from mcap.writer import Writer, CompressionType
from backend.paths import partial_path, publish_file
from .records import field_schema

class McapLogger:
    def __init__(self, path):
        self.output_path = Path(path).with_suffix(".mcap")
        self.active_path = partial_path(self.output_path)
        self._file = self._writer = None
        self._channels = {}
        self.count = 0
        self.closed = False

    def open(self):
        if self._writer: return
        if self.closed: raise RuntimeError("Recorder already closed")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.active_path.open("wb")
        self._writer = Writer(self._file, chunk_size=256 * 1024, compression=CompressionType.ZSTD)
        self._writer.start(library="mission-control-recorder/0.1.0")
        self._writer.add_metadata("mission-control.clocks", {
            "log_time": "ground-station arrival/acquisition Unix nanoseconds (index only)",
            "publish_time": "source timestamp nanoseconds; if unavailable, log_time per MCAP specification; payload.clock remains unavailable",
            "primary_timestamp": "payload timestamp_ns: onboard for rx, ground station for input/tx; never substitute arrival for missing onboard time",
        })

    def write(self, record):
        if not self._writer: raise RuntimeError("MCAP writer is not open")
        channel = self._channels.get(record.topic)
        if channel is None:
            schema = {"$schema":"http://json-schema.org/draft-07/schema#", "title":record.topic,
                      "type":"object", "properties": {
                          **{key:field_schema(value) for key,value in record.payload.items() if key != "data"},
                          "data":{"type":"object","properties":{key:field_schema(value) for key,value in record.payload["data"].items()}},
                      }}
            # Timestamp/ISO can be null for boot-relative or untimestamped messages.
            for key in ("timestamp", "timestamp_ns", "timestamp_iso", "timestamp_field", "onboard_timestamp_ns"):
                schema["properties"][key] = {}
            sid = self._writer.register_schema(name=record.topic, encoding="jsonschema", data=json.dumps(schema).encode())
            channel = self._writer.register_channel(topic=record.topic, message_encoding="json", schema_id=sid,
                metadata={"direction":record.payload["direction"],"timestamp_clock":"See payload.clock; onboard and ground-station clocks remain separate"})
            self._channels[record.topic] = channel
        self._writer.add_message(channel_id=channel, log_time=record.receive_timestamp_ns,
                                 publish_time=record.timestamp_ns if record.timestamp_ns is not None else record.receive_timestamp_ns,
                                 sequence=self.count & 0xffffffff,
                                 data=json.dumps(json_safe(record.payload), allow_nan=False, separators=(",",":")).encode())
        self.count += 1

    def flush(self):
        if self._writer:
            self._writer.flush()
            self._file.flush()

    def close(self):
        if self.closed: return
        self.closed = True
        if self._writer:
            try:
                self._writer.finish()
                self._file.flush()
            finally:
                self._file.close()
                self._writer = None
            publish_file(self.active_path, self.output_path)
