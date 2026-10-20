"""Verify durable typed values rather than merely checking logging calls."""
import json
import math
from pathlib import Path
import tempfile
import unittest
import pyarrow as pa
from rerun.chunk import RrdReader
from backend.logger.records import input_record, mavlink_record, restore_nonfinite
from backend.logger.rrd_logger import RrdLogger
from backend.logger.jsonl_logger import JsonlLogger
from backend.logger.mcap_logger import McapLogger
from pymavlink.dialects.v20 import ardupilotmega as mavlink
from mcap.reader import make_reader


class TypedRecordingTests(unittest.TestCase):
    def test_joystick_direction_changes_keep_one_signed_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = RrdLogger(Path(tmp) / "joystick")
            writer.open()
            for value in (0, 1000, -1000, 0):
                writer.write(input_record(dict(requested_throttle=value, requested_yaw=-value)))
            writer.close()
            samples = {name: [] for name in ("requested_throttle", "requested_yaw")}
            for chunk in RrdReader(writer.output_path).stream():
                name = chunk.entity_path.rsplit("/", 1)[-1]
                if chunk.entity_path != "/input/JOYSTICK/" + name or name not in samples:
                    continue
                batch = chunk.to_record_batch()
                if "value" in batch.schema.names:
                    column = batch.column("value")
                    self.assertEqual(column.type.value_type, pa.int64())
                    samples[name].extend(row[0] for row in column.to_pylist())
            self.assertEqual(samples["requested_throttle"], [0, 1000, -1000, 0])
            self.assertEqual(samples["requested_yaw"], [0, -1000, 1000, 0])

    def test_custom_values_survive_rrd_and_json_formats(self):
        data = dict(text="Battery low", enabled=False, missing=None, nan=float("nan"),
                    positive=float("inf"), negative=-float("inf"), empty=[],
                    array=[1, None, 3], large=2**64-1)
        with tempfile.TemporaryDirectory() as tmp:
            record = input_record(data)
            for writer_type in (RrdLogger, JsonlLogger, McapLogger):
                writer = writer_type(Path(tmp)/"complete")
                writer.open(); writer.write(record); writer.close()
            chunks = {c.entity_path: c.to_record_batch() for c in RrdReader(Path(tmp)/"complete.rrd").stream() if "state" in c.to_record_batch().schema.names}
            for name in data:
                self.assertIn("/input/JOYSTICK/"+name, chunks)
            def value(name):
                return chunks["/input/JOYSTICK/"+name].column("value").to_pylist()[0][0]
            self.assertEqual(value("text"), "Battery low")
            self.assertIs(value("enabled"), False)
            self.assertEqual(chunks["/input/JOYSTICK/missing"].column("state").to_pylist(), [["null"]])
            self.assertTrue(math.isnan(value("nan")))
            self.assertEqual(value("positive"), float("inf"))
            self.assertEqual(value("negative"), -float("inf"))
            self.assertEqual(value("array"), [1, None, 3])
            self.assertEqual(value("empty"), [])
            self.assertEqual(value("large"), 2**64-1)
            payloads = [json.loads((Path(tmp)/"complete.jsonl").read_text())]
            with (Path(tmp)/"complete.mcap").open("rb") as handle:
                payloads.extend(json.loads(m.data) for _, _, m in make_reader(handle).iter_messages())
            for payload in payloads:
                restored = restore_nonfinite(payload)["data"]
                self.assertTrue(math.isnan(restored["nan"]))
                self.assertEqual(restored["positive"], float("inf"))
                self.assertIsNone(restored["missing"])
                self.assertIs(restored["enabled"], False)

    def test_protocol_types_text_arrays_and_typed_null(self):
        encoder = mavlink.MAVLink(None, srcSystem=3, srcComponent=1)
        messages = [encoder.statustext_encode(4, b"Battery low"),
                    encoder.system_time_encode(1_791_900_000_000_001, 123),
                    encoder.battery_status_encode(0, 0, 0, 0, [65535]*10, 0, 0, 0, 0),
                    encoder.attitude_encode(123, float("nan"), 0, 0, 0, 0, 0)]
        with tempfile.TemporaryDirectory() as tmp:
            writer = RrdLogger(Path(tmp)/"protocol")
            writer.open()
            for message in messages:
                message.pack(encoder)
                record = mavlink_record(message)
                if message.get_type() == "ATTITUDE":
                    record.payload["data"]["pitch"] = None
                writer.write(record)
            writer.close()
            chunks = {c.entity_path: c.to_record_batch() for c in RrdReader(writer.output_path).stream() if "state" in c.to_record_batch().schema.names}
            text = chunks["/mavlink/STATUSTEXT/text"].column("value")
            self.assertEqual(text.to_pylist(), [["Battery low"]])
            timestamp = chunks["/mavlink/SYSTEM_TIME/time_unix_usec"].column("value")
            self.assertEqual(timestamp.type.value_type, pa.uint64())
            self.assertEqual(timestamp.to_pylist(), [[1_791_900_000_000_001]])
            volts = chunks["/mavlink/BATTERY_STATUS/voltages"].column("value")
            self.assertEqual(volts.type.value_type, pa.list_(pa.uint16(), 10))
            self.assertEqual(volts.to_pylist(), [[[65535]*10]])
            self.assertEqual(chunks["/mavlink/ATTITUDE/pitch"].column("value").to_pylist(), [[None]])

    def test_mcap_replay_retains_text_and_nonfinite_values(self):
        from backend.rerun.replay import export_mcap
        with tempfile.TemporaryDirectory() as tmp:
            writer = McapLogger(Path(tmp)/"source")
            writer.open()
            encoder = mavlink.MAVLink(None, srcSystem=3, srcComponent=1)
            gps = encoder.global_position_int_encode(123, 257591000, -803716000, 2000, 0, 0, 0, 0, 0)
            gps.pack(encoder)
            writer.write(mavlink_record(gps))
            writer.write(input_record(dict(text="hello", enabled=False, missing=None, nan=float("nan"))))
            writer.close()
            output = Path(tmp)/"replay.rrd"
            export_mcap(writer.output_path, output,
                        {"crs": "EPSG:4326", "bounds": [-81, 25, -80, 26], "center": [-80.37, 25.76]},
                        {"features": []})
            batches = {c.entity_path: c.to_record_batch() for c in RrdReader(output).stream()
                       if "state" in c.to_record_batch().schema.names}
            self.assertEqual(batches["/input/JOYSTICK/text"].column("value").to_pylist(), [["hello"]])
            self.assertEqual(batches["/input/JOYSTICK/enabled"].column("value").to_pylist(), [[False]])
            self.assertEqual(batches["/input/JOYSTICK/missing"].column("state").to_pylist(), [["null"]])
            self.assertTrue(math.isnan(batches["/input/JOYSTICK/nan"].column("value").to_pylist()[0][0]))
