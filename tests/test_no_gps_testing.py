"""Exercise the explicit GPS bypass with a mocked vehicle, never real motors."""
import unittest
import time
from types import SimpleNamespace
from unittest.mock import patch
from boats.heron.backend.batch import live_blockers, discover
from boats.heron.paths import ROOT
from boats.heron.backend.controller.vehicle_control import VehicleController
from boats.heron.backend.controller.runtime import ControllerRuntime
from boats.heron.backend.control.adapter import Adapter, PARAMETERS
from boats.heron.backend.control.planning import Waypoint


class NoGpsTests(unittest.TestCase):
    def setUp(self):
        with patch('boats.heron.backend.mavlink.transport.SessionRecorder'):
            self.c = VehicleController()
        c = self.c
        c.connected = c._confirmed_armed = True
        c.mode_str = c._confirmed_mode = 'MANUAL'
        c._last_vehicle_heartbeat = time.monotonic()
        c.master = SimpleNamespace(target_system=1,target_component=1)
        self.runtime = ControllerRuntime(c,SimpleNamespace(last_error=None),inputs=False)
        self.runtime.testing_without_gps = True
        self.service = self.runtime.programmatic
        self.send = patch.object(c.transport,'send').start()

    def tearDown(self):
        with self.runtime._commands: self.service.takeover('Test finished')
        patch.stopall()

    def test_nogps_raw_controls_allow_zero_position_but_still_send_finite_inputs(self):
        info = self.service.acquire({'mode':'live','kind':'thrusters'})
        self.service.set_target({'id':info['id'],'target':{'left':.2,'right':-.2}})
        self.service.tick()
        self.send.assert_any_call('manual_control',1,0,200,0,0,0)
        self.assertEqual(self.service.owner['lat'],0)
        self.assertFalse(self.service.owner['geofence_enabled'])
        self.assertTrue(self.service.owner['gps_bypass'])

    def test_heartbeat_arming_mode_and_expiry_are_not_bypassed(self):
        self.c._confirmed_armed = False
        with self.assertRaises(ValueError): self.service.acquire({'mode':'live'})
        self.c._confirmed_armed = True
        info = self.service.acquire({'mode':'live'})
        self.service.set_target({'id':info['id'],'target':{'throttle':.2,'steering':0}})
        self.service.tick(time.monotonic()+1)
        self.assertFalse(self.service.busy)
        self.assertIn('expired',self.service.last['reason'])

    def test_navigation_and_long_profiles_are_rejected(self):
        for operation in ('navigate','follow','execute'):
            with self.assertRaisesRegex(ValueError,'disabled'):
                self.service.command(operation,{})
        with self.assertRaisesRegex(ValueError,'disabled'): self.runtime.command('start',{})
        info = self.service.acquire({'mode':'live','kind':'thrusters'})
        with self.assertRaisesRegex(ValueError,'60 seconds'):
            self.service.play({'id':info['id'],'keyframes':[(0,{'left':0,'right':0}),(61,{'left':1,'right':1})]})
        self.service.tick(self.service._started+61)
        self.assertIn('60 s',self.service.last['reason'])

    def test_timed_pulse_survives_client_refresh_gap_and_neutralizes_at_deadline(self):
        info = self.service.acquire({'mode':'live','kind':'thrusters'})
        self.service.pulse({'id':info['id'],'duration_s':20,'target':{'left':.2,'right':0}})
        started = self.service.profile_started
        for elapsed in (1,10,19.9):
            self.service.tick(started+elapsed)
            self.assertTrue(self.service.busy)
            self.assertFalse(self.service.owner['profile_complete'])
            self.send.assert_called_with('manual_control',1,0,100,100,0,0)
        self.service.tick(started+20.001)
        self.assertTrue(self.service.owner['profile_complete'])
        self.assertEqual((self.service.owner['left'],self.service.owner['right']),(0,0))
        self.send.assert_called_with('manual_control',1,0,0,0,0,0)

    def test_no_gps_pulse_limit_and_safe_cancellation(self):
        info = self.service.acquire({'mode':'live','kind':'thrusters'})
        with self.assertRaisesRegex(ValueError,'less than 60'):
            self.service.pulse({'id':info['id'],'duration_s':60,'target':{'left':.2,'right':0}})
        first = self.service.pulse({'id':info['id'],'duration_s':20,'target':{'left':.2,'right':0}})
        second = self.service.pulse({'id':info['id'],'duration_s':20,'target':{'left':0,'right':.2}})
        self.service.stop_movement({'id':info['id'],'profile_id':first['profile_id']})
        self.assertEqual(self.service.owner['profile_id'],second['profile_id'])
        self.service.stop_movement({'id':info['id'],'profile_id':second['profile_id']})
        self.assertIsNone(self.service.profile)
        self.send.assert_called_with('manual_control',1,0,0,0,0,0)

    def test_integral_heading_is_exposed_as_float(self):
        info = self.service.acquire({'mode':'live','kind':'thrusters'})
        self.c.heading = 255
        self.service.set_target({'id':info['id'],'target':{'left':0,'right':0}})
        self.service.tick()
        self.assertIsInstance(self.service.owner['heading'],float)

    def test_normal_mode_still_requires_position(self):
        self.runtime.testing_without_gps = False
        with self.assertRaisesRegex(ValueError,'GPS fix'):
            self.service.acquire({'mode':'live'})

    def test_readiness_bypass_only_removes_gps_blockers(self):
        state = dict(connected=True,mode='MANUAL',armed=True,fix=1,satellites=0,lat=0,lon=0)
        self.assertTrue(live_blockers(state))
        self.assertEqual(live_blockers(state,no_gps=True),[])
        state['armed'] = False
        self.assertEqual(live_blockers(state,no_gps=True),['vehicle disarmed'])

    def test_numbered_cases_do_not_substitute_raw_throttle_for_missing_speed_feedback(self):
        from contextlib import contextmanager
        class BenchClient:
            testing_without_gps = True
            @contextmanager
            def armed_manual(self): yield self
            def set_manual(self): pass
            def arm(self): pass
            def disarm(self): pass
            def navigate(self,*args,**kwargs): raise ValueError('Navigation requires GPS feedback')
            @contextmanager
            def control(self,**kwargs): yield self
            def move(self,**kwargs): raise ValueError('Movement requires feedback')
            def go_to(self,*args,**kwargs): raise ValueError('Movement requires feedback')
            def turn_to(self,**kwargs): raise ValueError('Movement requires feedback')
            def run(self,*args,**kwargs): raise ValueError('Path requires GPS feedback')
        for entry in discover([ROOT/'boats/heron/experiments/motion']):
            client = BenchClient()
            with self.assertRaisesRegex(ValueError, 'feedback'):
                entry.function(client)


if __name__ == '__main__': unittest.main()
