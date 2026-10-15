"""Open an existing RRD without converting or rescaling its recorded values."""
import re
from pathlib import Path
import rerun as rr
import rerun.blueprint as rrb
import pyarrow as pa
from rerun.chunk import RrdReader
from backend.rerun.blueprint import blueprint
from backend.rerun.schema import field_catalog, default_fields
from backend.rerun.paths import geographic_path
from backend.rerun.diagnostics import diagnostic_catalog, BOOLEAN_PATHS
from backend.rerun.presets import chart_presets
from backend.rerun.state_changes import state_label


def _project_boolean_chunk(stream, chunk):
    """Add categorical lanes to older RRDs while retaining their original components/clocks."""
    batch = chunk.to_record_batch()
    value_column = state_column = None
    indexes = []
    for index, field in enumerate(batch.schema):
        metadata = field.metadata or {}
        if metadata.get(b"rerun:component") == b"value" and pa.types.is_list(field.type) and pa.types.is_boolean(field.type.value_type):
            value_column = batch.column(index).to_pylist()
        elif metadata.get(b"rerun:component") == b"state":
            state_column = batch.column(index).to_pylist()
        elif chunk.entity_path in BOOLEAN_PATHS and metadata.get(b"rerun:component_type") == b"rerun.components.Scalar":
            value_column = batch.column(index).to_pylist()
        elif metadata.get(b"rerun:kind") == b"index":
            indexes.append((field, batch.column(index)))
    if value_column is None and state_column is None:
        return
    groups = {}
    for row in range(batch.num_rows):
        sample = value_column[row] if value_column is not None else None
        if sample and sample[0] is not None:
            value = sample[0]
            if not isinstance(value, bool):
                value = bool(value) if value in {0, 1} else None
        elif sample == [] or (sample and sample[0] is None) or (state_column and state_column[row] == ["null"]):
            value = None
        else:
            continue
        mask = tuple(index for index, (_, column) in enumerate(indexes) if column[row].is_valid)
        rows, labels = groups.setdefault(mask, ([], []))
        rows.append(row)
        labels.append(state_label(chunk.entity_path.rsplit("/", 1)[-1], value))
    for mask, (rows, labels) in groups.items():
        times = []
        for index in mask:
            field, column = indexes[index]
            array = column.take(pa.array(rows)).to_numpy(zero_copy_only=False)
            kind = "timestamp" if pa.types.is_timestamp(field.type) else "duration" if pa.types.is_duration(field.type) else "sequence"
            times.append(rr.TimeColumn(field.name, **{kind: array}))
        if times:
            stream.send_columns(chunk.entity_path, indexes=times, columns=rr.StateChange.columns(state=labels))
        else:
            stream.log(chunk.entity_path, rr.StateChange(state=labels), static=True)


