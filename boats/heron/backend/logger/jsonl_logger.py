"""JSON Lines writer; one complete clock-aware Record per line."""
from boats.heron.backend.logger.records import json_safe
import json
from pathlib import Path
from boats.heron.paths import partial_path, publish_file

class JsonlLogger:
    def __init__(self, path):
        self.output_path = Path(path).with_suffix(".jsonl")
        self.active_path = partial_path(self.output_path)
        self._file = None
        self.closed = False

    def open(self):
        if self._file and not self._file.closed: return
        if self.closed: raise RuntimeError("Recorder already closed")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.active_path.open("w", encoding="utf-8")

    def write(self, record):
        if not self._file or self._file.closed: raise RuntimeError("JSONL writer is not open")
        self._file.write(json.dumps(json_safe(record.payload), allow_nan=False, separators=(",",":")) + "\n")

    def flush(self):
        if self._file and not self._file.closed: self._file.flush()

    def close(self):
        if self.closed: return
        self.closed = True
        if self._file and not self._file.closed:
            self._file.flush()
            self._file.close()
            publish_file(self.active_path, self.output_path)
