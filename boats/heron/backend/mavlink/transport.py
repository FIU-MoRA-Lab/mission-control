"""MAVLink connection and traffic observation; usable without control/UI code."""
import threading
from pymavlink import mavutil
from boats.heron.backend.logger.session_recorder import SessionRecorder
from boats.heron.backend.logger.mavlink_capture import MavlinkCapture

class MavlinkTransport:
    def __init__(self, connection_str, *, recorder=None, record=False, baud=57600):
        self.connection_str = connection_str
        self.baud = baud
        self.master = None
        self.send_lock = threading.RLock()
        self.recorder = recorder if recorder is not None else SessionRecorder(session_label="waypoints") if record else None
        self.capture = MavlinkCapture(self.recorder)
        self.thread = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None

    def bind_controller(self, controller):
        from .telemetry import VehicleTelemetry
        self.capture.on_error = lambda error: setattr(controller, "recording_error", error)
        self.thread = threading.Thread(target=VehicleTelemetry(controller).run, name="vehicle-telemetry", daemon=True)

    def start(self):
        if self.recorder: self.recorder.open()
        self.thread.start()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="gcs-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        # AUTO sends no manual input. It still needs a GCS heartbeat so the
        # autopilot can distinguish an idle operator from a lost ground station.
        while not self._heartbeat_stop.is_set():
            if self.master is not None:
                try:
                    self.send("heartbeat", mavutil.mavlink.MAV_TYPE_GCS,
                              mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0,
                              mavutil.mavlink.MAV_STATE_ACTIVE)
                except Exception:
                    # Connection setup/recovery belongs to the receive thread.
                    pass
            self._heartbeat_stop.wait(1.0)

    def connect(self):
        with self.send_lock:
            self.master = mavutil.mavlink_connection(self.connection_str, baud=self.baud)
            self.master.mav.set_send_callback(self.capture.sent)
            return self.master

    def send(self, message_type, *args):
        """Serialize writes and let the capture observer see the encoded packet."""
        with self.send_lock:
            if self.master is None:
                raise RuntimeError("Vehicle transport is not connected")
            return getattr(self.master.mav, f"{message_type}_send")(*args)

    def recv_match(self, **kwargs):
        message = self.master.recv_match(**kwargs)
        if message is not None: self.capture.received(message)
        return message

    def close(self):
        self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2)
        with self.send_lock:
            if self.master:
                self.master.close()
                self.master = None
