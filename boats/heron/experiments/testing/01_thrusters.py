"""Isolated right-thruster bench test at 99%, for 20 seconds.

Uses Rover's individual motor-test command, avoiding steering/throttle mixing.
Requires --mode live --no-gps. Each test starts and finishes disarmed.
"""


def run(mc):
    duration_s = 20
    for percent in range(99, 101, 60):
        for side in ["left", "right"]:
            print(f"{side} thruster: {percent}% for {duration_s} seconds", flush=True)
            mc.test_thruster(side, throttle_pct=percent, duration_s=duration_s)
