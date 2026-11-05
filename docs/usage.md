# Usage guide

Run commands from the repository root after following the [installation instructions](../README.md).

## Run the app

| Use | Command |
| --- | --- |
| Offline demo and waypoint simulation | `uv run mission-control --boat heron --demo` |
| Demo with a recording open | `uv run mission-control --boat heron --demo --recording assets/demo.mcap` |
| Connect to a vehicle | `uv run mission-control --boat heron --connection udpin:0.0.0.0:14550` |
| Connect and preload a route | `uv run mission-control --boat heron --connection udpin:0.0.0.0:14550 --mission-file route.geojson` |
| Show help | `uv run mission-control --boat heron --help` |

| Option | Meaning / default |
| --- | --- |
| `--demo` | Offline mode: no MAVLink socket, network setup, or desktop geolocation. Local joystick display/recording remains enabled. |
| `--connection ENDPOINT` | MAVLink endpoint; default `udpin:0.0.0.0:14550`. Here, `udpin` listens for UDP, `0.0.0.0` accepts packets on any local interface, and `14550` is the local port. Configure the vehicle/router to send telemetry to this computer on that port. |
| `--mission-file PATH` | Preload a JSON, CSV, or GeoJSON route for editing; starting the mission remains an explicit action. |
| `--recording PATH` | Open an MCAP or RRD replay at startup. Use with `--demo` for offline viewing. |
| `--fiu-root PATH` | Override the offline FIU basemap directory; default `assets/fiu-basemap`. |

In demo mode, enable **Edit route** in the FIU map, add waypoints, and select
**Start Mission** to simulate navigation. Close the app through its window.
For live use, configure the vehicle network first; inspect the network helper
with `uv run python -m boats.heron.backend.mavlink.network --dry-run`.
Do not share a MAVLink listening port between processes.

Python and Rust pin Rerun to 0.38.1; upgrade them together.

## Run the standalone logger

Requires only uv and the Python dependencies (`uv sync --locked`); no Cargo
build or native viewer is needed. In QGroundControl's **Application Settings →
MAVLink**, enable forwarding to `127.0.0.1:14560`, then run:

| Use | Command |
| --- | --- |
| Record QGC's forwarded telemetry | `uv run mission-recorder` |
| Choose an endpoint and output prefix | `uv run mission-recorder --connection udpin:127.0.0.1:14560 --output logs/test/session` |
| Record only MCAP for 30 seconds | `uv run mission-recorder --formats mcap --duration 30` |
| Include local joystick samples | `uv run mission-recorder --joystick` |
| Show help | `uv run mission-recorder --help` |

| Option | Meaning / default |
| --- | --- |
| `--connection ENDPOINT` | Dedicated forwarded/mirrored MAVLink feed; default `udpin:127.0.0.1:14560` listens on this computer's loopback interface, port `14560`. Use `udpin:0.0.0.0:14560` to receive from another machine. |
| `--output PREFIX` | File prefix, e.g. `logs/test/session` creates `session.mcap`, `session.jsonl`, and `session.rrd` in `logs/test/`. Default: a timestamped session under `logs/heron/` (unless the log directory is overridden). |
| `--formats LIST` | Comma-separated formats; default `mcap,jsonl,rrd`. |
| `--types LIST` | Record only these MAVLink message types, e.g. `HEARTBEAT,GLOBAL_POSITION_INT`; default: all types. |
| `--joystick` | Also record local Logitech joystick samples; sends no controls. |
| `--duration SECONDS` | Positive capture duration; default: run until Ctrl-C. |
| `--baud RATE` | Serial baud rate; default `57600`; unused for UDP. |

The logger sends no vehicle commands. Ctrl-C drains pending records and finalizes
files; `.part` suffixes indicate recordings still being written. Keep its
listening port/serial device separate from QGC and Mission Control.
QGC forwarding supplies vehicle messages; recording QGC's outgoing commands
requires a router/mirror feed containing that direction.

