"""Observe transport traffic; capture never issues vehicle commands."""
import time

class MavlinkCapture:
    def __init__(self, recorder, on_error=None):
        self.recorder = recorder
        self.on_error = on_error

    def received(self, message):
        return self._capture(message, "rx")

    def sent(self, message, *args, **kwargs):
        return self._capture(message, "tx")

    def _capture(self, message, direction):
        if not self.recorder or message is None: return
        stamp = time.time_ns()
        try:
            return self.recorder.log_message(message, direction=direction, receive_timestamp_ns=stamp)
        except Exception as exc:
            if isinstance(self.recorder.errors, list): self.recorder.errors.append(f"capture: {exc}")
            if self.on_error: self.on_error(str(exc))
            # A recorder failure cannot prevent decoding or sending the next packet.
