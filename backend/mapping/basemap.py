"""Stitch offline imagery and map geographic coordinates into its pixel frame."""
import math
from pathlib import Path
from PIL import Image
from backend.mapping.projection import latlon_to_tile

def build_basemap(tiles, geometry, output: Path):
    """Stitch local FIU tiles only; no internet dependency for lake planning."""
    metadata = tiles.metadata()
    points = geometry._lake_outline
    zoom = 19
    coords = [latlon_to_tile(lat, lon, zoom) for lat, lon in points]
    if not coords:
        raise ValueError("FIU shoreline is missing")
    x0 = math.floor(min(p[0] for p in coords)) - 1
    y0 = math.floor(min(p[1] for p in coords)) - 1
    x1 = math.floor(max(p[0] for p in coords)) + 2
    y1 = math.floor(max(p[1] for p in coords)) + 2
    image = Image.new("RGBA", ((x1 - x0) * 256, (y1 - y0) * 256), (24, 30, 37, 255))
    for x in range(x0, x1):
        for y in range(y0, y1):
            tile = tiles._fetch_fiu_tile(zoom, x, y)
            if tile is not None:
                image.alpha_composite(tile.convert("RGBA"), ((x-x0)*256, (y-y0)*256))
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    return {"path": str(output), "zoom": zoom, "x": x0, "y": y0,
            "width": image.width, "height": image.height, **metadata}


def local_xy(lon, lat, basemap):
    x, y = latlon_to_tile(lat, lon, basemap["zoom"])
    return [(x - basemap["x"]) * 256, (y - basemap["y"]) * 256]