Received messages retain onboard time when available, with arrival time stored
separately. See [writer APIs, clocks and QGC setup](../boats/heron/backend/logger/README.md).

## Manual control and backend health

Only the supported Logitech joystick supplies driving input:

| Input | MANUAL_CONTROL | RC_CHANNELS_OVERRIDE |
| --- | --- | --- |
| Left-stick Y | `x` (throttle), −1000…1000 | `chan3_raw`, 1000…2000 µs |
| Right-stick X | `r` (steering), −1000…1000 | `chan1_raw`, 1000…2000 µs |

RC mapping follows the existing Heron configuration; the vehicle's actual
channel assignments must match. The Vehicle panel shows requested fields,
the last attempted output pair, and whether both packets were written to the
local transport. This does not acknowledge vehicle reception or motor movement.
Received messages remain `/mavlink/<type>` in MCAP; sent messages use
`/commands/<type>`, with `direction` recorded in MCAP and JSONL. Rerun records
sent raw fields separately, shown under **Sent** in Recordings & layers after packets are
transmitted. Input-source/request snapshots are also logged to Rerun.

A vehicle heartbeat older than three seconds marks the link stale, zeros the
requested axes, disables manual input, and blocks further nonzero sends. A
returning heartbeat restores link status; manual input needs to be enabled
again. Loss of connectivity cannot guarantee a delivered neutral packet, so
vehicle-side RC override timeouts and link-loss behavior still matter.
Backend health exposes heartbeat age, receive-error count, and recording
failures. A recording error no longer prevents received telemetry decoding or
interrupts the second outgoing control packet.

## Post-run analysis

A reusable marimo notebook is available at `notebooks/recording_analysis.py`.
Run `uv run --extra analysis marimo edit notebooks/recording_analysis.py`, then
load a local RRD to browse annotations, inspect typed samples, filter events,
plot and compare signals, and export Parquet. See `notebooks/README.md` for
clock semantics and examples.

## Data layers

New RRD recordings preserve topic hierarchies: received numeric MAVLink fields
use `/mavlink/MESSAGE/field`, transmitted fields use `/commands/MESSAGE/field`,
and acquired joystick fields use `/input/JOYSTICK/field`. Derived chart values
use `/telemetry`; record metadata uses `/records` followed by the topic hierarchy.
Legacy RRDs with escaped topics beneath `/sensors` remain readable with their
original layer paths. Existing recordings cannot recover previously omitted fields.
New recordings also store typed `value` components for every decoded field, with
MAVLink protocol types and array lengths, plus a `state` component distinguishing
null, NaN, infinities, and present values. Finite scalar signals remain available
for charts. MCAP/JSONL encode nonfinite numbers as `{"$nonfinite":"nan"}`,
`{"$nonfinite":"+inf"}`, or `{"$nonfinite":"-inf"}`; replay restores those values.

Open **Recordings & layers** in the right panel. The controls follow the recording
selected in Rerun, including files opened through its File menu or drag and drop.
Each field has a checkbox: toggling it immediately shows or hides its chart
and, when GPS-aligned samples exist, its geographic reading layer. You can
show several fields from different messages simultaneously. Search by field,
unit, or message name.

The **Chart preset** selector offers **Control chain**, **Throttle + servos**,
**Steering + servos**, **Thruster feedback**, **Vehicle & mission state**, and **Activity & errors**.
**Thruster feedback** groups reported motor RPM, ESC current and supply voltage,
battery current/voltage, and servo outputs 1/3. It includes observed `ESC_STATUS`,
ArduPilot `ESC_TELEMETRY_*`, and `RPM` fields when present in live or recorded data.
Different electrical units use separate charts. RPM is evidence of motor rotation;
current indicates load, voltage indicates power, and servo outputs remain commands.
Battery current includes other equipment, and motor/ESC channel assignments must
be verified. Missing feedback is not inferred from commands.
The separate **State changes** preset uses Rerun's `StateChange` archetype and
native `StateTimelineView` for categorical lanes: DISARMED/ARMED, operating
mode (including MANUAL/AUTO/HOLD), input state, and mission transitions.
Vehicle-message lanes and local GCS lanes are separate. Unknown values reset
the lane to a gap; repeated unchanged states do not create new transitions.
Control presets overlay requested input, sent/received MANUAL_CONTROL, sent/received
RC overrides, received RC inputs, and servo outputs 1/3. Requested and
MANUAL_CONTROL values use the app’s comparison mapping `1500 + value × 0.5`
so all overlay lines use PWM microseconds. This is a comparison scale; servo
values are output commands, not measured thrust, and left/right assignment
is unverified. Original fields remain available with **Individual fields**.

