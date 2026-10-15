"""Engine-independent state and behavior for waypoint planning and teleop.

Front ends translate their native input events into this module and render its
state. No UI toolkit or drawing types belong here.
"""

from __future__ import annotations

import math

DEFAULT_ZOOM = 19
TILE_SIZE = 256
MIN_ZOOM = 1
MAX_ZOOM = 19


def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Convert geographic coordinates to fractional Web Mercator tile units."""
    lat_rad = math.radians(math.copysign(min(abs(lat), 85.05112878), lat))
    n = 2.0 ** zoom
    xtile = (lon + 180.0) / 360.0 * n
    ytile = (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n
    return xtile, ytile


def tile_to_latlon(xtile: float, ytile: float, zoom: int) -> tuple[float, float]:
    """Convert fractional Web Mercator tile units to geographic coordinates."""
    n = 2.0 ** zoom
    lon = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * ytile / n)))
    return math.degrees(lat_rad), lon


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance between coordinates in meters."""
    earth_radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return earth_radius * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


class MapViewport:
    """Map center, zoom, and projection operations shared by all front ends."""

    def __init__(self, lat=None, lon=None, zoom=None):
        # Start with a useful world view while waiting for a real location.
        # Zoom into the vehicle/desktop automatically when a fix arrives.
        self.center_lat = float(lat) if lat is not None else 0.0
        self.center_lon = float(lon) if lon is not None else 0.0
        has_location = (lat is not None and lon is not None and
                        (float(lat) != 0.0 or float(lon) != 0.0))
        self._auto_zoom = zoom is None and not has_location
        initial_zoom = zoom if zoom is not None else DEFAULT_ZOOM
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, int(initial_zoom)))
        self.auto_center = True

    def screen_to_geo(self, px, py, width, height):
        cx, cy = latlon_to_tile(self.center_lat, self.center_lon, self.zoom)
        tx = cx + (px - width / 2.0) / TILE_SIZE
        ty = cy + (py - height / 2.0) / TILE_SIZE
        return tile_to_latlon(tx, ty, self.zoom)

    def geo_to_screen(self, lat, lon, width, height):
        cx, cy = latlon_to_tile(self.center_lat, self.center_lon, self.zoom)
        tx, ty = latlon_to_tile(lat, lon, self.zoom)
        return width / 2.0 + (tx - cx) * TILE_SIZE, height / 2.0 + (ty - cy) * TILE_SIZE

    def pan_pixels(self, dx, dy):
        cx, cy = latlon_to_tile(self.center_lat, self.center_lon, self.zoom)
        self.center_lat, self.center_lon = tile_to_latlon(
            cx - dx / TILE_SIZE, cy - dy / TILE_SIZE, self.zoom
        )
        self.auto_center = False
        self._auto_zoom = False

    def set_zoom(self, zoom):
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, int(zoom)))
        self._auto_zoom = False
        return self.zoom

    def zoom_at(self, zoom_delta, px, py, width, height):
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom + int(zoom_delta)))
        if new_zoom == self.zoom:
            return self.zoom
        fixed_lat, fixed_lon = self.screen_to_geo(px, py, width, height)
        self.zoom = new_zoom
        self._auto_zoom = False
        tx, ty = latlon_to_tile(fixed_lat, fixed_lon, self.zoom)
        cx = tx - (px - width / 2.0) / TILE_SIZE
        cy = ty - (py - height / 2.0) / TILE_SIZE
        self.center_lat, self.center_lon = tile_to_latlon(cx, cy, self.zoom)
        return self.zoom

    def follow(self, lat, lon):
        if self.auto_center and (lat != 0 or lon != 0):
            self.center_lat, self.center_lon = lat, lon
            if self._auto_zoom:
                self.zoom = DEFAULT_ZOOM
                self._auto_zoom = False

    def follow_fallback(self, lat, lon):
        """Center on desktop location only until vehicle GPS becomes available."""
        if self.auto_center and (lat != 0 or lon != 0):
            self.center_lat, self.center_lon = lat, lon
            if self._auto_zoom:
                self.zoom = DEFAULT_ZOOM
                self._auto_zoom = False

    def recenter(self, lat=None, lon=None):
        if lat is not None and lon is not None and (lat != 0 or lon != 0):
            self.center_lat, self.center_lon = lat, lon
        self.auto_center = True


