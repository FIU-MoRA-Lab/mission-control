"""Verify recording isolation, input leases, offline projection, and RRD output."""
import json
import os
import subprocess
import sys
from urllib.request import Request, urlopen
import rerun as rr
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from mcap.writer import Writer

from boats.heron.backend.bridge.runtime import MissionBackend
from boats.heron.backend.controller.runtime import ControllerRuntime
from backend.mapping.tiles import RasterTiles
from boats.heron.backend.controller.vehicle_control import VehicleController
from boats.heron.backend.mapping.lake import LakeSafetyGeometry
from backend.mapping.basemap import build_basemap, local_xy
from boats.heron.visualization.replay import export_mcap

ROOT = Path(__file__).resolve().parents[1]

class RerunIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._fixture_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._fixture_directory.cleanup)
        cls.demo_recording = Path(cls._fixture_directory.name) / 'demo.mcap'
        with cls.demo_recording.open('wb') as handle:
            writer = Writer(handle)
            writer.start()
            writer.add_metadata('fixture', {'synthetic': 'true'})
            position = writer.register_channel('/mavlink/GLOBAL_POSITION_INT', 'json', 0)
            sensor = writer.register_channel('/sensors/water', 'json', 0)
            cls.demo_message_count = 0
            for second in range(3):
                stamp = (1_700_000_000 + second) * 1_000_000_000
                for channel, data in (
                    (position, {'lat': 257581000 + second * 100, 'lon': -803739000 + second * 100, 'alt': 1000}),
                    (sensor, {'temperature': 22.0 + second, 'conductivity': 400 + second * 10}),
                ):
                    writer.add_message(channel, stamp, json.dumps({'data': data}).encode(), stamp)
                    cls.demo_message_count += 1
            writer.finish()

    def test_live_catalog_is_reused_and_invalidated_when_fields_arrive(self):
        from boats.heron.visualization.schema import field_catalog
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MISSION_CONTROL_DATA_DIR': tmp}), patch('boats.heron.backend.mavlink.transport.SessionRecorder'):
            backend = MissionBackend(VehicleController(), RasterTiles(ROOT/'assets/fiu-basemap', tmp), None, demo=True)
            backend.gamepad = None
            with patch('boats.heron.backend.bridge.runtime.field_catalog', wraps=field_catalog) as build:
                first = backend.state(refresh_ui=False)['field_catalog']
                self.assertIs(first, backend.state(refresh_ui=False)['field_catalog'])
                self.assertEqual(build.call_count, 1)
                backend._sensor_fields['RC_CHANNELS'] = {'chan1_raw'}
                next_catalog = backend.state(refresh_ui=False)['field_catalog']
                self.assertIsNot(first, next_catalog)
                self.assertEqual(build.call_count, 2)
                channel = next(f for f in next_catalog if f['source'] == 'RC_CHANNELS.chan1_raw')
                self.assertTrue(channel['available'])
            backend.close()

    def test_observer_does_not_renew_manual_lease(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MISSION_CONTROL_DATA_DIR': tmp}), patch('boats.heron.backend.mavlink.transport.SessionRecorder'):
            controller = VehicleController()
            tiles = RasterTiles(ROOT/'assets/fiu-basemap', tmp)
            backend = ControllerRuntime(controller, tiles, inputs=False)
            backend.command('manual', {})
            backend._accept_gamepad_sample(900, 0, 'GAMEPAD')
            expired = time.monotonic()-2
            backend._last_ui = expired
            backend.state(refresh_ui=False)
            self.assertEqual(backend._last_ui, expired)
            backend.expire_inputs()
            self.assertFalse(backend._manual_active)
            self.assertEqual(backend.session.manual.throttle,0)
            tiles.close()

    def test_offline_basemap_and_recording_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            tiles=RasterTiles(ROOT/'assets/fiu-basemap', tmp)
            geometry=LakeSafetyGeometry()
            with patch('backend.mapping.tiles.requests.get', side_effect=AssertionError('Offline')):
                basemap=build_basemap(tiles,geometry,Path(tmp)/'fiu.png')
                for lat,lon in geometry._lake_outline:
                    x,y=local_xy(lon,lat,basemap)
                    self.assertTrue(0<=x<=basemap['width'])
                    self.assertTrue(0<=y<=basemap['height'])
                output=Path(tmp)/'replay.rrd'
                result=export_mcap(self.demo_recording,output,basemap,geometry.lake_geojson())
            self.assertTrue(result['synthetic'])
            self.assertGreater(result['streams'],0)
            self.assertGreater(output.stat().st_size,1000)
            self.assertNotIn('track',result)
            verification = subprocess.run([sys.executable,'-m','rerun','rrd','verify',str(output)],
                                          capture_output=True,text=True,timeout=15)
            self.assertEqual(verification.returncode,0,verification.stderr)
            tiles.close()

    def test_demo_starts_without_vehicle_socket_and_replay_pauses_input(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MISSION_CONTROL_DATA_DIR': tmp}), patch('boats.heron.backend.mavlink.transport.SessionRecorder'), patch('boats.heron.backend.controller.vehicle_control.VehicleController.start', side_effect=AssertionError('Live connection')):
            controller=VehicleController()
            backend=MissionBackend(controller,RasterTiles(ROOT/'assets/fiu-basemap',tmp),None,demo=True,cache=tmp)
            backend.start()
            try:
                self.assertFalse(controller.connected)
                self.assertIsNone(controller.master)
                self.assertIsNotNone(backend.gamepad)
                backend.command('manual',{})
                with self.assertRaises(ValueError):
                    backend.command('manual_axes',{'throttle':1000,'yaw':300})
                with patch('boats.heron.backend.bridge.runtime.export_mcap', wraps=export_mcap):
                    backend.command('load_mcap',{'path':str(self.demo_recording)})
                dataset = backend._recording['datasets'][0]
                field = dataset['fields'][0]
                selection = {'topic':dataset['topic'], 'field':field['name'],
                             'minimum':field['min'], 'maximum':field['max'],
                             'radius':4, 'opacity':.5, 'track_visible':False}
                with patch('boats.heron.backend.bridge.runtime.export_mcap', wraps=export_mcap) as export:
                    filtered = backend.command('filter_readings',selection)
                    self.assertGreater(filtered['matched'],0)
                    self.assertFalse(export.call_args.kwargs['include_raw'])
                    self.assertFalse(export.call_args.kwargs['track_visible'])
                with self.assertRaises(ValueError):
                    backend.command('filter_readings',dict(selection, minimum=field['max']+1))
                self.assertFalse(backend._manual_active)
                self.assertEqual(backend.session.manual.throttle,0)
                with self.assertRaises(ValueError): backend.command('mode',{'mode':'INVALID'})
                backend.command('mode',{'mode':'HOLD'})
                self.assertEqual(controller.mode_str,'HOLD')
            finally:
                backend.close()
            self.assertTrue(backend.live_recording.is_relative_to(Path(tmp) / "logs"))
            self.assertFalse((Path(tmp) / "blueprints").exists())
            verification = subprocess.run([sys.executable, "-m", "rerun", "rrd", "verify", str(backend.live_recording)], capture_output=True, text=True, timeout=15)
            self.assertEqual(verification.returncode, 0, verification.stderr)

    def test_managed_demo_bridge_can_stream_replay_and_exit_on_parent_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = rr.RecordingStream("test-receiver")
            import socket
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0))
                port = probe.getsockname()[1]
            url = sink.serve_grpc(grpc_port=port)
            env = dict(os.environ, MISSION_CONTROL_CACHE_DIR=tmp, MISSION_CONTROL_DATA_DIR=tmp, HERON_LOG_DIR=str(Path(tmp)/"logs"))
            diagnostics = Path(tmp)/'bridge.stderr'
            stderr = diagnostics.open('w')
            process = subprocess.Popen([sys.executable, '-u', '-m', 'boats.heron.backend.bridge',
                                        '--demo', '--rerun-url', url], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=stderr,
                                       text=True, env=env)
            try:
                import selectors
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(timeout=15), "Backend readiness timed out")
                ready = json.loads(process.stdout.readline())
                root = f"http://127.0.0.1:{ready['port']}"
                def request(path, data=None):
                    req = Request(root+path, data=json.dumps(data).encode() if data is not None else None,
                                  headers={'Authorization':f"Bearer {ready['token']}", 'Content-Type':'application/json'})
                    try:
                        with urlopen(req, timeout=20) as response: return json.load(response)
                    except Exception as exc:
                        self.fail(f"{exc}\n{diagnostics.read_text()}")
                self.assertTrue(request('/state')['demo'])
                replay = request('/command', {'action':'load_mcap','path':str(self.demo_recording)})
                self.assertEqual(replay['messageCount'], self.demo_message_count)
                self.assertGreater(len(replay['datasets']),0)
                dataset = replay['datasets'][0]
                field = dataset['fields'][0]
                filtered = request('/command', {'action':'filter_readings','topic':dataset['topic'],
                                               'field':field['name'], 'minimum':field['min'],
                                               'maximum':field['max'], 'track_visible':False})
                self.assertGreater(filtered['matched'],0)
                process.stdin.close()
                process.wait(timeout=8)
                self.assertEqual(process.returncode,0,diagnostics.read_text())
                outputs = list((Path(tmp) / "logs").rglob("*.rrd"))
                self.assertEqual(len(outputs), 3, outputs)
                for output in outputs:
                    verification = subprocess.run([sys.executable, "-m", "rerun", "rrd", "verify", str(output)], capture_output=True, text=True, timeout=15)
                    self.assertEqual(verification.returncode, 0, verification.stderr)

            finally:
                if process.poll() is None: process.kill(); process.wait()
                process.stdout.close(); process.stdin.close(); stderr.close()
                sink.disconnect()
