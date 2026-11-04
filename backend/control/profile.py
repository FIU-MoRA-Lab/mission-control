"""Finite continuous input profiles, evaluated using the backend's monotonic clock."""
from .planning import number


class InputProfile:
    def __init__(self, frames, keys):
        if not isinstance(frames,list) or not 2 <= len(frames) <= 1024:
            raise ValueError("A profile needs 2–1024 time/target keyframes")
        self.frames = []
        for at,target in frames:
            at = number(at,0,3600,"keyframe time")
            if set(target) != set(keys): raise ValueError("Profile axes do not match the control session")
            target = {key:number(target[key],-1,1,key) for key in keys}
            if self.frames and at <= self.frames[-1][0]: raise ValueError("Keyframe times must increase")
            self.frames.append((at,target))
        if self.frames[0][0] != 0: raise ValueError("The first keyframe must be at zero seconds")
        self.duration = self.frames[-1][0]

    def evaluate(self, elapsed):
        for (start,a),(end,b) in zip(self.frames,self.frames[1:]):
            if elapsed < end:
                u = max(0,min(1,(elapsed-start)/(end-start)))
                u = u*u*(3-2*u)  # Continuous value and first derivative at knots.
                return {key:a[key]+u*(b[key]-a[key]) for key in a},False
        return dict(self.frames[-1][1]),True


class InputPulse:
    """Backend-timed constant input that becomes neutral at the deadline."""

    def __init__(self, seconds, target, keys):
        self.duration = number(seconds,0,86400,"pulse duration")
        if self.duration == 0:
            raise ValueError("Pulse duration must be positive")
        if set(target) != set(keys):
            raise ValueError("Pulse axes do not match the control session")
        self.target = {key:number(target[key],-1,1,key) for key in keys}

    def evaluate(self, elapsed):
        complete = elapsed >= self.duration
        return ({key:0.0 for key in self.target} if complete else dict(self.target)),complete
