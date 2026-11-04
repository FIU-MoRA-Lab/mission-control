"""Check movement-based examples and the actual lake corridor they use."""
from contextlib import contextmanager
from dataclasses import asdict
import unittest
from backend.batch import discover
from backend.paths import ROOT
from backend.control.profile import InputProfile
from backend.control.motion import Motion
from backend.control.navigation import RouteFollower
from backend.mapping.constraints import OperatingArea, xy
from backend.mapping.lake import LakeSafetyGeometry
from experiments._common import HOME, END


class Client:
    def __init__(self): self.commands = []; self.plans = []; self.navigation = []; self.events = []
    def navigate(self, destination, **kwargs):
        self.navigation.append((destination, kwargs))
        self.events.append("navigate")
    @contextmanager
    def control(self,**kwargs):
        assert kwargs.get('kind', 'axes') == 'axes'
        assert kwargs['start'] == HOME and kwargs['end'] == END
        assert kwargs['heading_deg'] == 90
        yield self
    def move(self, **kwargs): self.commands.append(("move", kwargs))
    def go_to(self, destination, **kwargs):
        self.commands.append(("go_to", {"destination":asdict(destination), **kwargs}))
    def turn_to(self, **kwargs): self.commands.append(("turn_to", kwargs))
    def run(self, name, plan):
        self.plans.append(plan)
        self.events.append("run")
        return self
    def wait_until_finished(self): self.events.append("wait_until_finished")


class ManeuverTests(unittest.TestCase):
    def test_all_ten_cases_use_thrust_paths_or_movement_goals(self):
        entries = discover([ROOT/'experiments'])
        self.assertEqual(len(entries),10)
        for entry in entries:
            client = Client()
            entry.function(client)
            if client.plans:
                self.assertEqual(len(client.plans), 1)
                self.assertEqual(client.plans[0].waypoints[0], HOME)
                self.assertEqual(client.plans[0].waypoints[-1], END)
                self.assertFalse(client.commands)
                continue
            self.assertEqual(len(client.commands), 4 if entry.path.stem == "04_zigzag" else 1)
            for operation, data in client.commands:
                motion = Motion(operation, data, 90)
                self.assertGreater(motion.duration, 0)
                self.assertLessEqual(motion.duration, 180)
                self.assertFalse({"left", "right", "throttle", "steering"} & data.keys())
                if operation == "go_to":
                    self.assertEqual(motion.destination, END)

    def test_acceleration_thrust_tracks_distance_and_peaks_at_midpoint(self):
        entries = discover([ROOT/'experiments'])
        client = Client()
        entries[0].function(client)
        plan = client.plans[0]
        self.assertEqual(plan.launch_throttle, .05)
        follower = RouteFollower(plan)
        for fraction, expected in ((0, 0), (.25, .5), (.49, .98), (.5, 1), (.75, .5), (1, 0)):
            point = type(HOME)(HOME.lat + fraction * (END.lat-HOME.lat),
                               HOME.lon + fraction * (END.lon-HOME.lon))
            result = follower.step(point, 90)
            self.assertAlmostEqual(result.requested_throttle, expected, places=5)
            if fraction == 0:
                self.assertEqual(result.throttle, .05)
            if fraction == .49:
                self.assertEqual(result.leg, 0)  # No arrival-radius shortcut before the peak.

    def test_straight_cases_keep_25_50_75_and_100_percent_thrust_over_the_route(self):
        entries = [entry for entry in discover([ROOT/'experiments']) if entry.path.stem == "02_straight_runs"]
        self.assertEqual([entry.name for entry in entries], ["straight-25", "straight-50", "straight-75", "straight-100"])
        for entry, thrust in zip(entries, (.25, .5, .75, 1)):
            client = Client()
            entry.function(client)
            plan = client.plans[0]
            self.assertEqual(len(plan.legs), 1)
            self.assertEqual(plan.launch_throttle, 0)
            for fraction in (0, .25, .5, .75, 1):
                self.assertEqual(plan.legs[0].throttle.evaluate(fraction), thrust)

    def test_zigzag_expresses_headings_without_timestamps(self):
        entries = discover([ROOT/'experiments'])
        zigzag = next(entry for entry in entries if entry.path.stem == "04_zigzag")
        client = Client()
        zigzag.function(client)
        self.assertEqual([data["heading_deg"] for _, data in client.commands], [70, 110, 70, 110])

    def test_each_experiment_explicitly_positions_and_returns_to_start(self):
        for entry in discover([ROOT/'experiments']):
            client = Client()
            entry.function(client)
            expected = [HOME, END, HOME] if entry.path.stem == '04_zigzag' else [HOME, HOME]
            self.assertEqual([point for point, _ in client.navigation], expected)
            self.assertEqual(client.navigation[-1][1], {'speed_mps':1, 'heading_deg':90})
            if client.plans:
                self.assertEqual(client.events, ['navigate', 'run', 'wait_until_finished', 'navigate'])

    def test_failed_experiment_does_not_start_a_return_movement(self):
        class FailingClient(Client):
            def run(self, *args, **kwargs): raise RuntimeError('experiment failed')
            def move(self, **kwargs): raise RuntimeError('experiment failed')
            def turn_to(self, **kwargs): raise RuntimeError('experiment failed')
        for entry in discover([ROOT/'experiments']):
            client = FailingClient()
            with self.assertRaisesRegex(RuntimeError, 'experiment failed'):
                entry.function(client)
            self.assertEqual([point for point, _ in client.navigation], [HOME])

    def test_home_and_corridor_inside_padding_and_north_of_every_fountain(self):
        area = OperatingArea(LakeSafetyGeometry().lake_geojson())
        self.assertIsNone(area.segment_issue(HOME,END))
        self.assertEqual(HOME.lat,END.lat)
        self.assertLess(HOME.lon,END.lon)
        for center in area.centers:
            for p in (HOME,END):
                # y increases northward in the shared metric frame.
                self.assertGreater(xy(p,area.origin)[1],center[1]+area.padding)

    def test_profile_interpolates_at_arbitrary_backend_tick_times(self):
        profile = InputProfile([(0,{'left':0,'right':0}),(1,{'left':1,'right':1})],('left','right'))
        values,complete = profile.evaluate(.123456)
        self.assertFalse(complete)
        u=.123456
        self.assertAlmostEqual(values['left'],u*u*(3-2*u))
        self.assertNotEqual(values['left'],profile.evaluate(.1)[0]['left'])
        with self.assertRaises(ValueError): InputProfile([(0,{'left':0}),(0,{'left':1})],('left',))


if __name__ == '__main__': unittest.main()
