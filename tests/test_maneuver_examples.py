"""Check smooth profiles and the actual lake corridor used by the examples."""
from contextlib import contextmanager
import math
import unittest
from backend.batch import discover
from backend.paths import ROOT
from backend.control.profile import InputProfile
from backend.mapping.constraints import OperatingArea, xy
from backend.mapping.lake import LakeSafetyGeometry
from experiments._common import HOME, END


class Client:
    def __init__(self): self.profiles = []; self.plans = []
    @contextmanager
    def control(self,**kwargs):
        assert kwargs['kind'] == 'thrusters'
        assert kwargs['start'] == HOME and kwargs['end'] == END
        assert kwargs['heading_deg'] == 90
        yield self
    def run(self,name,plan):
        self.plans.append(plan)
        return self
    def wait_until_finished(self): pass
    def play(self,frames):
        self.profiles.append(InputProfile(frames,("left","right")))


class ManeuverTests(unittest.TestCase):
    def test_all_nine_cases_define_continuous_bounded_profiles(self):
        entries = discover([ROOT/'experiments'])
        self.assertEqual(len(entries),9)
        for entry in entries:
            client = Client()
            entry.function(client)
            if client.plans:
                self.assertEqual(len(client.plans),1)
                self.assertEqual(client.plans[0].waypoints[0],HOME)
                self.assertEqual(client.plans[0].waypoints[-1],END)
                continue
            self.assertEqual(len(client.profiles),1)
            profile = client.profiles[0]
            self.assertEqual(profile.evaluate(0)[0],{'left':0,'right':0})
            self.assertEqual(profile.evaluate(profile.duration)[0],{'left':0,'right':0})
            for i in range(1001):
                values,_ = profile.evaluate(profile.duration*i/1000)
                self.assertTrue(all(-1 <= v <= 1 for v in values.values()))
            for t,_ in profile.frames[1:-1]:
                before,_ = profile.evaluate(t-1e-6)
                after,_ = profile.evaluate(t+1e-6)
                self.assertTrue(all(abs(before[k]-after[k]) < 1e-5 for k in before))

    def test_circles_and_spins_skip_east_but_other_cases_keep_their_finish(self):
        for entry in discover([ROOT/'experiments']):
            circle_or_spin = entry.path.name in {'03_constant_turns.py','05_spin.py'}
            self.assertEqual(entry.visit_end,not circle_or_spin)

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
