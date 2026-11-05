"""Timed input settings; no geographic coordinates or navigation."""

RAMP_UP_S = 10.0
RAMP_DOWN_S = 10.0
STRAIGHT_S = 10.0
TURN_S = 10.0
ZIGZAG_LEG_S = 10.0
SPIN_S = 10.0

# Normalized MANUAL_CONTROL inputs, not speed or angular-rate targets.
TURN_THROTTLE = 0.30
TURN_STEERING = (0.25, 0.50, 0.75)
ZIGZAG_THROTTLE = 0.30
ZIGZAG_STEERING = 0.25
SPIN_STEERING = 0.50
