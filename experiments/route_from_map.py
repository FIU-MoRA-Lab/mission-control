"""General path example using map waypoints, or explicit coordinates headlessly."""
import argparse
from backend.sdk import MissionControl, Plan, Leg, Constant, Waypoint


def example(mc, *, throttle=.2, route=None):
    points = mc.routes.get(route)
    if not points:
        points = [Waypoint(25.7581,-80.37365),Waypoint(25.758171945629098,-80.37365)]
    if len(points) < 2:
        raise ValueError("A path needs at least two waypoints")
    plan = Plan([Leg(a,b,Constant(throttle)) for a,b in zip(points,points[1:])])
    execution = mc.run("route",plan)
    execution.wait_until_finished()
    return execution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--throttle",type=float,default=.2)
    parser.add_argument("--route",help="A named draft saved through mc.routes.save()")
    parser.add_argument("--live",action="store_true")
    args = parser.parse_args()
    with MissionControl.connect(mode="live" if args.live else "simulation") as mc:
        if args.live: mc.read_parameters()
        execution = example(mc,throttle=args.throttle,route=args.route)
        print(f"Saved {execution.directory}")


if __name__ == "__main__": main()
