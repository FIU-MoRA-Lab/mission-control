"""Exercise stream decoding, loss/reconnect, shutdown, and the split layout."""
from io import BytesIO
import shutil
import socket
import subprocess
import threading
import time
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from boats.heron.backend.camera import CameraFeed, validate_source
from boats.heron.visualization.blueprint import blueprint
from boats.heron.backend.bridge.runtime import MissionBackend
import rerun as rr
from rerun.chunk import RrdReader


class CameraTests(unittest.TestCase):
    def test_split_keeps_map_and_camera_identities_across_presets(self):
        first = blueprint(catalog=[], preset="controls").root_container.contents[0]
        second = blueprint(catalog=[], preset="state_changes").root_container.contents[0]
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.column_shares, [1, 1])
        self.assertEqual([view.class_identifier for view in first.contents], ["Map", "2D"])
        self.assertEqual([view.id for view in first.contents], [view.id for view in second.contents])
        self.assertEqual(first.contents[1].contents, ["/camera/image"])

    def test_rejects_local_files_and_embedded_passwords(self):
        for source in [None, "", "/dev/video0", "file:///tmp/video.mp4", "rtsp://pi:secret@host/live", "tcp://host:99999"]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                validate_source(source)
        self.assertEqual(validate_source(" tcp://192.168.2.2:5601 "), "tcp://192.168.2.2:5601")

    def test_missing_decoder_is_reported_and_can_be_disabled(self):
        messages = []
        feed = CameraFeed(lambda *_: None, messages.append)
        try:
            with patch("boats.heron.backend.camera.shutil.which", return_value=None):
                feed.configure(enabled=True)
                feed._thread.join(timeout=2)
            self.assertIn("Install FFmpeg", feed.state()["status"])
            self.assertFalse(feed.state()["connected"])
            feed.configure(enabled=False)
            self.assertEqual(feed.state()["status"], "Camera off")
        finally:
            feed.close()

    def test_camera_frames_save_with_receipt_time_and_without_vehicle_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "camera.rrd"
            stream = rr.RecordingStream("camera-test")
            stream.save(path)
            backend = MissionBackend.__new__(MissionBackend)
            backend._telemetry_lock = threading.RLock()
            backend._telemetry_closed = False
            backend._started_at = time.monotonic()
            backend.telemetry = SimpleNamespace(stream=stream)
            from boats.heron.backend.camera import placeholder
            backend._camera_frame(placeholder("Test"), 1_700_000_000_000_000_000)
            stream.flush()
            stream.disconnect()
            reader = RrdReader(path)
            chunks = list(reader.stream(store=reader.recordings()[0]))
            images = [chunk for chunk in chunks if chunk.entity_path == "/camera/image"]
            self.assertTrue(images)
            for chunk in images:
                timelines = chunk.to_record_batch().schema.names
                self.assertIn("mission", timelines)
                self.assertIn("receive_time", timelines)
                self.assertNotIn("vehicle_time", timelines)

    def test_reported_frame_rate_uses_received_frames_and_resets_on_disconnect(self):
        feed = CameraFeed(lambda *_: None, lambda *_: None)
        with patch("boats.heron.backend.camera.time.monotonic", return_value=102):
            feed._enabled = True
            feed._last_frame = 102
            feed._frame_times.extend(101 + frame / 30 for frame in range(31))
            self.assertEqual(feed.state()["fps"], 30)
            feed._status("Camera unavailable · retrying")
            self.assertEqual(feed.state()["fps"], 0)

    def test_run_rrds_include_only_their_camera_frames_with_receipt_timestamps(self):
        from boats.heron.backend.control.recording import RunRecording
        from boats.heron.backend.logger.session_recorder import SessionRecorder
        from boats.heron.backend.camera import placeholder
        import pyarrow as pa
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = SessionRecorder(root/'session',formats=('mcap','jsonl'))
            source.open()
            stream = rr.RecordingStream('camera-session-test')
            stream.save(root/'session.rrd')
            backend = MissionBackend.__new__(MissionBackend)
            backend._telemetry_lock = threading.RLock()
            backend._telemetry_closed = False
            backend._started_at = time.monotonic()
            backend.telemetry = SimpleNamespace(stream=stream)
            backend.programmatic = SimpleNamespace(recording=None)
            first,second = placeholder('First run'),placeholder('Second run')
            try:
                for index,contents in enumerate((first,second)):
                    run = RunRecording(root/f'run-{index}',source)
                    backend.programmatic.recording = run
                    stamp = run.started_ns+1_000_000
                    run.ended_ns = stamp
                    # Simulate a prior vehicle-clock log to catch timeline leakage.
                    writer = run.recorder.writers['rrd']
                    writer.stream.set_time('vehicle_boot_1_1',duration=123)
                    backend._camera_frame(placeholder('Before'),run.started_ns-1)
                    backend._camera_frame(contents,stamp)
                    backend._camera_frame(placeholder('After'),stamp+1)
                    with patch('boats.heron.backend.bridge.runtime.time.time_ns',return_value=stamp):
                        backend._camera_status('Camera unavailable')
                    self.assertEqual(run.close(),[])
                    run.camera_frame(placeholder('Closed'),stamp)
                    reader = RrdReader(root/f'run-{index}'/'recording.rrd')
                    frames = [chunk.to_record_batch() for chunk in reader.stream()
                              if chunk.entity_path == '/camera/image']
                    self.assertEqual(sum(frame.num_rows for frame in frames),1)
                    schema = frames[0].schema
                    self.assertIn('mission',schema.names)
                    self.assertIn('receive_time',schema.names)
                    self.assertNotIn('vehicle_boot_1_1',schema.names)
                    self.assertEqual(frames[0].column('receive_time').cast(pa.int64()).to_pylist(),[stamp])
                    blob = next(name for name in schema.names if 'blob' in name.lower())
                    self.assertEqual(bytes(frames[0].column(blob).to_pylist()[0][0]),contents)
            finally:
                source.close()
                stream.disconnect()
            session_frames = [chunk.to_record_batch() for chunk in RrdReader(root/'session.rrd').stream()
                              if chunk.entity_path == '/camera/image']
            self.assertEqual(sum(frame.num_rows for frame in session_frames),8)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for stream integration")
    def test_network_frames_reconnect_and_stop_without_vehicle(self):
        sample = subprocess.run([
            shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x240:r=30",
            "-t", "1", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-f", "matroska", "pipe:1"], capture_output=True, check=True, timeout=10).stdout
        frames, messages = [], []
        received = threading.Event()
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            server.settimeout(8)
            source = f"tcp://127.0.0.1:{server.getsockname()[1]}"

            def send_stream():
                # Two separate connections prove EOF recovery, not just initial decode.
                for _ in range(2):
                    try:
                        conn, _ = server.accept()
                        with conn:
                            conn.sendall(sample)
                    except OSError:
                        return

            sender = threading.Thread(target=send_stream, daemon=True)
            sender.start()

            def on_frame(contents, stamp):
                frames.append((contents, stamp))
                if len(frames) >= 60:
                    received.set()

            feed = CameraFeed(on_frame, messages.append, source=source)
            try:
                feed.configure(enabled=True)
                self.assertTrue(received.wait(12), f"Only {len(frames)} frames: {messages}")
                with Image.open(BytesIO(frames[0][0])) as image:
                    self.assertEqual(image.size, (640, 480))
                    r, g, b = image.getpixel((320, 240))
                    self.assertGreater(r, 200)
                    self.assertLess(g + b, 40)
                self.assertGreater(frames[0][1], 0)
                self.assertIn("Camera unavailable · retrying", messages)
            finally:
                started = time.monotonic()
                feed.close()
                self.assertLess(time.monotonic() - started, 2)
                self.assertFalse(feed.state()["connected"])
                sender.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
