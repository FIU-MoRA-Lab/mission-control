"""Onboard service install, isolated capture ownership, and failure handling."""
import configparser
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from boats.heron.backend.camera_service import INSTALL, configuration, encoder_command, install_payload, serve, ssh_command


class CameraServiceTests(unittest.TestCase):
    def test_remote_install_writes_configuration_and_enables_service(self):
        config = configuration(device="/dev/video2", port=5602, fps=25)
        payload = install_payload(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("pathlib.Path", side_effect=lambda path: root / path.lstrip("/")), \
                    patch("sys.stdin", StringIO(payload.decode())), patch("subprocess.run") as run:
                exec(INSTALL, {})
            self.assertEqual(json.loads((root / "etc/mission-control/camera.json").read_text()), config)
            self.assertTrue((root / "etc/systemd/system/mission-control-camera.service").is_file())
            self.assertTrue((root / "usr/local/lib/mission-control/camera_service.py").is_file())
            self.assertEqual([call.args[0][1] for call in run.call_args_list],
                             ["daemon-reload", "enable", "restart", "status"])

    def test_installed_unit_recovers_after_boot_camera_loss_and_stops_owned_container(self):
        payload = json.loads(install_payload(configuration()))
        unit = configparser.ConfigParser(interpolation=None)
        unit.read_string(payload["unit"])
        self.assertEqual(unit["Service"]["Restart"], "always")
        self.assertEqual(unit["Unit"]["StartLimitIntervalSec"], "0")
        self.assertIn("docker.service", unit["Unit"]["After"])
        self.assertIn("mission-control-camera", unit["Service"]["ExecStop"])
        self.assertIn("rm --force mission-control-camera", unit["Service"]["ExecStopPost"])
        self.assertEqual(unit["Install"]["WantedBy"], "multi-user.target")
        compile(payload["program"], "camera_service.py", "exec")

    def test_capture_owns_a_separate_container_and_only_the_selected_camera(self):
        config = configuration(device="/dev/v4l/by-id/usb-webcam", port=5602, fps=25)
        args = encoder_command(config, "sha256:" + "a" * 64)
        self.assertEqual(args[1], "run")
        self.assertNotIn("exec", args)
        self.assertEqual(args[args.index("--device") + 1], "/dev/v4l/by-id/usb-webcam:/dev/video0:rw")
        self.assertNotIn("--privileged", args)
        self.assertFalse(any("docker.sock" in arg or "ttyAMA" in arg for arg in args))
        self.assertIn("port=5602", args)
        self.assertIn("video/x-raw,width=640,height=480,framerate=25/1", args)
        self.assertIn("key-int-max=25", args)

    def test_older_onboard_configuration_uses_new_default_frame_rate(self):
        config = configuration(**dict(device="/dev/video0", container="blueos-core", port=5601))
        self.assertEqual(config["fps"], 30)

    def test_missing_core_image_fails_for_supervisor_to_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.json"
            path.write_text(json.dumps(configuration()))
            with patch("boats.heron.backend.camera_service.subprocess.run", side_effect=subprocess.CalledProcessError(1, "docker")) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    serve(path)
                self.assertEqual(run.call_count, 1)

    def test_resolves_image_and_propagates_encoder_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.json"
            path.write_text(json.dumps(configuration()))
            with patch("boats.heron.backend.camera_service.subprocess.run", side_effect=[
                SimpleNamespace(stdout="sha256:" + "b" * 64 + "\n"), SimpleNamespace(returncode=42)
            ]) as run:
                self.assertEqual(serve(path), 42)
                self.assertIn("sha256:" + "b" * 64, run.call_args.args[0])

    def test_rejects_invalid_configuration_and_ssh_options(self):
        for config in [dict(device="/etc/passwd"), dict(device="/dev/video0:/dev/ttyAMA0"),
                       dict(container="mission-control-camera"), dict(container="core; reboot"), dict(port=0),
                       dict(fps=0), dict(fps=61), dict(fps=True), dict(fps=29.97)]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                configuration(**config)
        for host in ["-oProxyCommand=other", "pi@boat extra", ""]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                ssh_command(host, "status")
        self.assertEqual(ssh_command("boat-alias", "status")[-2:], ["boat-alias", "status"])


if __name__ == "__main__":
    unittest.main()
