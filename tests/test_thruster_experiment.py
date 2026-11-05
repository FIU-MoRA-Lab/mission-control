"""Cover the public timed thruster API, including validation and cleanup."""
import unittest
from unittest.mock import MagicMock, patch

from boats.heron.backend.batch import discover
from boats.heron.paths import ROOT
from boats.heron.backend.sdk import MissionControl, Waypoint
from boats.heron.backend.sdk.client import Control


class ThrusterSdkTests(unittest.TestCase):
    def setUp(self):
        self.mc = MissionControl("http://127.0.0.1:1234", "test-token")
        self.addCleanup(self.mc.close)
        self.scope = MagicMock()
        self.acquire = patch.object(self.mc, "control", return_value=self.scope).start()
        self.addCleanup(patch.stopall)
        self.control = self.scope.__enter__.return_value

    def test_single_thruster_keeps_other_side_neutral(self):
        for side, percent, expected in (("left", 20, {"left": .2, "right": 0}),
                                      ("right", -30, {"left": 0, "right": -.3})):
            with self.subTest(side=side):
                self.mc.run_thruster(side, throttle_pct=percent, duration_s=3.5)
                self.control.pulse.assert_called_with(3.5, **expected)
                self.acquire.assert_called_with(mode=None, kind="thrusters", start=None)
                self.scope.__exit__.assert_called_with(None, None, None)

    def test_both_thrusters_have_explicit_independent_percentages(self):
        self.mc.run_thrusters(left_throttle_pct=-40, right_throttle_pct=70, duration_s=3.5)
        self.control.pulse.assert_called_once_with(3.5, left=-.4, right=.7)
        with self.assertRaises(TypeError):
            self.mc.run_thrusters(left_throttle_pct=20, duration_s=2)

    def test_invalid_side_never_acquires_control(self):
        for side in ("LEFT", "both", "", None):
            with self.subTest(side=side), self.assertRaisesRegex(ValueError, "side"):
                self.mc.run_thruster(side, throttle_pct=20, duration_s=2)
        self.acquire.assert_not_called()

    def test_invalid_inputs_never_acquire_control(self):
        for target in ({"left_throttle_pct": float("nan")}, {"right_throttle_pct": float("inf")}, {"left_throttle_pct": 101},
                       {"right_throttle_pct": -101}, {"left_throttle_pct": True}, {"right_throttle_pct": "0.2"},
                       {"duration_s": 0}, {"duration_s": -1}, {"duration_s": float("nan")},
                       {"duration_s": 86401}):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.mc.run_thrusters(**{"left_throttle_pct": 0, "right_throttle_pct": 0, "duration_s": 2, **target})
            self.acquire.assert_not_called()

    def test_no_gps_duration_is_validated_in_sdk(self):
        self.mc.testing_without_gps = True
        with self.assertRaisesRegex(ValueError, "less than 60"):
            self.mc.run_thruster("left", throttle_pct=20, duration_s=60)
        self.acquire.assert_not_called()

    def test_mode_and_simulation_start_are_forwarded(self):
        start = Waypoint(25.75830, -80.37380)
        self.mc.run_thruster("right", throttle_pct=20, duration_s=2, mode="live", start=start)
        self.acquire.assert_called_once_with(mode="live", kind="thrusters", start=start)

    def test_failure_exits_control_scope(self):
        self.control.pulse.side_effect = RuntimeError("test failure")
        self.scope.__exit__.return_value = False
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            self.mc.run_thruster("left", throttle_pct=20, duration_s=2)
        self.assertEqual(self.scope.__exit__.call_args.args[0], RuntimeError)

    def test_example_preserves_right_only_bench_settings(self):
        entry, = discover([ROOT / "boats/heron/experiments/testing"])
        mc = MagicMock()
        entry.function(mc)
        mc.test_thruster.assert_called_once_with("right", throttle_pct=99, duration_s=20)
        mc.run_thruster.assert_not_called()
        mc.armed_manual.assert_not_called()

    def test_sweep_failure_prevents_further_pulses(self):
        entry, = discover([ROOT / "boats/heron/experiments/testing"])
        mc = MagicMock()
        mc.test_thruster.side_effect = RuntimeError("motor test stopped")
        with self.assertRaisesRegex(RuntimeError, "motor test stopped"):
            entry.function(mc)
        mc.test_thruster.assert_called_once_with("right", throttle_pct=99, duration_s=20)

    def test_native_test_selects_one_motor_and_disarms_on_failure(self):
        self.mc.default_mode = "live"
        self.mc.testing_without_gps = True
        with patch.object(self.mc, "set_manual") as manual, patch.object(self.mc, "disarm") as disarm:
            self.control.pulse.side_effect = RuntimeError("test rejected")
            self.scope.__exit__.return_value = False
            with self.assertRaisesRegex(RuntimeError, "test rejected"):
                self.mc.test_thruster("right", throttle_pct=99, duration_s=20)
            manual.assert_called_once_with()
            self.assertEqual(disarm.call_count, 2)
            self.acquire.assert_called_once_with(kind="motor_test")
            self.control.pulse.assert_called_once_with(20, left=0, right=.99)

    def test_native_test_validation_happens_before_vehicle_actions(self):
        with patch.object(self.mc, "set_manual") as manual:
            for inputs in ({}, {"throttle_pct": 0}, {"throttle_pct": 1.5},
                           {"duration_s": 31}, {"duration_s": 0}, {"side": "both"}):
                with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                    self.mc.test_thruster(**{"side":"right", "throttle_pct":99, "duration_s":20, **inputs})
            manual.assert_not_called()



class PulseSdkTests(unittest.TestCase):
    def test_wait_polls_completion_without_refreshing_commands(self):
        client = MagicMock()
        client._call.return_value = {'profile_id':'pulse-1'}
        client.state.side_effect = [
            {'sdk':{'control':{'id':'control-1','status':'active','profile_id':'pulse-1','profile_complete':False}}},
            {'sdk':{'control':{'id':'control-1','status':'active','profile_id':'pulse-1','profile_complete':True}}},
        ]
        control = Control(client,{'id':'control-1','kind':'thrusters'})
        with patch('time.sleep'):
            control.pulse(20,left=.2,right=0)
        client._call.assert_called_once_with('pulse',id='control-1',duration_s=20,target={'left':.2,'right':0})

    def test_failure_reports_backend_reason_and_cancels_only_its_pulse(self):
        client = MagicMock()
        client._call.return_value = {'profile_id':'pulse-1'}
        client.state.return_value = {'sdk':{'control':{'id':'control-1','status':'failed','reason':'Vehicle heartbeat lost'}}}
        control = Control(client,{'id':'control-1','kind':'thrusters'})
        with self.assertRaisesRegex(RuntimeError,'Vehicle heartbeat lost'):
            control.pulse(20,left=.2,right=0)
        client._call.assert_called_with('control_stop',id='control-1',profile_id='pulse-1')


if __name__ == "__main__":
    unittest.main()
