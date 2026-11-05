"""Read native RRD samples without resampling or discarding typed values.

Each field has its own Polars table. This preserves uint64, lists, booleans,
strings and explicit nulls without a heterogeneous floating-point value column.
Timeline columns ending in _ns are exact int64 nanoseconds; time_s is relative
to the first sample on the selected timeline. No latest-at fill is performed.
"""
from dataclasses import dataclass
from pathlib import Path
import math
import re

import polars as pl
import pyarrow as pa
from rerun.chunk import RrdReader
from boats.heron.visualization.schema import field_catalog


def _parts(path):
    return [re.sub(r"\\(.)", r"\1", part) for part in re.split(r"(?<!\\)/", path.strip("/"))]


def _topic_field(path):
    parts = _parts(path)
    if len(parts) == 3 and parts[0] == "sensors" and parts[1].startswith("/"):
        return parts[1], parts[2]
    return "/" + "/".join(parts[:-1]), parts[-1]


def _state(value):
    if value is None:
        return "null"
    if isinstance(value, float) and not math.isfinite(value):
        return "nan" if math.isnan(value) else "+inf" if value > 0 else "-inf"
    return "present"


def _one(batch):
    if batch is None or len(batch) == 0:
        return None
    if len(batch) != 1:
        # Preserve batched scalars from externally produced RRDs as arrays.
        return batch
    return batch[0]


@dataclass
class Run:
    path: Path
    recording_id: str
    catalog: pl.DataFrame
    fields: dict[str, pl.DataFrame]
    metadata: dict[str, pl.DataFrame]
    timelines: tuple[str, ...]
    origins: dict[str, int]

    def samples(self, path: str, *, timeline: str = "mission") -> pl.DataFrame:
        """Return exact events on one clock; never align unrelated messages implicitly."""
        frame = self.fields[path]
        column = timeline + "_ns"
        if column not in frame.columns:
            raise ValueError(f"{path} has no {timeline!r} timeline")
        frame = frame.filter(pl.col(column).is_not_null()).sort(column)
        origin = self.origins[timeline]
        return frame.with_columns(((pl.col(column) - origin) / 1e9).alias("time_s"))


