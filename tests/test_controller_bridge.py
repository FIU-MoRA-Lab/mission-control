"""Integration checks for the GUI boundary without a vehicle or GUI toolkit."""
import io
import json
import math
import os
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
from backend.controller.runtime import ControllerRuntime
from backend.bridge.http_server import make_server
from backend.mapping.tiles import RasterTiles
from backend.controller.vehicle_control import VehicleController

REPO = Path(__file__).resolve().parents[1]


class ControllerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch('backend.mavlink.transport.SessionRecorder'):
            self.controller = VehicleController()
        self.tiles = RasterTiles(REPO / 'assets/fiu-basemap', Path(self.temp.name) / 'cache')
        self.backend = ControllerRuntime(self.controller, self.tiles, inputs=False)
        self.server = make_server(self.backend, 'test-token')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.root = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.controller.running = False
        self.controller.simulating_mission = False
        self.tiles.close()
        self.temp.cleanup()

    def request(self, path='/state', data=None, token='test-token'):
        request = Request(self.root + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=3) as response:
                return json.load(response)
        except HTTPError as error:
            error.close()
            raise

    def command(self, action, **data):
        return self.request('/command', {'action': action, **data})

    def joystick(self, throttle, yaw):
        with self.backend._commands:
            self.backend._accept_gamepad_sample(throttle, yaw, "GAMEPAD")

    def test_keyboard_and_button_axes_are_rejected(self):
        self.command('manual')
        self.joystick(500, 100)
        for source in ['KEYBOARD', 'BUTTON PAD', 'GAMEPAD']:
            with self.assertRaises(HTTPError) as error:
                self.command('manual_axes', throttle=1000, yaw=1000, source=source)
            self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.request()['manual']['throttle'], 500)

    def test_centered_joystick_and_disconnect_clear_input(self):
        self.command('manual')
        self.joystick(800, -300)
        self.assertTrue(self.request()['manual']['joystick_connected'])
        self.joystick(0, 0)
        self.assertEqual(self.backend.session.manual.throttle, 0)
        self.assertEqual(self.backend.session.manual.neutral_ticks, 5)
        self.joystick(800, -300)
        self.backend._accept_gamepad_sample(0, 0, 'NO JOYSTICK')
        state = self.request()['manual']
        self.assertFalse(state['joystick_connected'])
        self.assertFalse(state['active'])
        self.assertEqual((state['throttle'], state['yaw']), (0, 0))
        self.joystick(800, -300)
        self.assertEqual(self.backend.session.manual.throttle, 0)

    def test_api_requires_token_and_does_not_accept_query_auth_for_commands(self):
        for path in ['/state', '/state?token=test-token', '/tiles/fiu/19/145093/223298.png']:
            with self.assertRaises(HTTPError) as error:
                self.request(path, token='wrong')
            self.assertEqual(error.exception.code, 401)
        self.assertEqual(self.request()['waypoints'], [])

    def test_recording_delivery_errors_return_json_and_server_remains_available(self):
        for failure, status in [(TimeoutError("Recording delivery timed out"), 504),
                                (RuntimeError("gRPC connection severed"), 503)]:
            with patch.object(self.backend, "command", side_effect=failure):
                request = Request(self.root + '/command',
                                  data=json.dumps({'action': 'load_recording'}).encode(),
                                  headers={'Authorization': 'Bearer test-token', 'Content-Type': 'application/json'})
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=3)
                with error.exception as response:
                    self.assertEqual(response.code, status)
                    self.assertEqual(json.load(response), {'error': str(failure)})
            self.assertEqual(self.request()['waypoints'], [])

    def test_unexpected_request_errors_return_details_and_polling_recovers(self):
        for method, path, data in [("state", "/state", None),
                                   ("command", "/command", {"action": "start"})]:
            with patch.object(self.backend, method, side_effect=AttributeError("missing mission state")), \
                 self.assertLogs("backend.bridge.http_server", level="ERROR"):
                request = Request(self.root + path,
                                  data=json.dumps(data).encode() if data is not None else None,
                                  headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=3)
                with error.exception as response:
                    self.assertEqual(response.code, 500)
                    self.assertIn("AttributeError: missing mission state", json.load(response)["error"])
            self.assertEqual(self.request()["waypoints"], [])

    def test_malformed_commands_do_not_mutate_state(self):
        for fields in [{'lat': math.nan, 'lon': 0}, {'lat': 90, 'lon': 0},
                       {'lat': 25, 'lon': True}, {'lat': '25', 'lon': -80}]:
            with self.assertRaises(HTTPError) as error:
                self.command('add_waypoint', **fields)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError):
            self.request('/command', ['not', 'an', 'object'])
        self.assertEqual(self.request()['waypoints'], [])

    def test_route_edit_export_reload_and_padding_lock(self):
        for lat, lon in [(25.758, -80.374), (25.7581, -80.374), (25.7581, -80.3738)]:
            self.command('add_waypoint', lat=lat, lon=lon)
        self.assertFalse(self.request()['padding_enabled'])
        with self.assertRaises(HTTPError):
            self.command('padding', meters=5)
        self.command('close_loop')
        self.command('move_waypoint', index=1, lat=25.7582, lon=-80.374)
        path = str(Path(self.temp.name) / 'route.geojson')
        self.command('save', path=path)
        self.command('clear')
        self.command('load', path=path)
        state = self.request()
        self.assertEqual(len(state['waypoints']), 4)
        self.assertEqual(state['waypoints'][1]['lat'], 25.7582)
        self.assertEqual(state['waypoints'][0]['lat'], state['waypoints'][-1]['lat'])
        self.command('remove_waypoint', index=1)
        self.assertEqual([wp['id'] for wp in self.request()['waypoints']], [1, 2, 3])
        self.command('arm')
        self.command('clear')
        self.assertEqual(len(self.request()['waypoints']), 3)

    def test_gui_timeout_neutralizes_manual_control(self):
        self.command('manual')
        self.joystick( throttle=1000, yaw=-250)
        self.assertEqual(self.request()['manual']['throttle'], 1000)
        self.backend.expire_inputs(time.monotonic() + 1)
        self.assertEqual(self.backend.session.manual.throttle, 0)
        self.assertFalse(self.backend._manual_active)
        self.joystick( throttle=1000, yaw=0)
        self.assertEqual(self.backend.session.manual.throttle, 0)

    def test_quit_preparation_keeps_connection_alive_and_can_be_cancelled(self):
        self.command('manual')
        self.joystick( throttle=700, yaw=200)
        self.controller.is_armed = True
        self.assertEqual(self.request('/prepare-quit', {}), {'armed': True})
        self.assertTrue(self.controller.running)
        self.assertFalse(self.backend._closing)
        self.assertEqual(self.backend.session.manual.throttle, 0)
        self.assertFalse(self.backend._manual_active)
        # Cancelling the native dialog does not close the bridge or controller.
        self.command('manual_active', active=True)
        self.joystick( throttle=200, yaw=0)
        self.assertEqual(self.request()['manual']['throttle'], 200)

    def test_shutdown_requires_explicit_confirmation_and_rechecks_arming(self):
        self.assertEqual(self.request('/prepare-quit', {}), {'armed': False})
        self.controller.is_armed = True
        for fields in [{}, {'confirmed_armed': 'true'}, {'confirmed_armed': False}]:
            with self.assertRaises(HTTPError) as error:
                self.request('/shutdown', fields)
            self.assertEqual(error.exception.code, 409)
            self.assertTrue(self.controller.running)
        self.assertEqual(self.request('/shutdown', {'confirmed_armed': True}), {'ok': True})
        self.assertFalse(self.controller.running)
        self.assertTrue(self.controller.is_armed)
        with self.assertRaises(ValueError):
            self.backend.command('arm', {})

    def test_pending_disarm_still_requires_confirmation(self):
        self.controller._confirmed_armed = True
        self.controller.is_armed = False
        self.assertEqual(self.backend.prepare_quit(), {'armed': True})
        self.assertFalse(self.backend.request_shutdown())

    def test_close_sends_neutral_before_transport_close_and_is_idempotent(self):
        from unittest.mock import Mock
        events = []
        self.controller.master = Mock()
        self.controller.master.close.side_effect = lambda: events.append('transport')
        with patch.object(self.controller, 'send_manual_control',
                          side_effect=lambda throttle, yaw: events.append((throttle, yaw))):
            self.backend.close()
            self.backend.close()
        self.assertEqual(events, [(0, 0)] * 5 + ['transport'])
        self.controller.recorder.close.assert_called_once()

    def test_invalid_import_preserves_existing_route(self):
        self.command('add_waypoint', lat=25.758, lon=-80.374)
        path = Path(self.temp.name) / 'bad.json'
        path.write_text(json.dumps([{'lat': float('nan'), 'lon': -80.374}]))
        with self.assertRaises(HTTPError):
            self.command('load', path=str(path))
        self.assertEqual(len(self.request()['waypoints']), 1)

    def test_upload_locks_route_mutations_at_api_boundary(self):
        self.command('add_waypoint', lat=25.758, lon=-80.374)
        self.controller.is_uploading_mission = True
        self.assertFalse(self.request()['clear_available'])
        for action, data in [('clear', {}), ('close_loop', {}),
                             ('move_waypoint', {'index': 0, 'lat': 25.759, 'lon': -80.374})]:
            with self.assertRaises(HTTPError):
                self.command(action, **data)
        self.assertEqual(self.request()['waypoints'][0]['lat'], 25.758)

    def test_stale_joystick_input_expires_even_while_gui_polls(self):
        self.command('manual')
        self.joystick( throttle=700, yaw=0)
        now = time.monotonic()
        self.backend._last_ui = now + 0.5
        self.backend.expire_inputs(now + 0.5)
        self.assertEqual(self.backend.session.manual.throttle, 0)
        self.assertFalse(self.backend._manual_active)

    def test_switching_away_from_manual_and_stop_clear_axes(self):
        self.command('manual')
        self.joystick( throttle=1000, yaw=1000)
        self.command('manual_active', active=False)
        self.assertEqual(self.backend.session.manual.yaw, 0)
        self.command('manual')
        self.joystick( throttle=1000, yaw=0)
        self.command('stop')
        self.assertFalse(self.backend._manual_active)
        self.assertEqual(self.backend.session.manual.throttle, 0)

    def test_lake_geometry_export_contains_closed_safety_polygons(self):
        self.command('padding', meters=3.5)
        path = Path(self.temp.name) / 'lake.geojson'
        self.command('export_lake', path=str(path))
        lake = json.loads(path.read_text())
        self.assertEqual(lake['padding_m'], 3.5)
        roles = [feature['properties']['role'] for feature in lake['features']]
        self.assertEqual(roles.count('fountain_safety_area'), 3)
        self.assertIn('safe_area', roles)
        for feature in lake['features']:
            if feature['geometry']['type'] == 'Polygon':
                ring = feature['geometry']['coordinates'][0]
                self.assertEqual(ring[0], ring[-1])

    def test_fiu_tiles_render_offline_and_cache_works_without_esri(self):
        from backend.mapping.projection import latlon_to_tile
        x, y = latlon_to_tile(25.7581, -80.37365, 19)
        with patch('backend.mapping.tiles.requests.get', side_effect=AssertionError('Offline')):
            url = f'{self.root}/tiles/fiu/19/{int(x)}/{int(y)}.png?token=test-token'
            with urlopen(url) as response:
                tile = Image.open(io.BytesIO(response.read())).convert('RGBA')
            self.assertEqual(tile.size, (256, 256))
            self.assertGreater(tile.getextrema()[3][1], 0)
        cached = self.tiles.cache_dir / 'esri_imagery_z1_x0_y0.jpg'
        Image.new('RGB', (256, 256), 'blue').save(cached)
        with patch('backend.mapping.tiles.requests.get', side_effect=AssertionError('Cached tile should not download')):
            self.assertTrue(self.tiles.png('esri', 1, 0, 0).startswith(b'\x89PNG'))

    def test_offline_mission_upload_and_start_simulation(self):
        self.command('add_waypoint', lat=25.7581, lon=-80.37365)
        self.command('start')
        deadline = time.monotonic() + 2
        while not self.controller.mission_started and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.controller.mission_uploaded)
        self.assertTrue(self.controller.mission_started)
        self.assertTrue(self.controller.is_armed)
        self.assertEqual(self.controller.mode_str, 'AUTO')

    def test_mission_speed_command_updates_state_and_invalidates_upload(self):
        self.controller.mission_uploaded = True
        self.command('mission_speed', meters_per_second=1.5)
        state = self.request()
        self.assertEqual(state['mission_speed'], 1.5)
        self.assertFalse(state['uploaded'])
        self.controller.is_uploading_mission = True
        with self.assertRaises(HTTPError):
            self.command('mission_speed', meters_per_second=2)
        self.assertEqual(self.request()['mission_speed'], 1.5)


