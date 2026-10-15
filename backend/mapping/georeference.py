"""Read the georeferencing embedded in FIU rasters and their valid-coverage metadata."""
from dataclasses import dataclass
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class GeoRaster:
    path: Path
    west: float
    north: float
    pixel_lon: float
    pixel_lat: float
    width: int
    height: int
    bounds: tuple[float, float, float, float]  # west, south, east, north
    crs: str = "EPSG:4326"

    @classmethod
    def read(cls, path, image):
        keys = image.tag_v2.get(34735, ())
        entries = {keys[i]: keys[i+3] for i in range(4, len(keys), 4) if keys[i+1] == 0}
        if entries.get(1024) != 2 or entries.get(2048) != 4326:
            raise ValueError(f"{path.name}: FIU basemap requires a WGS84 geographic GeoTIFF")
        if entries.get(1025, 1) != 1:
            raise ValueError(f"{path.name}: unsupported GeoTIFF raster convention")
        tie = image.tag_v2[33922]
        scale = image.tag_v2[33550]
        if not all(math.isfinite(v) for v in (*tie, *scale)) or scale[0] <= 0 or scale[1] <= 0:
            raise ValueError(f"{path.name}: invalid GeoTIFF positioning tags")
        west = tie[3] - tie[0] * scale[0]
        north = tie[4] + tie[1] * scale[1]
        width, height = image.size
        raster_bounds = (west, north-height*scale[1], west+width*scale[0], north)
        coverage_file = path.parent.parent / "metadata_tiles" / f"{path.stem}.geojson"
        coverage = json.loads(coverage_file.read_text())
        points = [point for feature in coverage["features"]
                  for ring in feature["geometry"]["coordinates"] for point in ring]
        if not points or not all(len(p) >= 2 and all(math.isfinite(v) for v in p[:2]) for p in points):
            raise ValueError(f"{path.name}: invalid coverage metadata")
        valid = (min(p[0] for p in points), min(p[1] for p in points),
                 max(p[0] for p in points), max(p[1] for p in points))
        bounds = (max(valid[0], raster_bounds[0]), max(valid[1], raster_bounds[1]),
                  min(valid[2], raster_bounds[2]), min(valid[3], raster_bounds[3]))
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            raise ValueError(f"{path.name}: coverage does not overlap raster georeferencing")
        return cls(path, west, north, scale[0], scale[1], width, height, bounds)

    def pixel(self, lat, lon):
        """WGS84 coordinate to raster pixel boundary using ModelTiepoint/PixelScale."""
        return (lon-self.west)/self.pixel_lon, (self.north-lat)/self.pixel_lat


def mercator_lat(tile_y, zoom):
    return math.degrees(math.atan(math.sinh(math.pi*(1-2*tile_y/(1 << zoom)))))
