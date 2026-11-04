"""Startup status must identify the boat's actual readiness blockers."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from backend.batch import live_blockers, wait_for_vehicle
from backend.controller.vehicle_control import VehicleController
from backend.mavlink.telemetry import VehicleTelemetry


class LiveReadinessTests(unittest.TestCase):
    def test_connected_manual_but_disarmed_and_no_gps_reports_all_blockers(self):
        state = dict(connected=True,mode='MANUAL',armed=False,fix=1,satellites=0,lat=0,lon=0)
        blockers = live_blockers(state)
        self.assertIn('vehicle disarmed',blockers)
        self.assertTrue(any('GPS fix unavailable' in b for b in blockers))
        self.assertIn('vehicle position unavailable',blockers)

    def test_wait_does_not_request_parameters_until_position_is_ready(self):
        state = dict(connected=True,mode='MANUAL',armed=True,fix=1,satellites=0,lat=0,lon=0)
        from unittest.mock import Mock
        mc = Mock()
        mc.state.return_value = state
        with patch('backend.batch.time.sleep'), patch('backend.batch.time.monotonic',side_effect=[0,0,2]),patch('builtins.print'):
            with self.assertRaisesRegex(TimeoutError,'GPS fix unavailable'):
                wait_for_vehicle(mc,1)
        mc.read_parameters.assert_not_called()

    def test_sent_arm_request_is_not_ready_until_heartbeat_confirms_it(self):
        from unittest.mock import Mock
        optimistic = dict(connected=True,mode='MANUAL',armed=True,fix=1,satellites=0,lat=0,lon=0,
                          vehicle_confirmed={'mode':'MANUAL','armed':False})
        confirmed = {**optimistic,'vehicle_confirmed':{'mode':'MANUAL','armed':True}}
        self.assertIn('waiting for autopilot to confirm arming',live_blockers(optimistic,no_gps=True))
        mc = Mock()
        mc.state.side_effect = [optimistic,confirmed]
        def verify(): self.assertEqual(mc.state.call_count,2)
        mc.read_parameters.side_effect = verify
        with patch('backend.batch.time.sleep'),patch('builtins.print'):
            wait_for_vehicle(mc,1,no_gps=True)
        mc.read_parameters.assert_not_called()

    def test_ready_vehicle_passes(self):
        state = dict(connected=True,mode='MANUAL',armed=True,fix=3,satellites=10,lat=25.7583,lon=-80.3738)
        self.assertEqual(live_blockers(state),[])

    def test_experiment_setup_can_start_before_manual_and_arming(self):
        from unittest.mock import Mock
        mc = Mock()
        mc.state.return_value = dict(connected=True,mode='HOLD',armed=False,fix=0,lat=0,lon=0,
                                    vehicle_confirmed={'mode':'HOLD','armed':False})
        with patch('builtins.print'):
            wait_for_vehicle(mc,1,no_gps=True,require_control=False)
        mc.read_parameters.assert_not_called()

    def test_companion_heartbeat_cannot_refresh_vehicle_link(self):
        with patch('backend.mavlink.transport.SessionRecorder'):
            c = VehicleController()
        c.connected = True
        c.autopilot_component = 1
        c._last_vehicle_heartbeat = 123.0
        message = SimpleNamespace(get_type=lambda:'HEARTBEAT',get_srcComponent=lambda:194,
                                  type=0,autopilot=3,base_mode=128,custom_mode=0)
        def recv(**kwargs):
            c.running = False
            return message
        c.master = SimpleNamespace(recv_match=recv,probably_vehicle_heartbeat=lambda m:True)
        VehicleTelemetry(c).run()
        self.assertEqual(c._last_vehicle_heartbeat,123.0)
        self.assertFalse(c.is_armed)


if __name__ == '__main__': unittest.main()
