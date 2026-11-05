"""Geographic alignment checks: actual TIFF tags, coverage, Mercator warp, and GeoPoints."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from PIL import Image, TiffImagePlugin
import rerun as rr

from backend.mapping.tiles import RasterTiles
from backend.mapping.georeference import GeoRaster, mercator_lat
from boats.heron.visualization.live import RerunTelemetry

ROOT=Path(__file__).resolve().parents[1]

class FiuGeoreferenceTests(unittest.TestCase):
    def test_packaged_tags_and_coverage_define_basemap(self):
        with tempfile.TemporaryDirectory() as tmp:
            tiles=RasterTiles(ROOT/'assets/fiu-basemap',tmp)
            try:
                metadata=tiles.metadata()
                self.assertEqual(metadata['crs'],'EPSG:4326')
                self.assertEqual(len(metadata['rasters']),2)
                for raster,_ in tiles._rasters():
                    self.assertEqual(raster.pixel(raster.north,raster.west),(0,0))
                    coverage_file=raster.path.parent.parent/'metadata_tiles'/f'{raster.path.stem}.geojson'
                    points=json.loads(coverage_file.read_text())['features'][0]['geometry']['coordinates'][0]
                    self.assertAlmostEqual(raster.bounds[0],min(p[0] for p in points))
                    self.assertAlmostEqual(raster.bounds[3],max(p[1] for p in points))
                    self.assertEqual(raster.crs,'EPSG:4326')
            finally: tiles.close()

    def test_tile_rows_use_mercator_latitude_instead_of_linear_resize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'raster_tiles').mkdir(); (root/'metadata_tiles').mkdir()
            # Encode latitude as the green channel; source coordinates are WGS84.
            image=Image.new('RGB',(360,170))
            image.putdata([(0,round(85-(row+.5)+90),0) for row in range(170) for _ in range(360)])
            tags=TiffImagePlugin.ImageFileDirectory_v2()
            tags[33922]=(0.,0.,0.,-180.,85.,0.)
            tags[33550]=(1.,1.,0.)
            tags[34735]=(1,1,0,3,1024,0,1,2,1025,0,1,1,2048,0,1,4326)
            image.save(root/'raster_tiles/test.tif',tiffinfo=tags)
            coverage={'features':[{'geometry':{'coordinates':[[[-180,-85],[180,-85],[180,85],[-180,85],[-180,-85]]]}}]}
            (root/'metadata_tiles/test.geojson').write_text(json.dumps(coverage))
            tiles=RasterTiles(root,root/'cache')
            try:
                warped=tiles._fetch_fiu_tile(2,2,1)
                actual=warped.getpixel((128,128))[1]
                expected=mercator_lat(1+128.5/256,2)+90
                linear=(mercator_lat(1,2)+mercator_lat(2,2))/2+90
                self.assertAlmostEqual(actual,expected,delta=1.5)
                self.assertGreater(abs(actual-linear),5)
            finally: tiles.close()

    def test_live_boat_marker_is_red_at_vehicle_position(self):
        stream = Mock()
        adapter = RerunTelemetry(stream, {'crs': 'EPSG:4326', 'bounds': [], 'center': []})
        adapter.state({"simulating": False, "manual": {"throttle": 0, "yaw": 0, "source": "NONE", "active": False},
                       "mode": "HOLD", "armed": False, "connected": True, "fix": 2,
                       "lat": 25.758, "lon": -80.374, "waypoints": [], "lake": {}, "status": ""}, 1)
        marker = next(call.args[1] for call in stream.log.call_args_list if call.args[0] == "geo/vehicle")
        self.assertEqual(marker.positions.as_arrow_array().to_pylist(), [[25.758, -80.374]])
        self.assertEqual(marker.colors.as_arrow_array().to_pylist(), [0xff2d37ff])
        self.assertEqual(marker.radii.as_arrow_array().to_pylist(), [-7.0])

    def test_gps_loss_clears_boat_without_clearing_map_and_recovery_breaks_track(self):
        stream = Mock()
        adapter = RerunTelemetry(stream, {'crs': 'EPSG:4326', 'bounds': [], 'center': []})
        state = dict(simulating=False, manual=dict(throttle=0, yaw=0, source="NONE", active=False),
                     mode="HOLD", armed=False, connected=True, fix=1,
                     lat=25.7424611, lon=-80.3760635, waypoints=[], lake={}, status="")
        adapter.state(state, 0)
        self.assertFalse(any(c.args[0] == "geo/vehicle" for c in stream.log.call_args_list))
        adapter.state(state | dict(fix=3, lat=25.758), 1)
        adapter.state(state | dict(fix=3, lat=25.75801), 2)
        stream.reset_mock()
        adapter.state(state, 3)
        adapter.state(state, 4)
        clears = [c.args[0] for c in stream.log.call_args_list if isinstance(c.args[1], rr.Clear)]
        self.assertEqual(clears, ["geo/vehicle"])
        stream.reset_mock()
        # Returning to the same position must redraw the marker, with no line
        # connecting it across the interval where the fix was unavailable.
        adapter.state(state | dict(fix=3, lat=25.75801), 5)
        self.assertTrue(any(c.args[0] == "geo/vehicle" for c in stream.log.call_args_list))
        self.assertFalse(any(c.args[0].startswith("geo/track/") for c in stream.log.call_args_list))

    def test_live_overlays_keep_lat_lon_as_geopoints(self):
        stream=Mock()
        metadata={'crs':'EPSG:4326','bounds':[-80.38,25.75,-80.37,25.76],'center':[25.755,-80.375]}
        adapter=RerunTelemetry(stream,metadata)
        adapter.geometry({'features':[{'geometry':{'type':'Point','coordinates':[-80.374,25.758]},'properties':{'role':'gps'}}]})
        args=stream.log.call_args_list[-1].args
        self.assertEqual(args[0],'geo/lake/gps/0')
        self.assertIsInstance(args[1],rr.GeoPoints)
        self.assertEqual(args[1].positions.as_arrow_array().to_pylist(),[[25.758,-80.374]])
        self.assertFalse(any(call.args[0].startswith('local/') for call in stream.log.call_args_list))
