"""Analysis preserves types, explicit null events and independent acquisition times."""
from pathlib import Path
import math
import tempfile
import unittest
import rerun as rr
try:
    import polars as pl
    from boats.heron.backend.analysis.rrd import load_rrd
except ModuleNotFoundError:
    pl = None
from boats.heron.backend.logger.records import input_record
from boats.heron.backend.logger.rrd_logger import RrdLogger


@unittest.skipIf(pl is None, "Install the analysis extra")
class AnalysisTests(unittest.TestCase):
    def test_typed_samples_keep_states_types_and_shared_clock_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = RrdLogger(Path(tmp)/"run")
            writer.open()
            start = writer.started_ns
            writer.write(input_record(dict(text="hello", flag=False, number=float("nan"),
                                           missing=None, large=2**64-1, array=[1, None, 3]), timestamp_ns=start+1_000_000_000))
            writer.write(input_record(dict(text="later", flag=True, number=float("inf"), missing=7,
                                           large=2**64-2, array=[]), timestamp_ns=start+2_000_000_000))
            writer.close()
            run = load_rrd(writer.output_path)
            text = run.samples("/input/JOYSTICK/text")
            self.assertEqual(text["value"].to_list(), ["hello", "later"])
            self.assertEqual(text["time_s"].to_list(), [0., 1.])
            self.assertEqual(text["receive_time_ns"].to_list(), [start+1_000_000_000, start+2_000_000_000])
            missing = run.samples("/input/JOYSTICK/missing")
            self.assertEqual(missing["value"].to_list(), [None, 7])
            self.assertEqual(missing["state"].to_list(), ["null", "present"])
            numbers = run.samples("/input/JOYSTICK/number")
            self.assertTrue(math.isnan(numbers["value"][0]))
            self.assertEqual(numbers["state"].to_list(), ["nan", "+inf"])
            large = run.samples("/input/JOYSTICK/large")
            self.assertEqual(large["value"].dtype, pl.UInt64)
            self.assertEqual(large["value"].to_list(), [2**64-1, 2**64-2])
            self.assertEqual(run.fields["/input/JOYSTICK/flag"]["value"].dtype, pl.Boolean)
            self.assertEqual(run.samples("/input/JOYSTICK/array")["value"].to_list(), [[1, None, 3], []])
            self.assertEqual(run.metadata["/records/input/JOYSTICK"].height, 2)
            self.assertEqual(run.catalog.filter(pl.col("field") == "missing")["nulls"][0], 1)
            with self.assertRaisesRegex(ValueError, "exceeds"):
                load_rrd(writer.output_path, max_rows=1)

    def test_legacy_paths_and_no_implicit_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"legacy.rrd"
            stream = rr.RecordingStream("old")
            stream.save(str(path))
            stream.set_time("mission", duration=1)
            stream.log("telemetry/speed", rr.Scalars(2))
            stream.set_time("mission", duration=2)
            legacy = f"/sensors/{rr.escape_entity_path_part('/mavlink/SERVO_OUTPUT_RAW')}/servo3_raw"
            stream.log(legacy, rr.Scalars(1500))
            stream.flush(); stream.disconnect()
            run = load_rrd(path)
            self.assertEqual(run.samples(legacy)["time_s"].to_list(), [1.])
            self.assertEqual(run.fields[legacy].height, 1)
            row = run.catalog.filter(pl.col("path") == legacy).row(0, named=True)
            self.assertEqual(row["topic"], "/mavlink/SERVO_OUTPUT_RAW")
            self.assertEqual(row["storage"], "legacy scalar")
            self.assertEqual(row["unit"], "us")

    def test_recording_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"selection.rrd"
            stream = rr.RecordingStream("selection")
            stream.save(str(path))
            stream.set_time("mission", duration=1)
            stream.log("telemetry/speed", rr.Scalars(2))
            recording_id = stream.get_recording_id()
            stream.flush(); stream.disconnect()
            self.assertEqual(load_rrd(path, recording_id=recording_id).recording_id, recording_id)
            with self.assertRaisesRegex(ValueError, "recording_id"):
                load_rrd(path, recording_id="unknown")
