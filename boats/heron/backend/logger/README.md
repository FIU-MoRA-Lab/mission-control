# Standalone recording

MCAP/JSONL-only scripts do not import the Rerun SDK. The writers accept the same `Record` and have no dependency on vehicle control,
HID devices, map assets, the HTTP bridge, or a running viewer:

- `mcap_logger.py`: `McapLogger.open/write/flush/close`.
- `jsonl_logger.py`: `JsonlLogger.open/write/flush/close`.
- `rrd_logger.py`: `RrdLogger.open/write/flush/close`, using the headless Rerun SDK.
- `session_recorder.py`: selects writers and dispatches records on a background
  thread. Closing stops admission, drains pending records, and closes every writer.
- `records.py`: normalizes records and identifies the source clock.
- `mavlink_capture.py`: observes a transport's incoming/outgoing messages.
- `__main__.py`: passive recorder CLI, independent of our controller and UI.

```sh
uv run mission-recorder --connection udpin:127.0.0.1:14560
# Explicit file prefix; creates session.mcap, session.jsonl and session.rrd
uv run mission-recorder --connection udpin:127.0.0.1:14560 --output /tmp/capture/session
# Select writers; optionally capture local joystick positions without sending controls
uv run mission-recorder --connection udpin:127.0.0.1:14560 --formats mcap,jsonl,rrd --joystick
```

Ctrl-C and SIGTERM drain pending records and finalize all three files. During
recording they have `.part` suffixes. Completed names are published after close.
`--duration 30` stops after a bounded capture. No heartbeat, mode, arm/disarm,
manual-control command, network configuration, or message-rate request is sent.

RRDs use one-second / 4 MiB disk batching independently of the viewer. Close
flushes and publishes the recording without running compaction. Explicit
`flush()` still drains disk batches immediately; the recorder's periodic flush
leaves Rerun's own disk timer in control.

To compact a completed file, run
`uv run python -m boats.heron.backend.logger.compact recording.rrd`. A temporary compacted
copy replaces the original only if it is smaller and all stored values, types,
row identities and timelines match. Optimization or verification failures leave
the original unchanged and report an error.

## QGroundControl alongside the recorder

In QGroundControl's **Application Settings → MAVLink**, enable MAVLink forwarding
and set the destination to `127.0.0.1:14560`. Keep QGC's normal vehicle connection
and control setup. Start the command above on the separate forwarding port;
do not have QGC and the recorder bind the same listening port or serial device.

QGC's documented forwarding copies messages from connected vehicles. It does not
promise a copy of QGC's outgoing commands or raw local joystick axes. Capturing
both directions requires a router/mirror that forwards both vehicle and GCS
traffic to the recorder. `--joystick` independently reads the supported local
joystick; it neither sends controls nor establishes which inputs QGC uses.

[QGC forwarding documentation](https://docs.qgroundcontrol.com/Stable_V5.0/en/qgc-user-guide/settings_view/mavlink.html).

## Clock semantics

Every record preserves `data` and the following explicit envelope fields:

| Field | Meaning |
| --- | --- |
| `timestamp_ns`, `timestamp` | Source acquisition timestamp, in ns / seconds; null if absent |
| `clock` | `vehicle_boot`, `vehicle_unix`, `ground_station`, or `unavailable` |
| `timestamp_field` | Original onboard field used, e.g. `time_boot_ms` |
| `timestamp_iso` | ISO date only for Unix/GCS time; null for boot time/missing time |
| `onboard_timestamp_ns`, `onboard_clock` | Original onboard time retained even for locally sent messages |
| `receive_timestamp_ns`, `receive_timestamp_iso` | Ground-station arrival time, separate from source acquisition time |

Received MAVLink messages use `time_unix_usec`, `time_boot_ms`, `time_boot_us`,
or `time_usec` when present. Zero boot time is valid; zero Unix time in
`SYSTEM_TIME` means unavailable. MAVLink permits `time_usec` to represent boot
or Unix time; values >= 10^14 microseconds are interpreted as Unix time. The
original field/value remains in `data` so that interpretation is auditable.
Messages such as HEARTBEAT and STATUSTEXT have no onboard timestamp: their
primary timestamp stays **null**, never silently replaced by the GCS clock.
Local joystick input and locally transmitted commands use GCS time.

Boot clocks and GCS clocks are not assumed to be synchronized. This recorder
does not send TIMESYNC requests or infer an offset from message arrival. It
preserves out-of-order data and reboot clock resets as received.

MCAP's `log_time` uses arrival time for chronological indexing and existing GPS
arrival-time interpolation. `publish_time` uses source time where known. For
missing source time, MCAP requires `publish_time=log_time`; the payload still
explicitly says `timestamp_ns=null, clock=unavailable`. MCAP permits a
user-understood epoch, including boot time. Do not compare boot `publish_time`
to Unix/GCS `publish_time` without an explicit clock conversion.

RRD has separate timelines: `vehicle_boot_<system>_<component>` (duration),
`vehicle_time` (Unix), `ground_station_time` (Unix), and `receive_time` (arrival).
The `mission` timeline is elapsed arrival time for playback across mixed clocks;
it is not represented as onboard acquisition time. Selecting a vehicle timeline
shows only rows with that clock. Untimestamped messages have no vehicle timeline.
Vehicle scalar conversions are written from source records with their onboard
clock; periodic UI state snapshots do not invent vehicle acquisition times.

[MAVLink timestamp definition](https://mavlink.io/en/messages/common.html#GPS_RAW_INT),
[MCAP time fields](https://mcap.dev/spec#message-op0x05),
[clock synchronization protocol](https://mavlink.io/en/services/timesync.html).

## Python API without a UI

```python
from boats.heron.backend.logger.records import input_record
from boats.heron.backend.logger.session_recorder import SessionRecorder

with SessionRecorder('/tmp/capture/session') as recorder:
    recorder.write(input_record({'left_x': 0.25, 'left_y': 0.5,
                                 'right_x': 0.0, 'right_y': 0.0}))
```

An individual writer can be opened directly and given the same records. The
session recorder reports writer/observer errors in `errors`; one failed writer
does not stop the others or delay vehicle input. The bounded queue reports drops
rather than silently blocking the input/transport thread.
