"""Convert MCAP recordings and filtered reading layers into Rerun recordings."""
import json
from pathlib import Path
import rerun as rr
from mcap.reader import make_reader
from backend.recordings.mcap_reader import load_recording
from backend.rerun.live import RerunTelemetry
from backend.rerun.blueprint import blueprint
from backend.mavlink.telemetry_values import telemetry_fields
from backend.rerun.schema import field_catalog, default_fields
from backend.rerun.paths import field_path, geographic_path
from backend.logger.typed_fields import log_decoded_fields
from backend.logger.records import restore_nonfinite
from backend.rerun.diagnostics import RecordDiagnostics, chart_fields, catalog_types, diagnostic_catalog, BOOLEAN_PATHS
from backend.rerun.presets import chart_presets

def export_mcap(filename, destination, basemap, lake, *, url=None, recording=None,
                include_raw=True, radius=3.0, opacity=1.0, track_visible=True):
    """Convert in columns, retaining Heron's GPS alignment and raw scalar streams."""
    from collections import defaultdict
    recording = recording if recording is not None else load_recording(filename)
    stream = rr.RecordingStream("mission-control-replay" if include_raw else "mission-control-filtered")
    sinks = []
    if destination is not None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        sinks.append(rr.FileSink(str(destination)))
    if url: sinks.append(rr.GrpcSink(url))
    if not sinks: raise ValueError("A viewer connection or output file is required")
    stream.set_sinks(*sinks)
    adapter = RerunTelemetry(stream, basemap)
    stream.log("recording/info", rr.TextDocument(
        f"{recording['filename']}\n{recording['alignment']}\nSynthetic: {recording['synthetic']}\n"
        f"Unaligned readings: {recording['unaligned']} (retained in full replay scalar plots)"), static=True)
    stream.set_time("mission", duration=0)
    adapter.geometry(lake)
    first = int(recording["startTime"])
    catalog = defaultdict(set)
    diagnostics = RecordDiagnostics()
    field_types = {}
    telemetry_metrics = set()
    geographic, units = set(), {}
    if include_raw:
        fields = defaultdict(list)
        with Path(filename).open("rb") as handle:
            for _, channel, message in make_reader(handle, validate_crcs=True).iter_messages():
                if channel.message_encoding != "json": continue
                payload = restore_nonfinite(json.loads(message.data))
                if not isinstance(payload, dict): continue
                data = payload.get("data", payload)
                if not isinstance(data, dict): continue
                message_type = payload.get("msg_type", channel.topic.rsplit("/", 1)[-1])
                stream.reset_time()
                stream.set_time("mission", duration=(message.log_time-first)/1e9)
                stream.set_time("receive_time", timestamp=message.log_time/1e9)
                log_decoded_fields(stream, channel.topic, message_type, data, schemas=diagnostics.schemas)
                outgoing = payload.get("direction") == "tx" or channel.metadata.get("direction") == "tx" or channel.topic.startswith("/commands/")
                direction = "tx" if outgoing else "input" if channel.topic.startswith("/input/") or payload.get("direction") == "input" else "rx"
                projected = diagnostics.log(stream, topic=channel.topic, message_type=message_type, data=data, direction=direction,
                                            sys_id=payload.get("sys_id", 0), comp_id=payload.get("comp_id", 0), log_scalars=False)
                for path, value in projected.items():
                    if path in BOOLEAN_PATHS: continue
                    fields[path].append((message.log_time, float(value)))
                types = catalog_types(data)
                field_types.update({(channel.topic, field): dtype for field, dtype in types.items()})
                catalog[channel.topic].update(types)
                for metric, value in ({} if outgoing else telemetry_fields(message_type, data)).items():
                    telemetry_metrics.add(metric)
                    fields[f"telemetry/{metric}"].append((message.log_time, float(value)))
                for field, value in chart_fields(data).items():
                    catalog[channel.topic].add(field)
                    path = field_path(channel.topic, field)
                    fields[path].append((message.log_time, float(value)))
        for path, samples in fields.items():
            stream.send_columns(path, indexes=[
                rr.TimeColumn("mission", duration=[(t-first)/1e9 for t,_ in samples]),
                rr.TimeColumn("receive_time", timestamp=[t/1e9 for t,_ in samples]),
            ], columns=rr.Scalars.columns(scalars=[v for _,v in samples]))
    track = recording["track"]
    if track and track_visible:
        times = [rr.TimeColumn("mission", duration=[p["time"] for p in track])]
        stream.send_columns("geo/vehicle", indexes=times, columns=rr.GeoPoints.columns(
            positions=[[p["position"][1],p["position"][0]] for p in track], colors=[[255,45,55]]*len(track), radii=[rr.Radius.ui_points(7)]*len(track)))
        segments, segment = [], []
        for i, sample in enumerate(track):
            if i and sample["time"]-track[i-1]["time"] > 2:
                if len(segment)>1: segments.append(segment)
                segment = []
            segment.append(sample["position"])
        if len(segment)>1: segments.append(segment)
        if segments:
            stream.log("geo/track", rr.GeoLineStrings(lat_lon=[[[p[1],p[0]] for p in seg] for seg in segments], colors=[100,190,250]), static=True)
    for dataset in recording["datasets"]:
        for field in dataset["fields"]:
            name, low, high = field["name"], field["min"], field["max"]
            geographic.add((dataset["topic"], name))
            units[(dataset["topic"], name)] = field.get("unit", "")
            samples = [p for p in dataset["points"] if name in p["values"]]
            if not samples: continue
            values = [float(p["values"][name]) for p in samples]
            colors = []
            for value in values:
                fraction = (value-low)/(high-low) if high>low else .5
                colors.append([int(240-180*fraction),int(85+110*fraction),int(60+170*fraction),round(opacity*255)])
            times = [rr.TimeColumn("mission", duration=[p["time"] for p in samples])]
            # Configure a fixed visual style once, then one geographic sample per timeline row.
            path = geographic_path(dataset["topic"], name)
            stream.log(path, rr.GeoPoints.from_fields(radii=rr.Radius.ui_points(radius)), static=True)
            columns = rr.GeoPoints.columns(
                positions=[[p["position"][1],p["position"][0]] for p in samples], colors=colors)
            stream.send_columns(path, indexes=times, columns=[*columns,*rr.AnyValues(value=values).columns()])
            if not include_raw:
                catalog[dataset["topic"]].add(name)
                stream.send_columns(field_path(dataset["topic"], name), indexes=times, columns=rr.Scalars.columns(scalars=values))
    schema = field_catalog(catalog, telemetry=telemetry_metrics, units=units, geographic=geographic, types=field_types) + diagnostic_catalog(diagnostics.paths)
    selected = default_fields(schema)
    if not include_raw:
        selected = [field["id"] for field in schema]  # Explicit value-filtered reading layer.
    stream.send_blueprint(blueprint(catalog=schema, visible_fields=selected))
    stream.flush(timeout_sec=5)
    stream.disconnect()
    return {k:v for k,v in recording.items() if k not in {"track","datasets"}} | {
        "streams":len(recording["datasets"]),
        "application_id":stream.get_application_id(),
        "recording_id":stream.get_recording_id(),
        "sensor_topics":{topic:sorted(fields) for topic, fields in catalog.items()},
        "field_catalog":schema,
        "chart_presets":chart_presets(schema),
        "visible_fields":selected,
        "rerun_path":str(destination) if destination is not None else None,
        "datasets":[{"topic":d["topic"],"fields":d["fields"],"count":d["count"]} for d in recording["datasets"]],
    }
