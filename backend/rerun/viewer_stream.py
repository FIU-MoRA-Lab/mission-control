"""Optional viewer adapter; durable file writing belongs to logger/rrd_logger.py."""
import rerun as rr
from backend.logger.rrd_logger import RrdLogger, log_record
from backend.rerun.diagnostics import RecordDiagnostics

class ViewerStream:
    def __init__(self, path, url=None, *, disk_writer=None):
        self._owns_writer = not isinstance(disk_writer, RrdLogger)
        self.writer = RrdLogger(path) if self._owns_writer else disk_writer
        self.writer.open()
        self.path = self.writer.output_path
        self.active_path = self.writer.active_path
        self.disk = self.writer.stream
        self.viewer = None
        self._closed = False
        self.diagnostics = RecordDiagnostics()
        if url:
            self.viewer = rr.RecordingStream("mission-control-live", recording_id=self.disk.get_recording_id())
            self.viewer.connect_grpc(url)

    def record(self, record):
        if self._owns_writer:
            self.writer.write(record)
        if self.viewer:
            log_record(self.viewer, record, started_ns=self.writer.started_ns, diagnostics=self.diagnostics)

    def log(self, *args, **kwargs):
        self.disk.log(*args, **kwargs)
        if self.viewer: self.viewer.log(*args, **kwargs)

    def set_time(self, *args, **kwargs):
        self.disk.set_time(*args, **kwargs)
        if self.viewer: self.viewer.set_time(*args, **kwargs)

    def reset_time(self):
        self.disk.reset_time()
        if self.viewer: self.viewer.reset_time()

    def send_blueprint(self, *args, **kwargs):
        self.disk.send_blueprint(*args, **kwargs)
        if self.viewer: self.viewer.send_blueprint(*args, **kwargs)

    def flush(self, *, timeout_sec=2):
        if not self.writer.closed: self.writer.flush()
        if self.viewer:
            try: self.viewer.flush(timeout_sec=timeout_sec)
            except RuntimeError as exc:
                if "gRPC connection severed" not in str(exc): raise

    def disconnect(self):
        if self._closed: return
        self._closed = True
        try:
            if self._owns_writer: self.writer.close()
        finally:
            if self.viewer: self.viewer.disconnect()
