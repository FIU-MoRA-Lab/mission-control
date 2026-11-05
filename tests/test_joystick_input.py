"""Verify physical stick decoding and persistent local input recordings."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pymavlink import mavutil

from mcap.reader import make_reader
from rerun.chunk import RrdReader
from boats.heron.backend.input.joystick import JoystickReader
from boats.heron.backend.controller.vehicle_control import VehicleController
from boats.heron.backend.bridge.runtime import MissionBackend
from backend.mapping.tiles import RasterTiles
from boats.heron.visualization.replay import export_mcap

ROOT = Path(__file__).resolve().parents[1]


class JoystickInputTests(unittest.TestCase):
    def test_all_four_positions_preserve_direction_and_full_travel(self):
        self.assertEqual(JoystickReader.decode_positions([128]*4),
                         dict.fromkeys(('left_x', 'left_y', 'right_x', 'right_y'), 0.0))
        self.assertEqual(JoystickReader.decode_positions([0, 0, 255, 255]),
                         {'left_x': -1.0, 'left_y': 1.0, 'right_x': 1.0, 'right_y': -1.0})
        self.assertNotEqual(JoystickReader.decode_positions([130]*4)['left_x'], 0)
        self.assertEqual(JoystickReader.normalize_axis(130), 0)

    def test_reader_publishes_physical_axes_and_existing_driving_mapping(self):
        import queue
        queue_out = queue.Queue(maxsize=1)
        reader = JoystickReader(queue_out)
        class Device:
            def __init__(self, *args): pass
            def open_path(self, path): pass
            def set_nonblocking(self, enabled): pass
            def read(self, size, timeout_ms):
                reader.running = False
                return [255, 0, 0, 255]
            def close(self): pass
        with patch('boats.heron.backend.input.joystick._HID_AVAILABLE', True), patch('boats.heron.backend.input.joystick.hid', SimpleNamespace(enumerate=lambda: [{'vendor_id':0x046d, 'product_id':1, 'path':b'test'}], device=Device), create=True), patch('time.sleep'):
            reader.run()
        throttle, steering, source, positions, raw, timestamp_ns = queue_out.get_nowait()
        self.assertEqual((throttle, steering, source), (1000, -1000, 'GAMEPAD'))
        self.assertEqual(positions, {'left_x':1.0, 'left_y':1.0, 'right_x':-1.0, 'right_y':-1.0})
        self.assertEqual(raw, [255, 0, 0, 255])
        self.assertGreater(timestamp_ns, 0)

    def test_live_display_updates_while_recording_is_blocked(self):
        import threading
        from unittest.mock import Mock
        from boats.heron.backend.controller.runtime import ControllerRuntime
        from boats.heron.backend.logger.session_recorder import SessionRecorder
        entered, release, updated = threading.Event(), threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            recorder = SessionRecorder(Path(tmp)/"input", formats=("jsonl",))
            controller = VehicleController(recorder=recorder)
            backend = ControllerRuntime(controller, Mock(), inputs=False)
            records = []
            writer = recorder.writers["jsonl"]
            original = writer.write
            def record(item):
                entered.set()
                release.wait(timeout=3)
                records.append((item.payload["data"], item.timestamp_ns))
                original(item)
            writer.write = record
            recorder.open()
            first = dict.fromkeys(('left_x', 'left_y', 'right_x', 'right_y'), 0.0)
            latest = dict(first, left_x=0.8, right_y=-0.7)
            stamp = time.time_ns() - 100_000_000
            try:
                backend._accept_gamepad_sample(0, 0, 'GAMEPAD', first, [128]*4, stamp)
                self.assertTrue(entered.wait(timeout=1))
                def publish():
                    backend._publish_live_joystick(0, 0, 'GAMEPAD', latest)
                    updated.set()
                thread = threading.Thread(target=publish)
                thread.start()
                self.assertTrue(updated.wait(timeout=.2), 'Slow recording blocked physical display input')
                self.assertEqual(backend.joystick_state()['left_x'], 0.8)
                self.assertEqual(records, [])
                thread.join()
            finally:
                release.set()
                backend.close()
            self.assertEqual(records[0][1], stamp)
            self.assertEqual(records[0][0]['left_x'], 0.0)

    def test_hid_burst_reaches_display_without_waiting_for_control_sampling(self):
        import queue
        reports = [[128+i, 128, 128, 128] for i in range(8)]
        shown = []
        reader = JoystickReader(queue.Queue(maxsize=1), lambda *sample: shown.append(sample))
        class Device:
            def open_path(self, path): pass
            def set_nonblocking(self, enabled): pass
            def read(self, size, timeout_ms):
                report = reports.pop(0)
                if not reports: reader.running = False
                return report
            def close(self): pass
        with patch('boats.heron.backend.input.joystick._HID_AVAILABLE', True), patch('boats.heron.backend.input.joystick.hid', SimpleNamespace(enumerate=lambda: [{'vendor_id':0x046d,'product_id':1,'path':b'test'}], device=Device), create=True):
            reader.run()
        self.assertEqual(shown[-1][3]['left_x'], 7/127)
        self.assertTrue(all(any(len(sample)>3 and sample[4][0]==128+i for sample in shown) for i in range(8)))
        self.assertEqual(reader.error, '')

    def test_paused_samples_persist_in_mcap_rrd_and_replay_catalog(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MISSION_CONTROL_DATA_DIR':tmp}):
            controller = VehicleController()
            backend = MissionBackend(controller, RasterTiles(ROOT/'assets/fiu-basemap', tmp), None, demo=True)
            # Use explicit synthetic samples for deterministic recording validation.
            backend.gamepad = None
            backend.start()
            controller.recorder.log_message(mavutil.mavlink.MAVLink(None).gps_raw_int_encode(1, 3, 257500000, -803000000, 0, 0, 0, 0, 0, 10))
            positions = {'left_x':0.25, 'left_y':0.5, 'right_x':-0.75, 'right_y':-1.0}
            try:
                with backend._commands:
                    backend._accept_gamepad_sample(600, -800, 'GAMEPAD', positions, [160, 64, 32, 255])
                state = backend.state(refresh_ui=False)
                self.assertEqual(state['joystick'], dict(positions, connected=True, error=''))
                self.assertEqual(state['manual']['throttle'], 0)
                self.assertFalse(state['manual']['active'])
                self.assertEqual(controller.last_manual_send, None)
                with backend._commands:
                    backend._live_joystick_at -= 0.5
                    backend.expire_inputs(time.monotonic() + 0.5)
                self.assertFalse(backend.state(refresh_ui=False)['joystick']['connected'])
            finally:
                backend.close()
            with controller.recorder.output_path.open('rb') as handle:
                messages = list(make_reader(handle, validate_crcs=True).iter_messages())
            messages = [entry for entry in messages if entry[1].topic == "/input/JOYSTICK"]
            self.assertEqual(len(messages), 2)
            channel = messages[0][1]
            self.assertEqual(channel.topic, '/input/JOYSTICK')
            data = json.loads(messages[0][2].data)
            self.assertEqual(data['direction'], 'input')
            self.assertEqual(data['data']['left_x'], 0.25)
            self.assertEqual(data['data']['raw_axes'], [160, 64, 32, 255])
            self.assertFalse(json.loads(messages[1][2].data)['data']['connected'])
            chunks = list(RrdReader(backend.live_recording).stream())
            axes = {c.entity_path for c in chunks if c.entity_path.startswith('/telemetry/joystick/')}
            self.assertEqual(axes, {f'/telemetry/joystick/{name}' for name in positions})
            left = next(c for c in chunks if c.entity_path == '/telemetry/joystick/left_x')
            self.assertEqual(left.to_record_batch().column('Scalars:scalars').to_pylist()[0], [0.25])
            replay_path = Path(tmp)/'joystick-replay.rrd'
            replay = export_mcap(controller.recorder.output_path, replay_path, backend.basemap, backend.geometry.lake_geojson())
            catalog = {f['id']:f for f in replay['field_catalog']}
            self.assertIn('/telemetry/joystick/right_y', catalog)
            self.assertEqual(catalog['/telemetry/joystick/right_y']['stage'], 'input')
            self.assertEqual(catalog['/telemetry/joystick/right_y']['unit'], 'normalized')


if __name__ == '__main__':
    unittest.main()