State presets show heartbeat-derived armed and MANUAL/AUTO/HOLD traces,
local controller mission/link flags, waypoint progress, and vehicle health.
The activity view includes state transitions, received STATUSTEXT, command
and mission acknowledgments, and boolean/text message data. The separate
warning/error view includes severity 0–4 STATUSTEXT, rejected acknowledgments,
unhealthy enabled sensor masks, and local error statuses. Boolean fields use native StateTimelineView lanes with TRUE/FALSE or contextual
labels such as ENABLED/PAUSED. Null boolean samples reset their lanes to gaps
and retain their typed null in the data table. Older RRD boolean components
and known boolean 0/1 diagnostics gain state lanes when loaded; their original
components and timelines remain intact. Selecting a preset changes views, not recording coverage.
Preset changes target the selected recording's identity, so they cannot fall
back to the startup recording. Live and replay keep separate preset selections. Recording-load completion
unlocks the field and preset controls directly; replay layout updates use a
separate timeout from vehicle commands. Older RRDs show only their
existing projections; MCAP replay derives these views from recorded messages.
Control-only MCAPs without GPS can open with charts and activity and no track.

Ground speed, MAVLink RC channel 3 (throttle), RC channel 1 (steering), and
servo outputs 1 and 3 (thrusters) are enabled initially. **Hide all** clears the
visible fields; **Essentials** restores those defaults. Control charts wait for
actual received packets; battery and other fields remain available to select. Groups organize the field list, not the charts.
Hover over a field to see its source, numeric type, and enum (when available).
Protocol units come from pymavlink's installed ArduPilotMega dialect;
normalized vehicle telemetry uses meters, seconds, degrees, volts, and percent.
Custom sensor units come from recording metadata when available.
The combined panel opens recordings and provides one searchable field picker
for live and replay data. Related fields sit together: requested throttle,
sent control fields, and received RC telemetry share the throttle group;
steering, other RC channels, and actuator outputs have their own groups.
Thruster outputs 1 and 3 are grouped together, with neutral channel labels
until left/right assignment is verified. Other servo outputs remain in their
own group, including zero-valued readings. Group controls show all available
fields or hide the group; requested input precedes sent and received fields.
Position, heading/course, speed, GPS quality, and battery groups likewise
include their related converted and raw values. Exact message.field names
and sent/received labels distinguish each source. Expected control fields
without observed samples show "waiting for data" and are disabled.
Search includes descriptions and tags; hover explains
the source, units, scaling, and overlapping readings.

## Workspaces

The fixed header shows **live vehicle** speed, heading, GPS coordinates/fix,
satellites, and battery across all tabs. Unavailable live telemetry displays a
dash. Connection/arming state, confirmed arm/disarm, mode, and manual input
enable stay above the right-panel tabs. Heron accents mark arming and the active
mode in amber, enabled manual input in green, and disarm/stop in muted red.

The live header and vehicle map use latitude/longitude from
`GLOBAL_POSITION_INT`, the autopilot's estimated position. GPS receiver,
AHRS, and home-position messages remain recorded but do not replace these
coordinates. The last estimated position is retained until another
`GLOBAL_POSITION_INT` arrives; demo mode uses simulated coordinates.

- **Vehicle:** joystick status, compact input values, collapsible control diagnostics, backend
  health, and activity.
