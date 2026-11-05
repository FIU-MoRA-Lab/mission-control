"""Describe chartable entities in the recording selected by the native viewer."""
import re

from boats.heron.visualization.schema import field_catalog, default_fields
from boats.heron.visualization.diagnostics import diagnostic_catalog
from boats.heron.visualization.presets import chart_presets
from boats.heron.visualization.paths import geographic_path


def viewer_catalog(data):
    application_id, recording_id = data.get("application_id"), data.get("recording_id")
    if not all(isinstance(value, str) and value for value in (application_id, recording_id)):
        raise ValueError("Choose a viewer recording")
    entities = data.get("entities")
    if not isinstance(entities, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("path"), str)
        or item.get("data_type") not in {"number", "bool", "text", "state", "geo"}
        for item in entities
    ):
        raise ValueError("Choose recorded chart entities")
    paths = {item["path"]: item["data_type"] for item in entities}
    topics, types, recorded = {}, {}, {}
    metrics = set()
    for path, dtype in paths.items():
        if dtype == "geo" or path.startswith(("/diagnostics/", "/state_changes/")):
            continue
        if path.startswith("/telemetry/"):
            metrics.add(path.removeprefix("/telemetry/"))
        elif path.startswith(("/sensors/", "/mavlink/", "/commands/", "/input/", "/vehicle/")):
            parts = re.split(r"(?<!\\)/", path.lstrip("/"))
            topic = (re.sub(r"\\(.)", r"\1", parts[1]) if len(parts) == 3 and parts[0] == "sensors" and "\\" in parts[1]
                     else "/" + "/".join(re.sub(r"\\(.)", r"\1", part) for part in parts[:-1]))
            field = re.sub(r"\\(.)", r"\1", parts[-1])
            topics.setdefault(topic, set()).add(field)
            if dtype != "number":
                types[(topic, field)] = dtype
            recorded[(topic, field)] = path
    geographic = {key for key in recorded if geographic_path(*key) in paths}
    schema = field_catalog(topics, telemetry=metrics, types=types, geographic=geographic)
    for field in schema:
        for (topic, name), path in recorded.items():
            if field["source"] == f"{topic}.{name}":
                field.update(id=path, path=path)
                break
    schema.extend(field for field in diagnostic_catalog(set(paths)) if field["path"] in paths)
    known = {field["path"] for field in schema}
    for path, dtype in paths.items():
        if dtype == "geo":
            continue
        if path not in known:
            schema.append(dict(id=path, path=path, label=path.rsplit("/", 1)[-1], unit="",
                               group="Recorded fields", category="11 · Vehicle state" if dtype == "state" else "06 · Custom sensors",
                               source=path, kind="state" if dtype == "state" else "sensor", data_type=dtype,
                               default=False, available=True, tags=["recorded"], description="Recorded value."))
        elif dtype == "state":
            # Native state lanes take precedence over protocol numeric types.
            for field in schema:
                if field["path"] == path:
                    field["data_type"] = "bool" if field.get("data_type") == "bool" else "state"
    return dict(application_id=application_id, recording_id=recording_id, entity_count=data.get("entity_count"), sensor_topics={k: sorted(v) for k, v in topics.items()},
                field_catalog=schema, visible_fields=default_fields(schema), chart_presets=chart_presets(schema))
