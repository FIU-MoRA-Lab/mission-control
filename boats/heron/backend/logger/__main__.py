"""Passive standalone recorder for QGroundControl-forwarded or mirrored MAVLink."""
import argparse
import queue
import signal
import threading
import time
from pathlib import Path
from boats.heron.backend.mavlink.transport import MavlinkTransport
from boats.heron.backend.input.joystick import JoystickReader
from .session_recorder import SessionRecorder, WRITERS


def main():
    parser = argparse.ArgumentParser(description="Record a MAVLink feed to MCAP, JSONL and RRD without a UI or control commands.")
    parser.add_argument('--connection', default='udpin:127.0.0.1:14560', help='Dedicated forwarded/mirrored MAVLink endpoint; do not share QGC\'s listening port')
    parser.add_argument('--baud', type=int, default=57600)
    parser.add_argument('--output', type=Path, help='Session file prefix; all formats share the same directory/stem')
    parser.add_argument('--formats', default='mcap,jsonl,rrd', help='Comma-separated subset of mcap,jsonl,rrd')
    parser.add_argument('--types', help='Optional comma-separated MAVLink message filter')
    parser.add_argument('--joystick', action='store_true', help='Also record local Logitech joystick samples; never sends controls')
    parser.add_argument('--duration', type=float, help='Stop after this many seconds, useful for bounded captures')
    args = parser.parse_args()
    if args.duration is not None and args.duration <= 0: parser.error('--duration must be positive')
    formats = tuple(name.strip() for name in args.formats.split(','))
    if not formats or any(name not in WRITERS for name in formats):
        parser.error("--formats must contain mcap, jsonl and/or rrd")
    stopped = threading.Event()
    def stop(*_): stopped.set()
    for signum in (signal.SIGINT, signal.SIGTERM): signal.signal(signum, stop)
    recorder = SessionRecorder(args.output, formats=formats,
        msg_types=args.types.split(',') if args.types else None, session_label='standalone')
    transport = MavlinkTransport(args.connection, recorder=recorder, baud=args.baud)
    input_queue = queue.Queue(maxsize=1)
    joystick = JoystickReader(input_queue) if args.joystick else None
    started = time.monotonic()
    try:
        with recorder:
            transport.connect()
            if joystick: joystick.start()
            print(f'Recording {args.connection} to {recorder.output_path.parent}', flush=True)
            while not stopped.is_set() and (args.duration is None or time.monotonic()-started < args.duration):
                transport.recv_match(blocking=True, timeout=.02 if joystick else .1)
                if joystick:
                    try:
                        sample = input_queue.get_nowait()
                        throttle, steering, source = sample[:3]
                        positions = sample[3] if len(sample)>3 else dict.fromkeys(('left_x','left_y','right_x','right_y'),0.0)
                        data = dict(positions, source=source, connected=source.startswith('GAMEPAD'), control_source='external', error=joystick.error)
                        if len(sample)>4: data['raw_axes'] = sample[4]
                        recorder.log_input(data, timestamp_ns=sample[5] if len(sample)>5 else None)
                    except queue.Empty: pass
                if recorder.errors: raise RuntimeError('; '.join(recorder.errors[-3:]))
    finally:
        if joystick:
            joystick.running = False
            if joystick.is_alive(): joystick.join(timeout=1)
        transport.close()
        recorder.close()
    if recorder.errors: raise RuntimeError('; '.join(recorder.errors[-3:]))
    print(f'Saved {recorder.msg_count} records: ' + ', '.join(str(writer.output_path) for writer in recorder.writers.values()))


if __name__ == '__main__': main()