- **FIU map:** geographic satellite imagery with an Edit route toggle, click to add a waypoint, drag to move,
  right-click to delete, middle drag to pan, scroll to zoom, coordinate
  entry, JSON/CSV/GeoJSON load, GeoJSON save, close loop, clear, and confirmed
  mission start. **Mission speed** sets the target ground speed in m/s
  (default 1.0, range 0.1–10.0). Set it while disarmed before starting;
  changing it invalidates an earlier upload. Start uploads the route with a
  MAVLink `DO_CHANGE_SPEED` command before the first waypoint. The speed is
  saved in route GeoJSON and also controls offline simulation movement.
  Actual ground speed remains visible in the vehicle header. This speed setting
  applies to AUTO missions; joystick driving still uses the full manual input range.
  Start uploads the route automatically, selects the beginning of the plan, and
  waits for vehicle telemetry confirming AUTO, arming, and an active waypoint.
  If confirmation does not arrive within eight seconds, the app reports an
  unconfirmed start; commands may already have reached the vehicle, so inspect
  its current state before retrying. Vehicle STATUSTEXT warnings (including
  failsafe and EKF messages) appear in activity and the status bar.
  The live connection sends a GCS heartbeat every second, including during AUTO
  and while joystick input is disabled. Lake padding and
  fountain exclusion outlines use the copied Heron geometry logic. Changing
  padding resumes following live data so the map displays the updated boundary,
  even if the timeline was paused or showing an earlier time.
- **Recordings & layers:** open MCAP or RRD files as a separate replay.
  GPS-aligned readings appear as colored spatial entities; all numeric message
  fields also appear in plots, including readings without aligned GPS.
  The field controls toggle charts and geographic readings together.
  MCAP receive-time alignment retains Heron's two-second maximum GPS gap.
  RRD loading preserves existing values and timelines, using the first data
  recording in the file. Scalar fields populate the same layer catalog.

The Rerun viewport includes offline FIU imagery, vehicle and
sensor plots, and activity logs. Use Rerun's recording selector to switch
between live data and replay; the Vehicle panel always displays current live
state. Open the Blueprint/Selection panels to select fields, hide streams,
change views, inspect values, and configure spatial history/time ranges.
Save recordings as RRD and layouts as blueprints through Rerun's File menu.

The FIU satellite map and route tools work without internet and cover the FIU lake.
FIU uses Rerun's geographic `MapView`: vehicle positions and sensor readings
are WGS84 latitude/longitude `GeoPoints`, with routes and lake outlines logged
as `GeoLineStrings`. The TIFF ModelTiepoint/ModelPixelScale/GeoKey tags locate
the satellite imagery; the companion GeoJSON metadata clips valid coverage.
The backend reprojects it into Web Mercator tiles for the native map. GPS logs
can therefore overlay the imagery directly without converting data to pixels.

`frontend/vendor/re_view_map/` is a small, pinned adaptation of Rerun 0.38.1 that
uses the local authenticated FIU tile server instead of online tile providers.
Rerun still provides geographic rendering, picking, pan/zoom, and timeline
queries. The default workspace contains only the FIU map. The waypoint editor
retains a stitched preview of the same georeferenced tiles. Coordinate entry
and mission import still accept routes outside the FIU imagery coverage.

## Input and shutdown

Enable joystick input, then use the Logitech HID adapter. Left-stick Y controls
throttle; right-stick X controls steering. Keyboard and UI/API axis commands are
disabled. **Stop vehicle** or Esc invokes Heron's emergency stop. Focus loss,
native file dialogs, confirmation dialogs, replay loading, and input disable
pause teleoperation.