def open_rrd(filename, *, url=None, destination=None):
    reader = RrdReader(filename)
    entries = reader.recordings()
    if not entries:
        raise ValueError("The RRD contains no data recording")
    # A separate replay identity prevents reopening a live RRD from modifying
    # the active live recording or its blueprint.
    stream = rr.RecordingStream("mission-control-rrd-replay")
    sinks = []
    if url:
        sinks.append(rr.GrpcSink(url))
    if destination:
        sinks.append(rr.FileSink(str(destination)))
    if not sinks:
        raise ValueError("A viewer connection or output file is required")
    stream.set_sinks(*sinks)
    topics, metrics, paths, geographic, extra = {}, set(), set(), set(), []
    rows = 0
    recorded_paths, map_paths = {}, {}
    diagnostic_paths, field_types = set(), {}
    state_paths = set()
    try:
        pending = []
        for chunk in reader.stream(store=entries[0]):
            path = chunk.entity_path
            paths.add(path)
            rows += chunk.num_rows
            chunk_schema = chunk.to_record_batch().schema
            if any((field.metadata or {}).get(b"rerun:archetype") == b"rerun.archetypes.StateChange" for field in chunk_schema):
                state_paths.add(path)
                if path in BOOLEAN_PATHS: diagnostic_paths.add(path)
            scalar = any(field.metadata and field.metadata.get(b"rerun:component_type") == b"rerun.components.Scalar"
                         for field in chunk_schema)
            dtype = None
            for field in chunk_schema:
                if field.metadata and field.metadata.get(b"rerun:component") == b"value" and pa.types.is_list(field.type):
                    if pa.types.is_boolean(field.type.value_type): dtype = "bool"
                    elif pa.types.is_string(field.type.value_type): dtype = "text"
            if scalar and path.startswith("/diagnostics/"):
                diagnostic_paths.add(path)
            elif scalar or dtype:
                if path.startswith("/telemetry/"):
                    metrics.add(path.removeprefix("/telemetry/"))
                elif path.startswith(("/sensors/", "/mavlink/", "/commands/", "/input/", "/vehicle/")):
                    parts = re.split(r"(?<!\\)/", path.lstrip("/"))
                    # Legacy recordings escaped the entire topic beneath /sensors.
                    if parts[0] == "sensors" and len(parts) == 3 and "\\" in parts[1]:
                        topic = re.sub(r"\\(.)", r"\1", parts[1])
                    else:
                        topic = "/" + "/".join(re.sub(r"\\(.)", r"\1", part) for part in parts[:-1])
                    field = re.sub(r"\\(.)", r"\1", parts[-1])
                    topics.setdefault(topic, set()).add(field)
                    recorded_paths[(topic, field)] = path
                    if dtype: field_types[(topic, field)] = dtype
                else:
                    extra.append(path)
            pending.append(chunk)
            if len(pending) >= 128:
                stream.send_chunks(pending)
                pending = []
        if pending:
            stream.send_chunks(pending)
        boolean_paths = {recorded_paths[key] for key, dtype in field_types.items() if dtype == "bool"}
        missing_lanes = (boolean_paths | (diagnostic_paths & BOOLEAN_PATHS)) - state_paths
        if missing_lanes:
            for chunk in reader.stream(store=entries[0]):
                if chunk.entity_path in missing_lanes:
                    _project_boolean_chunk(stream, chunk)
        for topic, fields in topics.items():
            for field in fields:
                candidates = [geographic_path(topic, field),
                              f"/geo/readings/{rr.escape_entity_path_part(topic)}/{rr.escape_entity_path_part(field)}"]
                for map_path in candidates:
                    if map_path in paths:
                        geographic.add((topic, field))
                        map_paths[(topic, field)] = map_path
                        break
        schema = field_catalog(topics, telemetry=metrics, geographic=geographic, types=field_types)
        # Preserve legacy physical paths so their layer controls still select data.
        for descriptor in schema:
            if descriptor["kind"] == "telemetry":
                continue
            for (topic, field), path in recorded_paths.items():
                if descriptor["source"] == f"{topic}.{field}":
                    descriptor.update(id=path, path=path)
                    if (topic, field) in map_paths:
                        descriptor["map_path"] = map_paths[(topic, field)]
                    break
        for path in sorted(set(extra)):
            schema.append(dict(id=path, path=path, label=path.rsplit("/", 1)[-1], unit="",
                               group="RRD scalars", category="06 · Custom sensors", source=path,
                               kind="sensor", data_type="number", default=False, tags=["rrd", "sensor"],
                               description="Recorded scalar value; units were not specified in the field catalog."))
        schema.extend(diagnostic_catalog(diagnostic_paths))
        known_paths = {field["path"] for field in schema}
        for path in sorted(state_paths - known_paths):
            schema.append(dict(id=path, path=path, label=path.rsplit("/", 1)[-1], unit="",
                               group="Recorded states", category="11 · Vehicle state", source=path,
                               kind="state", data_type="state", default=False, available=True, tags=["state", "categorical"],
                               description="Recorded Rerun StateChange lane, preserving its labels and timelines."))
        selected = default_fields(schema)
        if schema:
            stream.send_blueprint(blueprint(catalog=schema, visible_fields=selected))
        else:
            stream.send_blueprint(rrb.Blueprint(auto_layout=True, auto_views=True))
        # A whole recording can queue far more data than a live update. Keep this
        # below the frontend's 120-second recording-request timeout.
        try:
            stream.flush(timeout_sec=60)
        except RuntimeError as exc:
            if "Flush timed out" not in str(exc):
                raise
            raise TimeoutError(
                f"Timed out delivering {Path(filename).name} to Rerun after 60 seconds. "
                "The recording may be partially loaded; check that the viewer is responsive and retry."
            ) from exc
        return {"filename": Path(filename).name, "format": "rrd", "messageCount": rows,
                "streams": len(topics), "datasets": [], "sensor_topics": {k: sorted(v) for k, v in topics.items()},
                "application_id": stream.get_application_id(), "recording_id": stream.get_recording_id(),
                "field_catalog": schema, "chart_presets": chart_presets(schema), "visible_fields": selected, "rerun_path": str(filename)}
    finally:
        stream.disconnect()