def load_rrd(filename: str | Path, *, recording_id: str | None = None,
             max_rows: int = 2_000_000) -> Run:
    """Load one data recording. Refuse ambiguous multi-recording files.

    Old scalar-only recordings are readable, but their missing fields cannot be
    reconstructed. Custom null-only fields retain explicit state with null dtype.
    max_rows bounds decoded chunk rows, including scalar projections and schemas.
    """
    path = Path(filename).expanduser().resolve()
    reader = RrdReader(path)
    stores = [entry for entry in reader.recordings() if entry.kind == "recording"]
    if recording_id:
        stores = [entry for entry in stores if entry.recording_id == recording_id]
    if len(stores) != 1:
        raise ValueError("Choose one recording_id; available: " + ", ".join(entry.recording_id for entry in stores))
    typed, scalars, metadata, annotations, types = {}, {}, {}, {}, {}
    timelines = set()
    total = 0
    for chunk in reader.stream(store=stores[0]):
        total += chunk.num_rows
        if total > max_rows:
            raise ValueError(f"Recording exceeds {max_rows:,} chunk rows; increase max_rows for this run.")
        batch = chunk.to_record_batch()
        entity = chunk.entity_path
        indexes = {field.name: batch.column(field.name).cast(pa.int64()).to_pylist()
                   for field in batch.schema if (field.metadata or {}).get(b"rerun:kind") == b"index"
                   and (pa.types.is_timestamp(field.type) or pa.types.is_duration(field.type))}
        timelines.update(indexes)
        data = {field.name: batch.column(field.name).to_pylist()
                for field in batch.schema if (field.metadata or {}).get(b"rerun:kind") == b"data"}
        if entity.endswith("/schema"):
            annotation = annotations.setdefault(entity.removesuffix("/schema"), {})
            for name, values in data.items():
                if values:
                    annotation[name] = _one(values[-1])
            continue
        is_metadata = entity.startswith("/records/")
        is_typed = "state" in data or "value" in data
        is_scalar = "Scalars:scalars" in data
        if not (is_metadata or is_typed or is_scalar):
            continue
        if not indexes:
            continue  # static styling/properties are not acquired samples
        destination = metadata if is_metadata else typed if is_typed else scalars
        rows = destination.setdefault(entity, {})
        if not is_metadata:
            component = "value" if "value" in data else "Scalars:scalars" if is_scalar else None
            if component:
                dtype = batch.schema.field(component).type.value_type
                if component == "Scalars:scalars" and any(v is not None and len(v) != 1 for v in data[component]):
                    dtype = pa.list_(dtype)
                previous = types.get((entity, is_typed))
                if previous is not None and previous != dtype:
                    try:
                        dtype = pa.unify_schemas([pa.schema([("value", previous)]),
                                                  pa.schema([("value", dtype)])]).field("value").type
                    except pa.ArrowInvalid as exc:
                        raise ValueError(f"Inconsistent value types at {entity}: {previous} and {dtype}") from exc
                types[(entity, is_typed)] = dtype
        row_ids = batch.column("rerun.controls.RowId").to_pylist()
        for i, row_id in enumerate(row_ids):
            row = rows.setdefault(row_id, {"row_id": row_id.hex()})
            row.update({name + "_ns": values[i] for name, values in indexes.items()})
            if is_metadata:
                row.update({name: _one(values[i]) for name, values in data.items()})
            else:
                for name in ("state", "value", "Scalars:scalars"):
                    if name in data and data[name][i] is not None:
                        row["value" if name == "Scalars:scalars" else name] = _one(data[name][i])
    frames, descriptors = {}, []
    for entity in sorted(set(typed) | set(scalars)):
        complete = entity in typed
        rows = list((typed if complete else scalars)[entity].values())
        for row in rows:
            row.setdefault("value", None)
            row.setdefault("state", _state(row["value"]))
        time_names = sorted({name for row in rows for name in row if name.endswith("_ns")})
        schema = pa.schema([("row_id", pa.string()), *[(name, pa.int64()) for name in time_names],
                            ("value", types.get((entity, complete), pa.null())), ("state", pa.string())])
        frames[entity] = pl.from_arrow(pa.Table.from_pylist(rows, schema=schema))
        topic, field = _topic_field(entity)
        if entity.startswith("/telemetry/"):
            catalog = field_catalog({}, telemetry={entity.removeprefix("/telemetry/")})
        else:
            catalog = field_catalog({topic: {field}}, telemetry=set())
        description = catalog[0] if catalog else {}
        annotation = annotations.get(entity, {})
        descriptors.append(dict(path=entity, topic=topic, field=field,
                                type=str(schema.field("value").type),
                                protocol_type=annotation.get("protocol_type", description.get("data_type", "")),
                                unit=annotation.get("unit", description.get("unit", "")),
                                enum=annotation.get("enum", description.get("enum", "")),
                                description=description.get("description", ""),
                                storage="typed" if complete else "legacy scalar",
                                samples=len(rows), nulls=sum(row["state"] == "null" for row in rows),
                                nonfinite=sum(row["state"] in {"nan", "+inf", "-inf"} for row in rows)))
    metadata_frames = {}
    for entity, rows in metadata.items():
        records = list(rows.values())
        # Arrow inference preserves int64 timestamp integers and nullable columns.
        metadata_frames[entity] = pl.from_arrow(pa.Table.from_pylist(records))
    catalog_schema = {name: pl.String for name in ["path", "topic", "field", "type", "protocol_type", "unit", "enum", "description", "storage"]}
    catalog_schema.update({name: pl.Int64 for name in ["samples", "nulls", "nonfinite"]})
    return Run(path, stores[0].recording_id, pl.DataFrame(descriptors, schema=catalog_schema),
               frames, metadata_frames, tuple(sorted(timelines)),
               {name: min(frame[name + "_ns"].min() for frame in frames.values()
                          if name + "_ns" in frame.columns and frame[name + "_ns"].min() is not None)
                for name in timelines if any(name + "_ns" in frame.columns and frame[name + "_ns"].min() is not None for frame in frames.values())})
