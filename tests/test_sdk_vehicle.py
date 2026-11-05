"""Explicit setup waits for confirmed vehicle state; parameter reads tolerate loss."""
import unittest
from unittest.mock import Mock, patch

from boats.heron.backend.control.adapter import PARAMETERS
from boats.heron.backend.sdk import MissionControl


class VehicleSdkTests(unittest.TestCase):
    def setUp(self):
        self.mc = MissionControl('http://127.0.0.1:1234', 'test-token', mode='live')
        self.addCleanup(self.mc.close)

    def test_actions_wait_for_confirmed_telemetry_not_optimistic_state(self):
        for method, field, value in (('set_manual','mode','MANUAL'), ('arm','armed',True), ('disarm','armed',False)):
            other = 'AUTO' if field == 'mode' else not value
            states = [{'connected':True,field:value,'vehicle_confirmed':{field:other}},
                      {'connected':True,'vehicle_confirmed':{field:value}}]
            with self.subTest(method=method), patch.object(self.mc,'_call') as command, \
                 patch.object(self.mc,'state',side_effect=states) as state, patch('time.sleep'):
                getattr(self.mc,method)()
                command.assert_called_once_with('vehicle_action',command=method)
                self.assertEqual(state.call_count,2)

    def test_simulation_setup_never_sends_vehicle_commands(self):
        self.mc.default_mode = 'simulation'
        with patch.object(self.mc,'_call') as command:
            self.mc.set_manual()
            self.mc.arm()
            self.mc.disarm()
        command.assert_not_called()

    def test_vehicle_action_keeps_sdk_request_envelope(self):
        response = Mock(ok=True)
        response.json.return_value = {'sent':True}
        with patch.object(self.mc.http,'post',return_value=response) as post, \
             patch.object(self.mc,'state',return_value={'connected':True,'vehicle_confirmed':{'armed':True}}):
            self.mc.arm()
        self.assertEqual(post.call_args.kwargs['json'],
                         {'action':'sdk','operation':'vehicle_action','command':'arm'})

    def test_rejection_link_loss_and_confirmation_timeout_propagate(self):
        with patch.object(self.mc,'_call',side_effect=RuntimeError('Kill switch')):
            with self.assertRaisesRegex(RuntimeError,'Kill switch'):
                self.mc.arm()
        with patch.object(self.mc,'_call'), patch.object(self.mc,'state',return_value={'connected':False}):
            with self.assertRaisesRegex(RuntimeError,'disconnected'):
                self.mc.arm()
        with patch.object(self.mc,'_call'), \
             patch.object(self.mc,'state',return_value={'connected':True,'vehicle_confirmed':{'armed':False}}), \
             patch('time.monotonic',side_effect=[0,2]):
            with self.assertRaisesRegex(TimeoutError,'confirm arm'):
                self.mc.arm(timeout_s=1)

    def test_lost_parameter_replies_are_retried_without_discarding_other_values(self):
        missing = PARAMETERS[:2]
        values = {key:0 for key in PARAMETERS if key not in missing}
        requests = []
        clock = [0]

        def call(operation, **data):
            self.assertEqual(operation,'parameters')
            if data.get('request'):
                key, = data['names']
                requests.append(key)
                # Lose the first reply for the first parameter.
                if key != missing[0] or requests.count(key) == 2:
                    values[key] = 1
            return dict(values)

        with patch.object(self.mc,'_call',side_effect=call), \
             patch('time.monotonic',side_effect=lambda:clock[0]), \
             patch('time.sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            result = self.mc.read_parameters(timeout=2)
        self.assertEqual(set(result),set(PARAMETERS))
        self.assertEqual(requests.count(missing[0]),2)
        self.assertEqual(requests.count(missing[1]),1)
        self.assertEqual(set(requests),set(missing))

    def test_parameter_timeout_names_missing_replies(self):
        key = 'SERVO1_FUNCTION'
        values = {name:0 for name in PARAMETERS if name != key}
        with patch.object(self.mc,'_call',return_value=values), \
             patch('time.monotonic',side_effect=[0,2]):
            with self.assertRaisesRegex(TimeoutError,key):
                self.mc.read_parameters(timeout=1)

    def test_armed_manual_orders_setup_and_disarms_on_success(self):
        with patch.object(self.mc,'set_manual') as manual, patch.object(self.mc,'arm') as arm, \
             patch.object(self.mc,'disarm') as disarm:
            with self.mc.armed_manual(timeout_s=3) as client:
                self.assertIs(client,self.mc)
                manual.assert_called_once_with(timeout_s=3)
                arm.assert_called_once_with(timeout_s=3)
                disarm.assert_not_called()
            disarm.assert_called_once_with(timeout_s=3)

    def test_armed_manual_preserves_body_error_and_disarms(self):
        failure = RuntimeError('motor test failed')
        with patch.object(self.mc,'set_manual'), patch.object(self.mc,'arm'), \
             patch.object(self.mc,'disarm') as disarm:
            with self.assertRaises(RuntimeError) as caught:
                with self.mc.armed_manual():
                    raise failure
            self.assertIs(caught.exception,failure)
            disarm.assert_called_once()

    def test_armed_manual_exposes_both_body_and_cleanup_failures(self):
        failure,cleanup = RuntimeError('motor test failed'), TimeoutError('disarm failed')
        with patch.object(self.mc,'set_manual'), patch.object(self.mc,'arm'), \
             patch.object(self.mc,'disarm',side_effect=cleanup):
            with self.assertRaises(ExceptionGroup) as caught:
                with self.mc.armed_manual():
                    raise failure
            self.assertEqual(caught.exception.exceptions,(failure,cleanup))

    def test_armed_manual_disarms_after_unconfirmed_arming(self):
        with patch.object(self.mc,'set_manual'), patch.object(self.mc,'arm',side_effect=TimeoutError('arm unconfirmed')), \
             patch.object(self.mc,'disarm') as disarm:
            with self.assertRaisesRegex(TimeoutError,'arm unconfirmed'):
                with self.mc.armed_manual():
                    self.fail('must not enter experiment')
            disarm.assert_called_once()


if __name__ == '__main__':
    unittest.main()