The Joystick panel automatically learns small resting offsets on all four axes
after one second of stable reports near center (within 12% of the factory
center). Each stick learns independently. A dead zone filters remaining noise;
movement outside it scales smoothly to full travel. Automatic learning stays
near center because a held stick cannot reliably be distinguished from drift.
For larger offsets, select **Recalibrate sticks**, release both sticks, and hold
them still for one second. Manual calibration accepts offsets up to 30% of
travel and times out after ten seconds. It pauses driving; select **Enable
joystick** again after calibration finishes. Calibration lasts for the current
app session and is learned again on restart.

The UI shows each stick as a circle with crosshairs, a green filtered input dot,
and an outline showing the raw position.
Both X/Y axes are captured for each stick, even while driving is paused. HID
reports update a direct live feed; the circles read the newest sample independently
of HTTP state polling, Rerun playback, and recording. The HID reader consumes
reports as they arrive, and the native pipe feed refreshes at up to 120 Hz. Positions
are normalized to −1…1 (X right, Y up), before the driving deadzone/gain. Samples
are recorded by a separate worker at the control sampling cadence (20 Hz) in MCAP/JSONL as `/input/JOYSTICK`
and in RRD as `/telemetry/joystick/left_x`, `left_y`, `right_x`, and `right_y`.
The MCAP also retains the four original HID axis values, acquisition timestamps,
and input status. A full recording queue reports dropped samples without blocking
live input. Select
the joystick position fields in Recordings & layers to plot them live or in replay.

The UI only enables/pauses input and displays joystick state; Python owns HID
samples and the 20 Hz control loop. The native worker pauses input when UI frame
heartbeat timestamp stops updating for 250 ms. Frame heartbeats do not enter the
command queue, so rendering cannot queue stale heartbeats ahead of button actions. The Python backend retains the 400 ms joystick sample
expiry and 750 ms UI lease. Joystick loss sends neutral and disables input;
resuming requires enabling input again. The Rerun logging thread is an observer
and cannot renew that lease.

Closing the window pauses inputs and checks armed state. An armed vehicle asks
for confirmation. Shutdown rechecks arming, sends neutral output, closes the
transport, and flushes the MCAP log. It does not disarm the vehicle. Backend
stdin EOF triggers the same cleanup if the viewer process exits unexpectedly.

The command API uses an ephemeral loopback port and random bearer token. The
separate Rerun gRPC telemetry receiver also binds loopback. Viewing a replay
never sends recorded commands to the vehicle.

Each recording session creates one folder inside `logs/heron/`, with a matching
MCAP, RRD, and JSON Lines file:

```text
logs/heron/heron_waypoints_<timestamp>/
  heron_waypoints_<timestamp>.mcap
  heron_waypoints_<timestamp>.rrd
  heron_waypoints_<timestamp>.jsonl
```

The MCAP contains indexed, compressed MAVLink messages with JSON schemas;
JSONL contains the same decoded messages, one JSON object per line. The RRD
contains raw numeric streams, normalized vehicle telemetry, maps, and layouts.
The local RRD writer is independent of the viewer connection. While recording,
files use `.part` suffixes; normal quit, parent EOF, SIGINT, and SIGTERM flush
and finalize them before publishing their completed names. An abrupt kill or
power loss leaves explicitly unfinished `.part` files, rather than a file that
looks complete. MCAP/JSONL chunks are flushed and synced at least once per second
while messages are arriving. Existing recordings are left in their original
locations; this layout applies to new sessions.

RRD disk writes batch for up to one second or 4 MiB; viewer updates keep their
own shorter batching interval. On close, the RRD is flushed and published without
running compaction. The viewer allows up to five minutes for backend cleanup.
Derived status snapshots are logged only when they change, and track geometry
uses overlapping blocks of up to 128 points
instead of repeatedly saving the entire route history. Acquired message samples
are retained at their original rate.

Compaction is an explicit operation on completed recordings. It checks every
stored component and exact timeline value before a smaller temporary copy
atomically replaces the original. Optimization or verification failures leave
the original unchanged and report an error:

```sh
uv run python -m boats.heron.backend.logger.compact /path/to/recording.rrd
```

