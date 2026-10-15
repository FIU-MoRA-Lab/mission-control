"""Preserve RRD values/clocks across compaction and keep originals on failure."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pyarrow as pa
import rerun as rr
from rerun.chunk import RrdReader

from backend.logger.compact import compact_rrd, fingerprint
from backend.logger.rrd_logger import RrdLogger
from backend.logger.session_recorder import SessionRecorder
from backend.rerun.live import RerunTelemetry


class RrdCompactionTests(unittest.TestCase):
    def fragmented_recording(self, path):
        stream = rr.RecordingStream("compaction-test")
        stream.set_sinks(rr.FileSink(str(path)))
        for i in range(40):
            stream.reset_time()
            stream.set_time("mission", sequence=i)
            if i % 2:
                stream.set_time("vehicle_boot_1_1", sequence=i * 3)
            stream.log("samples", rr.AnyValues(
                value=pa.array([2**63 + i], type=pa.uint64()),
                nullable=pa.array([None if i % 3 else i], type=pa.int64()),
                text=f"sample {i}", vector=[float(i), float("nan")]))
            stream.flush()
        stream.disconnect()

    def test_compaction_preserves_cells_and_clocks_and_reduces_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.rrd"
            self.fragmented_recording(path)
            before = fingerprint(path)
            size = path.stat().st_size
            chunks = sum(1 for _ in RrdReader(path).stream())
            self.assertTrue(compact_rrd(path))
            self.assertEqual(fingerprint(path), before)
            self.assertLess(path.stat().st_size, size)
            self.assertLess(sum(1 for _ in RrdReader(path).stream()), chunks)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_verification_failure_leaves_original_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.rrd"
            self.fragmented_recording(path)
            before = path.read_bytes()
            with patch("backend.logger.compact.fingerprint", side_effect=["original", "changed"]):
                with self.assertRaisesRegex(ValueError, "changed stored"):
                    compact_rrd(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_close_publishes_original_when_optimizer_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = RrdLogger(Path(tmp) / "test.rrd")
            writer.open()
            writer.stream.log("sample", rr.Scalars(42))
            with patch("backend.logger.compact.compact_rrd", side_effect=RuntimeError("disk full")):
                with self.assertLogs("backend.logger.rrd_logger", level="WARNING"):
                    writer.close()
            writer.close()
            self.assertEqual(writer.compaction_error, "disk full")
            self.assertTrue(writer.output_path.exists())
            self.assertFalse(writer.active_path.exists())
            self.assertGreater(fingerprint(writer.output_path)[1], 0)

    def test_periodic_flush_does_not_force_rrd_batches_but_explicit_flush_does(self):
        recorder = SessionRecorder(auto_path=False)
        disk = Mock(spec=RrdLogger)
        mcap = Mock(spec=["flush"])
        recorder.writers = {"rrd": disk, "mcap": mcap}
        recorder._flush_writers(periodic=True)
        disk.periodic_flush.assert_called_once()
        disk.flush.assert_not_called()
        mcap.flush.assert_called_once()
        recorder._flush_writers()
        disk.flush.assert_called_once()


class LiveStorageTests(unittest.TestCase):
    def test_status_changes_and_track_blocks_preserve_path_without_growing_snapshots(self):
        stream = Mock()
        adapter = RerunTelemetry(stream, dict(crs="EPSG:4326", bounds=[], center=[]))
        state = dict(simulating=False, manual=dict(throttle=0, yaw=0, source="NONE", active=False),
                     mode="HOLD", armed=False, connected=True, lon=-80.0, lat=25.0,
                     waypoints=[], lake={}, status="")
        adapter.state(state, 0)
        adapter.state(state, 1)
        for i in range(1, 260):
            adapter.state(state | dict(lat=25 + i * .00001), i + 1)
        adapter.state(state | dict(lat=25 + 259 * .00001, mode="AUTO"), 261)
        calls = stream.log.call_args_list
        self.assertEqual(sum(c.args[0] == "vehicle/status" for c in calls), 2)
        self.assertEqual(sum(c.args[0] == "commands/input" for c in calls), 1)
        self.assertEqual(sum(c.args[0] == "geo/vehicle" for c in calls), 260)
        blocks = {}
        for call in calls:
            if call.args[0].startswith("geo/track/"):
                points = call.args[1].line_strings.as_arrow_array().to_pylist()[0]
                self.assertLessEqual(len(points), 128)
                blocks[call.args[0]] = points
        self.assertEqual(len(blocks), 3)
        path = []
        for block in blocks.values():
            if path:
                self.assertEqual(path[-1], block[0])
                path.extend(block[1:])
            else:
                path.extend(block)
        self.assertEqual(path, [[25 + i * .00001, -80.0] for i in range(260)])
