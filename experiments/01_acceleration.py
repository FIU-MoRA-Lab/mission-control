"""Position-based acceleration to the midpoint, deceleration to the east endpoint."""
from backend.sdk import Plan, Leg, Linear, Waypoint
from experiments._common import HOME, END, thrust, inputs


def run(mc):
    if getattr(mc,"testing_without_gps",False):
        with thrust(mc) as control:
            control.play([(0,inputs(0,0)),(5,inputs(1,1)),(10,inputs(0,0))])
        return
    midpoint = Waypoint(HOME.lat,(HOME.lon+END.lon)/2)
    plan = Plan([Leg(HOME,midpoint,Linear(0,1)),Leg(midpoint,END,Linear(1,0))],
                launch_throttle=.05,arrival_radius_m=1)
    mc.run("acceleration-to-east",plan).wait_until_finished()
