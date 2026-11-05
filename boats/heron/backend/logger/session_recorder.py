"""UI-independent asynchronous session recorder composing selectable file writers."""
from boats.heron.backend.logger.records import json_safe
import queue
import threading
import time
from pathlib import Path
from boats.heron.paths import session_path, log_directory
from .records import mavlink_record, input_record
from .mcap_logger import McapLogger
from .jsonl_logger import JsonlLogger

def _rrd_writer(path):
    # MCAP/JSONL-only scripts do not need the Rerun SDK installed or imported.
    from .rrd_logger import RrdLogger
    return RrdLogger(path)

WRITERS = {"mcap":McapLogger,"jsonl":JsonlLogger,"rrd":_rrd_writer}

class SessionRecorder:
    def __init__(self, output_path=None, *, formats=("mcap","jsonl","rrd"), msg_types=None,
                 auto_path=True, session_label="session", print_stdout=False):
        if not formats or any(name not in WRITERS for name in formats):
            raise ValueError("Choose mcap, jsonl and/or rrd")
        self.output_path = (Path(output_path).with_suffix(".mcap") if output_path else
                            session_path(session_label).with_suffix(".mcap") if auto_path else None)
        self.json_path = self.output_path.with_suffix(".jsonl") if self.output_path else None
        self.rrd_path = self.output_path.with_suffix(".rrd") if self.output_path else None
        self.writers = {name:WRITERS[name](self.output_path) for name in dict.fromkeys(formats)} if self.output_path else {}
        self.active_path = self.writers["mcap"].active_path if "mcap" in self.writers else None
        self.active_json_path = self.writers["jsonl"].active_path if "jsonl" in self.writers else None
        self.msg_types = {name.upper() for name in msg_types} if msg_types else None
        self.print_stdout = print_stdout
        self.msg_count = 0
        self.errors = []
        self._observers = []
        self._queue = queue.Queue(maxsize=8192)
        self._stop = threading.Event()
        self._lifecycle = threading.RLock()
        self._opened = False
        self._closed = False
        self._last_flush = 0.0
        self._worker = threading.Thread(target=self._run, name="session-recorder", daemon=True)

    def subscribe(self, observer):
        """Observer receives a Record after writes; never runs in the input/transport thread."""
        self._observers.append(observer)

    def open(self):
        with self._lifecycle:
            if self._closed: raise RuntimeError("Recorder already closed")
            if self._opened: return
            opened = []
            try:
                for writer in self.writers.values():
                    writer.open()
                    opened.append(writer)
            except Exception:
                for writer in opened: writer.close()
                raise
            self._opened = True
            self._last_flush = time.monotonic()
            self._worker.start()

    def write(self, record):
        with self._lifecycle:
            if not self._opened or self._closed:
                raise RuntimeError("Recorder is not open")
            try: self._queue.put_nowait(record)
            except queue.Full: self.errors.append("Recorder queue full; record dropped")
        return record.payload

    def log_message(self, message, *, direction="rx", receive_timestamp_ns=None):
        if message is None or (self.msg_types and message.get_type().upper() not in self.msg_types): return
        return self.write(mavlink_record(message, direction=direction, receive_timestamp_ns=receive_timestamp_ns))

    def log_input(self, data, *, timestamp_ns=None):
        return self.write(input_record(data, timestamp_ns=timestamp_ns))

    def _run(self):
        while not self._stop.is_set() or not self._queue.empty():
            try: record = self._queue.get(timeout=.05)
            except queue.Empty: continue
            try:
                for name, writer in self.writers.items():
                    try: writer.write(record)
                    except Exception as exc: self.errors.append(f"{name}: {exc}")
                self.msg_count += 1
                for observer in self._observers:
                    try: observer(record)
                    except Exception as exc: self.errors.append(f"observer: {exc}")
                if self.print_stdout:
                    import json
                    print(json.dumps(json_safe(record.payload), allow_nan=False))
                if time.monotonic() - self._last_flush >= 1:
                    self._flush_writers(periodic=True)
                    self._last_flush = time.monotonic()
            finally:
                self._queue.task_done()

    def _flush_writers(self, *, periodic=False):
        for name, writer in self.writers.items():
            try:
                if periodic and hasattr(writer, "periodic_flush"):
                    writer.periodic_flush()
                else:
                    writer.flush()
            except Exception as exc: self.errors.append(f"{name}: {exc}")

    def flush(self):
        if self._opened:
            self._queue.join()
            self._flush_writers()

    def close(self):
        with self._lifecycle:
            if self._closed: return
            self._closed = True
            self._stop.set()
        if self._opened: self._worker.join()
        for name, writer in self.writers.items():
            try: writer.close()
            except Exception as exc: self.errors.append(f"{name}: {exc}")

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_): self.close()


def default_log_directory(): return log_directory()