`MISSION_CONTROL_DATA_DIR` overrides the automatic recording
output root; `MISSION_CONTROL_LOG_DIR` overrides the log root for all formats
(legacy `HERON_LOG_DIR` also works). Automatic replay and filtered RRD exports
also get their own folders beneath this root. No separate `recordings/` or
`blueprints/` folder is generated. Basemap cache files use the OS cache directory
for `mission-control`; `MISSION_CONTROL_CACHE_DIR` overrides the cache location.
## Live camera

The top viewport row is split equally between the map and camera. Drag the
divider to resize either view. Camera frames are included in the live session's
RRD for timeline playback; they are not added to MCAP or JSONL telemetry files.

The Mac needs `ffmpeg` on its PATH. The boat needs the onboard
`mission-control-camera` systemd service. Install it once from your terminal:

```sh
uv run python -m boats.heron.backend.camera_service install
```

SSH prompts for the Pi's password if an accepted SSH key is not available.
No credentials are stored. The installer uses passwordless `sudo`, as provided
by the standard BlueOS Pi account; an SSH host alias with equivalent access can
be supplied with `--host`. After installation there is no SSH connection to keep
open. The service starts at boot, retries after encoder failures or an absent
USB camera, and writes diagnostics to the system journal.

The service runs GStreamer in its own container, reusing the installed
`blueos-core` image for its tools. It converts the raw USB camera to 640×480
H.264 at 30 fps (3 Mbit/s target bitrate) and serves Matroska over TCP port 5601. Only the selected camera
device is passed into that container. Stopping the service stops and removes
its container. This supports the raw-only webcam even on the boat's older
BlueOS camera manager, which rejected a raw RTSP stream.

```sh
uv run python -m boats.heron.backend.camera_service status
uv run python -m boats.heron.backend.camera_service logs
uv run python -m boats.heron.backend.camera_service restart
uv run python -m boats.heron.backend.camera_service stop
# Optional installation settings (defaults shown):
uv run python -m boats.heron.backend.camera_service install --host pi@192.168.2.2 --device /dev/video0 --container blueos-core --port 5601
# Use another supported capture rate if needed:
uv run python -m boats.heron.backend.camera_service install --fps 25
```

Configuration lives on the Pi at `/etc/mission-control/camera.json`; the unit is
`/etc/systemd/system/mission-control-camera.service`. Re-running the installer
updates configuration and restarts the service. A stable `/dev/v4l/by-id/...`
camera path can be supplied with `--device`. Capture defaults to 30 fps; use a
rate advertised by the camera. The app preserves incoming frame cadence and the
existing camera label shows the measured receive rate. After upgrading from the
10 fps service, re-run the installer and restart Mission Control to update both
capture and decoding. To disable startup at boot, run
`sudo systemctl disable --now mission-control-camera` on the Pi.

Stop any old `camera-stream.sh` terminal before installation so it releases
the camera and port. That temporary launcher has been removed. If another
camera service already owns the device, stop that stream through its own
manager before installing; this installer does not stop other camera services.

Mission Control connects to `tcp://192.168.2.2:5601` automatically outside demo
mode. The existing **Camera** controls connect/reconnect/disconnect independently
of the vehicle and also accept RTSP and HTTP video URLs. A lost signal replaces
the latest image with an offline message and retries automatically. Live camera
controls refer to the current camera; the viewport follows the selected recording
and timeline. Hover the camera status for decoder errors; use the service's
`logs` command for onboard capture errors.

Set `MISSION_CONTROL_CAMERA_URL` to override the initial source, or set
`MISSION_CONTROL_CAMERA_ENABLED=0` to disable automatic connection. Demo mode
starts with the camera off; **Connect** can still enable it explicitly.
Camera frames use GCS receipt time, not an onboard acquisition timestamp, and
the source clock is not assumed synchronized to vehicle telemetry.

On macOS, allow the app under **System Settings → Privacy & Security → Local
Network**. If Terminal connects but the app reports “No route to host,” check
this permission. Hover the camera status for decoder connection details.
