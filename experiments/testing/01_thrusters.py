"""Lab throttle sweep: alternate left/right at 20–100%, 20 seconds per pulse.

Both thrusters return to neutral between pulses. No GPS or navigation is needed
when launched with --mode live --no-gps. Each pulse uses a fresh control scope.
"""


def run(mc):
    with mc.armed_manual():
        duration_s = 20
        for percent in range(20, 101, 20):
            for side in ("left", "right"):
                print(
                    f"{side} thruster: {percent}% for {duration_s} seconds", flush=True
                )
                mc.run_thruster(side, throttle_pct=percent, duration_s=duration_s)