@unittest.skipIf(sys.platform == 'win32', 'Pipe readiness uses Unix selectors')
class BackendProcessTests(unittest.TestCase):
    def test_managed_parent_eof_stops_python_and_releases_udp_socket(self):
        import socket
        with tempfile.TemporaryDirectory() as temp:
            # Reserve an unused UDP port without touching the vehicle port.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.bind(('127.0.0.1', 0))
                port = udp.getsockname()[1]
            environment = {**os.environ, 'PYTHONPATH': str(REPO),
                           'HERON_CACHE_DIR': str(Path(temp) / 'tiles'),
                           'HERON_LOG_DIR': str(Path(temp) / 'logs')}
            process = subprocess.Popen([sys.executable, '-u', '-m', 'backend.bridge.standalone',
                                        '--managed', '--no-ip-setup', '--connection',
                                        f'udpin:127.0.0.1:{port}'], env=environment,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(timeout=10), 'Backend readiness timed out')
                info = json.loads(process.stdout.readline())
                self.assertGreater(info['port'], 0)
                self.assertGreater(len(info['token']), 20)
                request = Request(f"http://127.0.0.1:{info['port']}/state",
                                  headers={'Authorization': f"Bearer {info['token']}"})
                with urlopen(request, timeout=2) as response:
                    self.assertFalse(json.load(response)['connected'])
                process.stdin.close()
                process.wait(timeout=7)
                self.assertEqual(process.returncode, 0, process.stderr.read())
                from mcap.reader import make_reader
                logs = list((Path(temp) / 'logs').rglob('*.mcap'))
                self.assertEqual(len(logs), 1)
                with logs[0].open('rb') as stream:
                    reader = make_reader(stream, validate_crcs=True)
                    messages = list(reader.iter_messages())
                    # Idle GCS heartbeats and local input status are recorded
                    # without a vehicle; no drive commands should be emitted.
                    self.assertTrue(all(channel.topic in {'/input/JOYSTICK', '/commands/HEARTBEAT'}
                                        for _, channel, _ in messages))
                    self.assertEqual(reader.get_summary().statistics.message_count, len(messages))
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    udp.bind(('127.0.0.1', port))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
