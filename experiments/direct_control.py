"""General left/right control example; runnable alone or by the batch runner."""
import argparse
from backend.sdk import MissionControl, Waypoint


def example(mc, *, left=.2, right=-.2, seconds=3):
    points = mc.routes.get()
    start = points[0] if points else Waypoint(25.7581,-80.37365)
    with mc.control(kind="thrusters",start=start) as control:
        control.hold(seconds,left=left,right=right)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left",type=float,default=.2)
    parser.add_argument("--right",type=float,default=-.2)
    parser.add_argument("--seconds",type=float,default=3)
    parser.add_argument("--live",action="store_true")
    args = parser.parse_args()
    with MissionControl.connect(mode="live" if args.live else "simulation") as mc:
        if args.live: mc.read_parameters()
        with mc.record("direct-control",metadata=vars(args)) as recording:
            example(mc,left=args.left,right=args.right,seconds=args.seconds)
        print(f"Saved {recording.directory}")


if __name__ == "__main__": main()
