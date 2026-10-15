"""Cached Esri raster tiles with bundled FIU imagery, available to the controller bridge."""
import math
import threading
import time
from io import BytesIO
from pathlib import Path
import requests
from PIL import Image
from backend.mapping.projection import TILE_SIZE, latlon_to_tile
from backend.mapping.georeference import GeoRaster, mercator_lat

ESRI_TILE_URL = "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"


class RasterTiles:
    def __init__(self, fiu_root, cache_dir):
        self.fiu_root = Path(fiu_root)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._fiu_lock = threading.Lock()
        self._fiu_images = {}
        self._fiu_rasters = {}
        self.last_error = ""
        self._downloads = threading.BoundedSemaphore(6)

    def png(self, source, z, x, y):
        if source not in {"esri", "fiu"} or not 0 <= z <= 22 or not (0 <= x < 2**z and 0 <= y < 2**z):
            raise ValueError("Invalid tile")
        if source == "fiu":
            image = self._fetch_fiu_tile(z, x, y)
            if image is None:
                raise OSError("FIU imagery is unavailable")
        else:
            path = self.cache_dir / f"esri_imagery_z{z}_x{x}_y{y}.jpg"
            image = None
            try:
                with Image.open(path) as cached:
                    image = cached.convert("RGB")
            except (OSError, ValueError):
                pass
            if image is None or time.time() - path.stat().st_mtime > 86400:
                try:
                    with self._downloads:
                        response = requests.get(ESRI_TILE_URL.format(z=z, x=x, y=y), timeout=5,
                                                headers={"User-Agent": "HeronUSV-GCS/2.0"})
                        response.raise_for_status()
                        with Image.open(BytesIO(response.content)) as downloaded:
                            image = downloaded.convert("RGB")
                        # Atomic replacement prevents readers seeing partially written tiles.
                        import tempfile
                        with tempfile.NamedTemporaryFile(dir=self.cache_dir, suffix=".jpg", delete=False) as out:
                            temp = Path(out.name)
                            image.save(out, format="JPEG", quality=92)
                        temp.replace(path)
                    self.last_error = ""
                except (requests.RequestException, OSError, ValueError) as exc:
                    self.last_error = self._brief_error("Esri tiles", exc)
                    if image is None:
                        raise OSError(self.last_error) from exc
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def close(self):
        with self._fiu_lock:
            for image in self._fiu_images.values():
                image.close()
            self._fiu_images.clear()
            self._fiu_rasters.clear()

    def _fetch_fiu_tile(self, z: int, x: int, y: int):
        """Warp the packaged WGS84 GeoTIFF imagery into a Web Mercator tile."""
        with self._fiu_lock:
            return self._render_fiu_tile(z, x, y)

    def _overlay_fiu_tile(self, base_image, z: int, x: int, y: int):
        """Keep Esri visible outside the packaged FIU imagery coverage."""
        overlay = self._fetch_fiu_tile(z, x, y)
        if overlay is None:
            return base_image
        return Image.alpha_composite(base_image.convert("RGBA"), overlay).convert("RGB")

    def _rasters(self):
        """Open trusted packaged rasters lazily and validate their georeferencing."""
        for path in sorted(self.fiu_root.glob("**/raster_tiles/*.tif")):
            if path not in self._fiu_images:
                Image.MAX_IMAGE_PIXELS = None  # The packaged TIFFs are 16384 x 16384.
                image = Image.open(path)
                try:
                    raster = GeoRaster.read(path, image)
                except Exception:
                    image.close()
                    raise
                self._fiu_images[path] = image
                self._fiu_rasters[path] = raster
            yield self._fiu_rasters[path], self._fiu_images[path]

    def metadata(self):
        with self._fiu_lock:
            rasters = list(self._rasters())
        if not rasters:
            raise ValueError("FIU basemap GeoTIFFs were not found")
        bounds = [min(r.bounds[0] for r,_ in rasters), min(r.bounds[1] for r,_ in rasters),
                  max(r.bounds[2] for r,_ in rasters), max(r.bounds[3] for r,_ in rasters)]
        return {"crs":"EPSG:4326", "bounds":bounds,
                "center":[(bounds[1]+bounds[3])/2, (bounds[0]+bounds[2])/2],
                "rasters":[{"file":r.path.name,"bounds":list(r.bounds),
                            "origin":[r.north,r.west],"pixel_scale":[r.pixel_lon,r.pixel_lat]} for r,_ in rasters]}

    def _render_fiu_tile(self, z: int, x: int, y: int):
        """Reproject WGS84 rasters to EPSG:3857 XYZ tiles using exact row latitudes."""
        from PIL import ImageDraw
        output = Image.new("RGBA", (TILE_SIZE, TILE_SIZE), (0, 0, 0, 0))
        rasters = list(self._rasters())
        if not rasters:
            self.last_error = "FIU basemap GeoTIFFs were not found."
            return None
        n = 1 << z
        tile_west, tile_east = x/n*360-180, (x+1)/n*360-180
        tile_north, tile_south = mercator_lat(y,z), mercator_lat(y+1,z)
        for raster, image in rasters:
            west,south,east,north = raster.bounds
            west,east = max(west,tile_west), min(east,tile_east)
            south,north = max(south,tile_south), min(north,tile_north)
            if west >= east or south >= north:
                continue
            src_left,src_top = raster.pixel(north,west)
            src_right,src_bottom = raster.pixel(south,east)
            box = (max(0,math.floor(src_left)),max(0,math.floor(src_top)),
                   min(raster.width,math.ceil(src_right)),min(raster.height,math.ceil(src_bottom)))
            crop = image.crop(box).convert("RGBA")
            # One mesh strip per output row: linear latitude resize is not Web Mercator.
            mesh = []
            for row in range(TILE_SIZE):
                lat_top = mercator_lat(y+row/TILE_SIZE,z)
                lat_bottom = mercator_lat(y+(row+1)/TILE_SIZE,z)
                left,top = raster.pixel(lat_top,tile_west)
                right,bottom = raster.pixel(lat_bottom,tile_east)
                left,right,top,bottom = left-box[0],right-box[0],top-box[1],bottom-box[1]
                mesh.append(((0,row,TILE_SIZE,row+1), (left,top,left,bottom,right,bottom,right,top)))
            warped = crop.transform((TILE_SIZE,TILE_SIZE),Image.Transform.MESH,mesh,
                                    resample=Image.Resampling.BILINEAR)
            left = max(0,math.ceil((west-tile_west)/(tile_east-tile_west)*TILE_SIZE-.5))
            right = min(TILE_SIZE,math.ceil((east-tile_west)/(tile_east-tile_west)*TILE_SIZE-.5))
            top = max(0,math.ceil((latlon_to_tile(north,west,z)[1]-y)*TILE_SIZE-.5))
            bottom = min(TILE_SIZE,math.ceil((latlon_to_tile(south,west,z)[1]-y)*TILE_SIZE-.5))
            if left < right and top < bottom:
                mask=Image.new("L",output.size,0)
                ImageDraw.Draw(mask).rectangle((left,top,right-1,bottom-1),fill=255)
                # Retain the source alpha inside its metadata coverage.
                from PIL import ImageChops
                warped.putalpha(ImageChops.multiply(warped.getchannel("A"),mask))
                output.alpha_composite(warped)
        self.last_error = ""
        return output

    @staticmethod
    def _brief_error(source, exc):
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            return f"{source} returned HTTP {exc.response.status_code}."
        return f"{source} request failed ({type(exc).__name__})."

