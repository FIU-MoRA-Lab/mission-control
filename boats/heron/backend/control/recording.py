"""Per-run recorder, with a timestamp-bounded copy of the session's raw traffic."""
from dataclasses import replace
import time
import threading
from boats.heron.backend.logger.records import input_record
from boats.heron.backend.logger.session_recorder import SessionRecorder


def control_record(data):
    record = input_record(data)
    return replace(record,topic="/input/CONTROL",payload={**record.payload,"msg_type":"CONTROL","direction":"local"})


class RunRecording:
    def __init__(self, directory, source):
        self.source = source
        self.recorder = SessionRecorder(directory / "recording.mcap",formats=("mcap","jsonl","rrd"))
        self.recorder.open()
        self.started_ns = time.time_ns()
        self.ended_ns = None
        self.source_error_offset = len(source.errors)
        self._lock = threading.Lock()
        self._closed = False

    def copy(self, record):
        # SDK state/events are already written directly below, not copied twice.
        if record.topic == "/input/CONTROL":
            return
        stamp = record.receive_timestamp_ns
        with self._lock:
            if not self._closed and stamp >= self.started_ns and (self.ended_ns is None or stamp <= self.ended_ns):
                self.recorder.write(record)

    def log(self, data):
        record = control_record(data)
        self.recorder.write(record)
        # Expose the identical events to the existing live viewer and session log.
        try:
            self.source.write(record)
        except RuntimeError:
            pass  # Standalone tests may have no open session recorder.

    def camera_frame(self, contents, received_ns):
        """Save an encoded JPEG in this run's RRD if its receipt is in the run window."""
        with self._lock:
            if self._closed or received_ns < self.started_ns or (self.ended_ns is not None and received_ns > self.ended_ns):
                return
            try:
                self.recorder.writers["rrd"].write_camera_frame(contents,received_ns)
            except Exception as exc:
                self.recorder.errors.append(f"rrd camera: {exc}")

    def close(self):
        # Drain source observers before closing the destination; no lost queued tail.
        self.source.flush()
        # Producers may enqueue an older acquisition timestamp after the source
        # drain. Serialize admission with close; late callbacks simply stop copying.
        with self._lock:
            self._closed = True
            self.recorder.close()
        return list(self.recorder.errors) + list(self.source.errors[self.source_error_offset:])
