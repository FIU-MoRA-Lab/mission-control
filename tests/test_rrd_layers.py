"""RRD loading preserves data and discovers shared replay layer controls."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import rerun as rr
from rerun.chunk import RrdReader
from backend.rerun.rrd import open_rrd
from backend.rerun.schema import field_catalog, default_fields


class RrdLayerTests(unittest.TestCase):
    def test_rrd_delivery_allows_slow_flush_and_releases_failed_streams(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.rrd"
            writer = rr.RecordingStream("flush-regression")
            writer.save(str(source))
            writer.log("telemetry/speed", rr.Scalars(2.5), static=True)
            writer.flush()
            writer.disconnect()

            def slow_flush(*, timeout_sec):
                if timeout_sec <= 5:
                    raise RuntimeError("Flush timed out - not all log messages were sent")

            stream = Mock()
            stream.flush.side_effect = slow_flush
            with patch("backend.rerun.rrd.rr.RecordingStream", return_value=stream):
                result = open_rrd(source, url="rerun+http://127.0.0.1:9876/proxy")
                self.assertEqual(result["filename"], "source.rrd")
                stream.disconnect.assert_called_once()

                stream.reset_mock()
                stream.flush.side_effect = RuntimeError("Flush timed out - not all log messages were sent")
                with self.assertRaisesRegex(TimeoutError, "source.rrd.*60 seconds"):
                    open_rrd(source, url="rerun+http://127.0.0.1:9876/proxy")
                stream.disconnect.assert_called_once()

                stream.reset_mock()
                stream.flush.side_effect = RuntimeError("gRPC connection severed")
                with self.assertRaisesRegex(RuntimeError, "gRPC connection severed"):
                    open_rrd(source, url="rerun+http://127.0.0.1:9876/proxy")
                stream.disconnect.assert_called_once()

    def test_rrd_reopens_with_separate_identity_and_geographic_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/"source.rrd", Path(tmp)/"reopened.rrd"
            stream = rr.RecordingStream("mission-control-live")
            stream.save(str(source))
            stream.set_time("mission", duration=1)
            stream.log("telemetry/speed", rr.Scalars(2.5))
            topic, field = "/mavlink/GPS_RAW_INT", "lat"
            raw_path = f"sensors/{rr.escape_entity_path_part(topic)}/{field}"
            map_path = f"geo/readings/{rr.escape_entity_path_part(topic)}/{field}"
            stream.log(raw_path, rr.Scalars(257500000))
            stream.log(map_path, rr.GeoPoints(lat_lon=[25.75, -80.3]))
            stream.log("water/temperature", rr.Scalars(21.5))
            original_id = stream.get_recording_id()
            stream.flush()
            stream.disconnect()
            result = open_rrd(source, destination=output)
            self.assertNotEqual(result["recording_id"], original_id)
            self.assertIn("/telemetry/speed", result["visible_fields"])
            raw = next(f for f in result["field_catalog"] if f["source"] == f"{topic}.{field}")
            self.assertEqual(raw["map_path"], "/"+map_path)
            self.assertEqual(raw["unit"], "degE7")
            self.assertIn("/water/temperature", {f["id"] for f in result["field_catalog"]})
            chunks = list(RrdReader(output).stream())
            scalar = next(c for c in chunks if c.entity_path == "/"+raw_path)
            values = scalar.to_record_batch().column("Scalars:scalars").to_pylist()
            self.assertEqual(values, [[257500000.0]])

    def test_writer_uses_topic_hierarchy_and_reopened_layers_match(self):
        from backend.logger.records import Record, input_record
        from backend.logger.rrd_logger import RrdLogger
        with tempfile.TemporaryDirectory() as tmp:
            writer = RrdLogger(Path(tmp) / "hierarchy")
            writer.open()
            stamp = writer.started_ns + 1_000_000
            for direction, topic in [("rx", "/mavlink/SERVO_OUTPUT_RAW"),
                                     ("tx", "/commands/SERVO_OUTPUT_RAW")]:
                payload = dict(data={"servo3_raw": 1500}, direction=direction,
                               msg_type="SERVO_OUTPUT_RAW", clock="ground_station",
                               sys_id=3, comp_id=1)
                writer.write(Record(topic, payload, stamp, stamp))
            writer.write(input_record({"left_x": .25}, timestamp_ns=stamp))
            writer.close()
            paths = {chunk.entity_path for chunk in RrdReader(writer.output_path).stream()}
            expected = {"/mavlink/SERVO_OUTPUT_RAW/servo3_raw",
                        "/commands/SERVO_OUTPUT_RAW/servo3_raw", "/input/JOYSTICK/left_x"}
            self.assertTrue(expected <= paths, paths)
            self.assertFalse(any("\\" in path for path in paths), paths)
            result = open_rrd(writer.output_path, destination=Path(tmp) / "reopened.rrd")
            self.assertTrue(expected <= {f["path"] for f in result["field_catalog"]})
            self.assertIn("/mavlink/SERVO_OUTPUT_RAW/servo3_raw", result["visible_fields"])

    def test_control_defaults_follow_message_fields_across_topic_prefixes(self):
        for prefix in ("", "/mavlink/", "/vehicle/"):
            catalog = field_catalog({
                prefix + "RC_CHANNELS": {"chan1_raw", "chan2_raw", "chan3_raw"},
                prefix + "SERVO_OUTPUT_RAW": {"servo1_raw", "servo2_raw", "servo3_raw"},
                "/commands/RC_CHANNELS_OVERRIDE": {"chan1_raw", "chan3_raw"},
            }, telemetry={"speed", "battery_pct"})
            selected = set(default_fields(catalog))
            self.assertEqual(len(selected), 5)
            self.assertNotIn("/telemetry/battery_pct", selected)
            self.assertEqual({f["source"] for f in catalog if f["id"] in selected and f["kind"] == "raw"}, {
                prefix + "RC_CHANNELS.chan1_raw", prefix + "RC_CHANNELS.chan3_raw",
                prefix + "SERVO_OUTPUT_RAW.servo1_raw", prefix + "SERVO_OUTPUT_RAW.servo3_raw",
            })

    def test_rrd_control_outputs_are_selected_when_reopened(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/"controls.rrd", Path(tmp)/"reopened.rrd"
            stream = rr.RecordingStream("mission-control-live")
            stream.save(str(source))
            stream.set_time("mission", duration=1)
            for topic, fields in {"RC_CHANNELS": {"chan1_raw", "chan3_raw"},
                                  "SERVO_OUTPUT_RAW": {"servo1_raw", "servo3_raw"}}.items():
                for field in fields:
                    path = f"sensors/{rr.escape_entity_path_part('/vehicle/' + topic)}/{field}"
                    stream.log(path, rr.Scalars(1500))
            stream.flush()
            stream.disconnect()
            result = open_rrd(source, destination=output)
            self.assertEqual(len(result["visible_fields"]), 4)
            self.assertEqual(set(result["visible_fields"]), set(default_fields(result["field_catalog"])))

    def test_catalog_explains_duplicates_and_array_units(self):
        catalog = field_catalog({"GLOBAL_POSITION_INT": {"lat"}, "GPS_RAW_INT": {"lat"},
                                 "BATTERY_STATUS": {"voltages[0]"}, "/commands/MANUAL_CONTROL": {"x"}})
        fields = {f["source"]: f for f in catalog}
        self.assertIn("estimated", fields["GLOBAL_POSITION_INT.lat"]["description"])
        self.assertIn("receiver", fields["GPS_RAW_INT.lat"]["description"])
        self.assertIn("10,000,000", fields["GPS_RAW_INT.lat"]["description"])
        self.assertEqual(fields["BATTERY_STATUS.voltages[0]"]["unit"], "mV")
        self.assertEqual(fields["/commands/MANUAL_CONTROL.x"]["direction"], "sent")

    def test_control_chain_shares_groups_and_missing_fields_are_explicit(self):
        catalog = field_catalog({"/commands/MANUAL_CONTROL": {"x"}, "RC_CHANNELS": {"chan2_raw", "chan3_raw"}})
        fields = {f["source"]: f for f in catalog}
        requested = next(f for f in catalog if f["id"] == "/telemetry/manual/throttle")
        self.assertEqual(requested["category"], fields["/commands/MANUAL_CONTROL.x"]["category"])
        self.assertEqual(requested["category"], fields["RC_CHANNELS.chan3_raw"]["category"])
        self.assertTrue(fields["/commands/MANUAL_CONTROL.x"]["available"])
        self.assertFalse(fields["/commands/RC_CHANNELS_OVERRIDE.chan3_raw"]["available"])
        self.assertEqual(fields["RC_CHANNELS.chan2_raw"]["label"], "RC_CHANNELS.chan2_raw")
        self.assertTrue(fields["RC_CHANNELS.chan2_raw"]["available"])
        self.assertEqual(fields["MANUAL_CONTROL.x"]["direction"], "received")

    def test_active_thrusters_are_distinct_from_other_servo_outputs(self):
        catalog = field_catalog({"/mavlink/SERVO_OUTPUT_RAW": {"servo1_raw", "servo2_raw", "servo3_raw"}}, telemetry=set())
        fields = {f["label"]: f for f in catalog}
        first, third = fields["SERVO_OUTPUT_RAW.servo1_raw"], fields["SERVO_OUTPUT_RAW.servo3_raw"]
        self.assertEqual(first["category"], third["category"])
        self.assertNotEqual(first["category"], fields["SERVO_OUTPUT_RAW.servo2_raw"]["category"])
        self.assertTrue(fields["SERVO_OUTPUT_RAW.servo2_raw"]["available"])
        self.assertEqual(first["display_label"], "Thruster output 1 · SERVO_OUTPUT_RAW.servo1_raw")
        self.assertIn("unverified", first["description"])
        self.assertEqual(first["direction"], "received")
        self.assertEqual(first["unit"], "us")


if __name__ == "__main__":
    unittest.main()
