"""Boat selection must resolve before starting a UI or vehicle backend."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.launch import main
from backend.mapping.blueprint import map_view
from backend.paths import ASSETS
from boats.heron import paths
from boats.heron.backend.sdk.discovery import endpoint_path


class BoatSelectionTests(unittest.TestCase):
    def test_heron_launches_one_window(self):
        process = Mock()
        process.wait.return_value = 0
        with patch('sys.argv', ['mission-control', '--boat', 'heron', '--demo']), \
             patch('backend.launch.launch_viewer', return_value=process) as launch:
            with self.assertRaises(SystemExit) as result:
                main()
        self.assertEqual(result.exception.code, 0)
        launch.assert_called_once_with(['--demo'])

    def test_surveyor_and_unknown_boats_do_not_start_a_window(self):
        for boat in ('surveyor', 'unknown'):
            with self.subTest(boat=boat), \
                 patch('sys.argv', ['mission-control', '--boat', boat]), \
                 patch('backend.launch.launch_viewer') as launch, \
                 contextlib.redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as result:
                    main()
                self.assertEqual(result.exception.code, 2)
                self.assertIn('pending' if boat == 'surveyor' else 'invalid choice', error.getvalue())
                launch.assert_not_called()

    def test_boat_flag_can_precede_or_follow_run(self):
        for args in (['--boat', 'heron', 'run', 'cases', '--list'],
                     ['run', 'cases', '--boat', 'heron', '--list']):
            with self.subTest(args=args), patch('sys.argv', ['mission-control', *args]), \
                 patch('boats.heron.backend.batch.main', return_value=0) as batch, \
                 patch('backend.launch.launch_viewer') as launch:
                with self.assertRaises(SystemExit) as result:
                    main()
                self.assertEqual(result.exception.code, 0)
                batch.assert_called_once_with(['cases', '--list'])
                launch.assert_not_called()

    def test_state_and_default_recordings_are_scoped_to_heron(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {'MISSION_CONTROL_DATA_DIR': tmp}, clear=True):
            self.assertEqual(paths.data_directory(), Path(tmp) / 'boats' / 'heron')
            self.assertEqual(paths.log_directory(), Path(tmp) / 'logs' / 'heron')
            self.assertEqual(endpoint_path(), paths.data_directory() / '.mission-control' / 'connection.json')
            self.assertEqual(paths.ASSETS, ASSETS)
            self.assertTrue((paths.CONFIG / 'fiu_lake.geojson').is_file())

    def test_shared_map_keeps_identity_when_boat_fields_change(self):
        with patch.dict(os.environ, {'MISSION_CONTROL_CUSTOM_MAP': '0'}):
            self.assertEqual(map_view(['/geo/vehicle/**']).id,
                             map_view(['/geo/vehicle/**', '/geo/sensor/**']).id)
