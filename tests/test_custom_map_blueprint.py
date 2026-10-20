"""Keep prototype opt-in, selection filters, and camera identity consistent."""
import os
import unittest
from unittest.mock import patch
from backend.rerun.blueprint import blueprint
from backend.rerun.schema import field_catalog


class CustomMapBlueprintTests(unittest.TestCase):
    def map_view(self, flag, **kwargs):
        with patch.dict(os.environ, {"MISSION_CONTROL_CUSTOM_MAP": flag}):
            return blueprint(**kwargs).root_container.contents[0].contents[0]

    def test_prototype_is_opt_in_and_avoids_existing_map_state(self):
        normal = self.map_view("0")
        custom = self.map_view("1")
        self.assertEqual(normal.class_identifier, "Map")
        self.assertEqual(custom.class_identifier, "FiuMap")
        self.assertNotEqual(normal.id, custom.id)
        self.assertEqual(normal.origin, custom.origin)
        self.assertEqual(normal.contents, custom.contents)

    def test_layer_filters_preserve_camera_and_base_geometry(self):
        catalog = field_catalog({"/sensor": {"temperature"}}, geographic={("/sensor", "temperature")})
        field = next(f for f in catalog if f.get("map_path"))
        hidden = self.map_view("1", catalog=catalog, visible_fields=[])
        shown = self.map_view("1", catalog=catalog, visible_fields=[field["id"]])
        self.assertEqual(hidden.id, shown.id)
        self.assertEqual(hidden.class_identifier, shown.class_identifier)
        self.assertIn("/geo/vehicle/**", hidden.contents)
        self.assertIn("/geo/route/**", hidden.contents)
        self.assertNotIn(field["map_path"] + "/**", hidden.contents)
        self.assertIn(field["map_path"] + "/**", shown.contents)
