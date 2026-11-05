"""Exercise replay GPS alignment without relying on external sample files."""
import json
from pathlib import Path
import tempfile
import unittest

from mcap.writer import Writer
from boats.heron.backend.recordings.mcap_reader import load_recording


class McapReaderTests(unittest.TestCase):
    def read(self, messages):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.mcap"
            with path.open("wb") as handle:
                writer = Writer(handle)
                writer.start()
                for seconds, topic, data in messages:
                    channel = writer.register_channel(topic, "json", 0)
                    writer.add_message(channel, int(seconds * 1e9),
                                       json.dumps({"data": data}).encode(), 0)
                writer.finish()
            return load_recording(path)

    def test_estimated_position_takes_priority_and_distant_samples_stay_unaligned(self):
        recording = self.read([
            (1, "/mavlink/GPS_RAW_INT", {"lat": 260000000, "lon": -810000000, "fix_type": 3}),
            (1, "/mavlink/GLOBAL_POSITION_INT", {"lat": 250000000, "lon": -800000000}),
            (2, "/sensor", {"temperature": 20, "ok": True, "invalid": {"$nonfinite": "nan"}}),
            (4, "/sensor", {"temperature": 99}),
        ])
        self.assertEqual(recording["track"], [{"time": 0, "position": [-80, 25]}])
        sensor = next(layer for layer in recording["datasets"] if layer["topic"] == "/sensor")
        self.assertEqual(sensor["count"], 1)
        self.assertEqual(sensor["points"][0]["values"], {"temperature": 20})
        self.assertEqual(sensor["points"][0]["position"], [-80, 25])
        self.assertEqual(recording["unaligned"], 1)

    def test_sent_coordinates_cannot_create_a_track(self):
        recording = self.read([(1, "/commands/GLOBAL_POSITION_INT",
                                {"lat": 250000000, "lon": -800000000})])
        self.assertEqual(recording["track"], [])
        self.assertEqual(recording["datasets"], [])
        self.assertEqual(recording["messageCount"], 1)

    def test_empty_recording_remains_readable(self):
        recording = self.read([])
        self.assertEqual(recording["track"], [])
        self.assertEqual(recording["duration"], 0)
