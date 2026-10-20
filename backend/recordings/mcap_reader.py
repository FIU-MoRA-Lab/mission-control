"""Read JSON MCAP telemetry and align numeric readings to nearby GPS samples."""
from bisect import bisect_left
from collections import defaultdict
import json
import math
from pathlib import Path

from mcap.reader import make_reader

from backend.logger.records import numeric_fields, restore_nonfinite


def _position(message_type, data):
    if message_type not in {"GLOBAL_POSITION_INT", "GPS_RAW_INT", "GPS2_RAW"}:
        return None
    lat, lon = data.get("lat"), data.get("lon")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (lat, lon)):
        return None
    if message_type in {"GPS_RAW_INT", "GPS2_RAW"} and data.get("fix_type", 0) < 2:
        return None
    lat, lon = lat / 1e7, lon / 1e7
    if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
        return [lon, lat]
    return None


def load_recording(filename):
    """Use MCAP receive times; never align samples across a GPS gap over 2 s.

    Recordings without GPS remain valid for scalar plots and diagnostics.
    Transmitted commands cannot supply a vehicle position.
    """
    filename = Path(filename)
    samples = []
    positions = defaultdict(list)
    start = end = None
    count = 0
    synthetic = False
    with filename.open("rb") as handle:
        reader = make_reader(handle, validate_crcs=True)
        for metadata in reader.iter_metadata():
            synthetic |= any(str(value).lower() == "true" for key, value in metadata.metadata.items()
                             if key == "synthetic")
        for _, channel, message in reader.iter_messages():
            count += 1
            stamp = message.log_time
            start = stamp if start is None else min(start, stamp)
            end = stamp if end is None else max(end, stamp)
            if channel.message_encoding != "json":
                continue
            payload = restore_nonfinite(json.loads(message.data))
            if not isinstance(payload, dict):
                continue
            data = payload.get("data", payload)
            if not isinstance(data, dict):
                continue
            kind = payload.get("msg_type", channel.topic.rsplit("/", 1)[-1])
            outgoing = (payload.get("direction") == "tx" or channel.metadata.get("direction") == "tx"
                        or channel.topic.startswith("/commands/"))
            position = None if outgoing else _position(kind, data)
            if position is not None:
                positions[kind].append((stamp, position))
            values = numeric_fields(data)
            if values:
                samples.append((stamp, channel.topic, values))

    # Prefer the same autopilot estimate used by the live map.
    gps = next((positions[kind] for kind in ("GLOBAL_POSITION_INT", "GPS_RAW_INT", "GPS2_RAW")
                if positions[kind]), [])
    gps.sort(key=lambda item: item[0])
    times = [stamp for stamp, _ in gps]
    start = start if start is not None else 0
    end = end if end is not None else start
    datasets = defaultdict(list)
    unaligned = 0
    for stamp, topic, values in samples:
        index = bisect_left(times, stamp)
        candidates = [i for i in (index - 1, index) if 0 <= i < len(gps)]
        nearest = min(candidates, key=lambda i: abs(times[i] - stamp)) if candidates else None
        if nearest is None or abs(times[nearest] - stamp) > 2_000_000_000:
            unaligned += 1
            continue
        datasets[topic].append({"time": (stamp - start) / 1e9,
                                "position": gps[nearest][1], "values": values})
    layers = []
    for topic, points in sorted(datasets.items()):
        ranges = defaultdict(list)
        for point in points:
            for name, value in point["values"].items():
                ranges[name].append(value)
        fields = [{"name": name, "min": min(values), "max": max(values), "unit": ""}
                  for name, values in sorted(ranges.items())]
        layers.append({"topic": topic, "points": points, "fields": fields, "count": len(points)})
    return {"filename": filename.name, "format": "mcap", "messageCount": count,
            "startTime": str(start), "endTime": str(end), "duration": (end - start) / 1e9,
            "synthetic": synthetic, "alignment": "Receive time; nearest GPS within 2 seconds",
            "unaligned": unaligned, "datasets": layers,
            "track": [{"time": (stamp - start) / 1e9, "position": position}
                      for stamp, position in gps]}
