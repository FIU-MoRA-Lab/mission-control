"""Bounded video decoding on a worker independent of vehicle input and telemetry."""
from io import BytesIO
from collections import deque
import os
import logging
import shutil
import subprocess
import threading
import time
import tempfile
from urllib.parse import urlsplit

from PIL import Image, ImageDraw

DEFAULT_SOURCE = "tcp://192.168.2.2:5601"
WIDTH, HEIGHT = 640, 480
FRAME_BYTES = WIDTH * HEIGHT * 3


def validate_source(source):
    if not isinstance(source, str) or len(source) > 2048:
        raise ValueError("Enter a camera stream URL")
    source = source.strip()
    url = urlsplit(source)
    if url.scheme not in {"tcp", "rtsp", "http", "https"} or not url.hostname:
        raise ValueError("Use a tcp://, rtsp://, http:// or https:// camera URL")
    if url.username or url.password:
        raise ValueError("Use a camera URL without embedded credentials")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("Choose a valid camera port")
    return source


def jpeg(image):
    output = BytesIO()
    image.save(output, format="JPEG", quality=80)
    return output.getvalue()


def placeholder(message):
    image = Image.new("RGB", (WIDTH, HEIGHT), (18, 24, 32))
    draw = ImageDraw.Draw(image)
    draw.text((WIDTH / 2, HEIGHT / 2 - 18), "CAMERA", anchor="mm", fill=(166, 184, 202), font_size=24)
    draw.text((WIDTH / 2, HEIGHT / 2 + 22), message, anchor="mm", fill=(210, 218, 228), font_size=16)
    return jpeg(image)


class CameraFeed:
    def __init__(self, on_frame, on_status, *, source=None):
        self.source = validate_source(source or os.environ.get("MISSION_CONTROL_CAMERA_URL") or DEFAULT_SOURCE)
        self.on_frame, self.on_status = on_frame, on_status
        self._lock = threading.Lock()
        self._control = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._enabled = False
        self._message = "Camera off"
        self._last_frame = None
        self._frame_times = deque(maxlen=120)
        self._error = ""

    def state(self):
        with self._lock:
            age = None if self._last_frame is None else time.monotonic() - self._last_frame
            connected = self._enabled and age is not None and age < 2
            times = self._frame_times
            fps = ((len(times) - 1) / (times[-1] - times[0])
                   if connected and len(times) > 1 and times[-1] > times[0] else 0)
            return dict(source=self.source, enabled=self._enabled, connected=connected,
                        status="Live" if connected else self._message if age is None else "Camera signal lost · reconnecting",
                        error=self._error, frame_age=age, width=WIDTH, height=HEIGHT, fps=round(fps, 1))

    def _status(self, message):
        with self._lock:
            changed = self._message != message
            self._message = message
            self._last_frame = None
            self._frame_times.clear()
        if changed:
            self.on_status(message)

    def configure(self, *, enabled, source=None):
        if not isinstance(enabled, bool):
            raise ValueError("Camera enabled must be a boolean")
        source = validate_source(source if source is not None else self.source)
        with self._control:
            self._close()
            with self._lock:
                self.source = source
                self._enabled = enabled
                self._error = ""
            if enabled:
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="camera-feed", daemon=True)
                self._thread.start()
            else:
                self._status("Camera off")
        return self.state()

    def _run(self):
        executable = shutil.which("ffmpeg")
        if not executable:
            self._status("Install FFmpeg to view the camera")
            return
        self._status("Connecting to camera…")
        while not self._stop.is_set():
            args = [executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-rw_timeout", "5000000", "-probesize", "32768", "-analyzeduration", "1000000"]
            if self.source.startswith("rtsp://"):
                args += ["-rtsp_transport", "tcp"]
            args += ["-i", self.source, "-an", "-sn", "-dn", "-vf",
                     "scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2",
                     "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
            process = None
            errors = tempfile.TemporaryFile()
            try:
                with self._lock:
                    if self._stop.is_set():
                        break
                    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                               stderr=errors)
                    self._process = process
                while not self._stop.is_set():
                    frame = process.stdout.read(FRAME_BYTES)
                    if len(frame) != FRAME_BYTES or self._stop.is_set():
                        break
                    received_ns = time.time_ns()
                    self.on_frame(jpeg(Image.frombytes("RGB", (WIDTH, HEIGHT), frame)), received_ns)
                    with self._lock:
                        self._last_frame = time.monotonic()
                        self._frame_times.append(self._last_frame)
                        while self._frame_times[0] < self._last_frame - 2:
                            self._frame_times.popleft()
                        self._message = "Live"
                        self._error = ""
            except (OSError, RuntimeError, ValueError) as exc:
                if self._stop.is_set():
                    break
                with self._lock:
                    self._error = str(exc)
            finally:
                if process:
                    self._terminate(process)
                    process.stdout.close()
                with self._lock:
                    self._process = None
                    errors.seek(0)
                    detail = errors.read(2048).decode(errors="replace").strip()
                    if detail:
                        self._error = detail.replace(self.source, "camera stream")
                errors.close()
            if not self._stop.is_set():
                if self.state()["status"] != "Camera unavailable · retrying":
                    logging.getLogger(__name__).warning("Camera disconnected: %s", self.state()["error"] or "Stream ended")
                self._status("Camera unavailable · retrying")
                self._stop.wait(2)

    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _close(self):
        self._stop.set()
        with self._lock:
            self._enabled = False
            self._last_frame = None
            self._frame_times.clear()
            process = self._process
        if process:
            self._terminate(process)
        if self._thread:
            self._thread.join(timeout=1)
            if self._thread.is_alive():
                raise RuntimeError("Camera decoder did not stop")
            self._thread = None

    def close(self):
        with self._control:
            self._close()
