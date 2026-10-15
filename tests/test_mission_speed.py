"""Mission speed encoding, upload acknowledgment, persistence and simulation."""
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from pymavlink import mavutil
from backend.controller.vehicle_control import VehicleController


def message(kind, **fields):
    return SimpleNamespace(get_type=lambda: kind, **fields)


class MissionSpeedTests(unittest.TestCase):
    def setUp(self):
        with patch('backend.mavlink.transport.SessionRecorder'):
            self.controller = VehicleController()
        self.controller.add_waypoint(25.7581, -80.37365)
        self.controller.add_waypoint(25.7582, -80.37375)
        self.controller.set_mission_speed(1.7)

    def upload(self, request_kind='MISSION_REQUEST_INT', ack=0, omit_last=False):
        c = self.controller
        c.connected = True
        c.master = SimpleNamespace(target_system=1, target_component=1)
        sent = []

        def send(kind, *args):
            sent.append((kind, args))
            if kind == 'mission_clear_all':
                c._mission_req_queue.put(message('MISSION_ACK', type=0))
            elif kind == 'mission_count':
                count = args[2]
                for seq in range(count - int(omit_last)):
                    c._mission_req_queue.put(message(request_kind, seq=seq))
                c._mission_req_queue.put(message('MISSION_ACK', type=ack))

        with patch.object(c.transport, 'send', side_effect=send), patch('backend.controller.vehicle_control.time.sleep'):
            result = c.upload_mission()
        return result, sent

    def test_upload_includes_speed_before_all_waypoints_and_accepts_clear_ack(self):
        result, sent = self.upload()
        self.assertTrue(result)
        self.assertIn(('mission_count', (1, 1, 4)), sent)
        items = [args for kind, args in sent if kind == 'mission_item_int']
        self.assertEqual([args[2] for args in items], [0, 1, 2, 3])
        speed = items[1]
        self.assertEqual(speed[4], mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED)
        self.assertEqual(speed[7:11], (1, 1.7, -1, 0))
        self.assertEqual(items[2][11:13], (int(25.7581 * 1e7), int(-80.37365 * 1e7)))
        self.assertEqual(self.controller._mission_waypoint_offset, 1)

    def test_legacy_requests_use_float_coordinates(self):
        result, sent = self.upload('MISSION_REQUEST')
        self.assertTrue(result)
        items = [args for kind, args in sent if kind == 'mission_item']
        self.assertEqual(items[2][11:13], (25.7581, -80.37365))

    def test_rejection_or_incomplete_upload_does_not_mark_uploaded(self):
        for ack, omit in [(1, False), (0, True)]:
            with self.subTest(ack=ack, incomplete=omit):
                result, _ = self.upload(ack=ack, omit_last=omit)
                self.assertFalse(result)
                self.assertFalse(self.controller.mission_uploaded)

    def test_missing_final_ack_fails(self):
        c = self.controller
        c.connected = True
        c.master = SimpleNamespace(target_system=1, target_component=1)

        def send(kind, *args):
            if kind == 'mission_count':
                c._mission_req_queue.put(message('MISSION_REQUEST_INT', seq=0))

        # Reach the overall upload deadline after sending home, without waiting.
        with patch.object(c.transport, 'send', side_effect=send), \
             patch('backend.controller.vehicle_control.time.sleep'), \
             patch('backend.controller.vehicle_control.time.time', side_effect=[0, 0, 0, 16, 16]):
            self.assertFalse(c.upload_mission())
        self.assertFalse(c.mission_uploaded)

    def test_speed_validation_and_mission_lock(self):
        c = self.controller
        for invalid in [0, -1, 10.1, float('nan'), float('inf'), True, '2']:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                c.set_mission_speed(invalid)
        for flag in ['is_armed', 'mission_started', 'is_uploading_mission', 'is_starting_mission']:
            setattr(c, flag, True)
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                c.set_mission_speed(2)
            setattr(c, flag, False)
        c.mission_uploaded = True
        c.set_mission_speed(2)
        self.assertFalse(c.mission_uploaded)

    def test_saved_route_restores_speed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'route.geojson'
            self.assertTrue(self.controller.save_waypoints_file(path))
            self.controller.set_mission_speed(2)
            self.assertTrue(self.controller.load_waypoints_file(path))
            self.assertEqual(self.controller.mission_speed, 1.7)

    def test_simulation_distance_matches_selected_speed(self):
        c = self.controller
        c.lat, c.lon = 25.75, -80.37
        lat, lon = c.lat, c.lon
        c.simulating_mission = True

        def stop_after_tick(_):
            c.simulating_mission = False

        with patch('backend.controller.vehicle_control.time.sleep', side_effect=stop_after_tick):
            c._simulated_mission_loop([{'lat': lat + 0.001, 'lon': lon + 0.001}])
        distance = math.hypot((c.lat - lat) * 111320, (c.lon - lon) * 111320 * math.cos(math.radians(lat)))
        self.assertAlmostEqual(distance, 1.7 * 0.08, places=6)
        self.assertEqual(c.groundspeed, 1.7)
