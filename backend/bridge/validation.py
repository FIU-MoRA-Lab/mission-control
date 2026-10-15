"""Validate the command payloads crossing the frontend/backend boundary."""
import math

def number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    if not minimum <= value <= maximum:
        raise ValueError(f"Value must be between {minimum} and {maximum}")
    return float(value)


def coordinates(data):
    return (number(data.get("lat", data.get("latitude")), -85.05112878, 85.05112878),
            number(data.get("lon", data.get("longitude")), -180, 180))


def file_path(data):
    path = data.get("path")
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("Expected a file path")
    return path

