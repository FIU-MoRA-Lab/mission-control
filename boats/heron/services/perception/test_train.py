"""Dataset boundary checks, independent of model downloads and GPU access."""
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from train import audit


class DatasetAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "data.yaml"
        self.data.write_text("train: ../train/images\nval: ../valid/images\nnames: [Civilian, Military]\n")
        for split, color in (("train", "red"), ("valid", "blue")):
            (self.root / split / "images").mkdir(parents=True)
            (self.root / split / "labels").mkdir()
            Image.new("RGB", (32, 32), color).save(self.root / split / "images" / "sample.png")
            (self.root / split / "labels" / "sample.txt").write_text("0 0.5 0.5 0.25 0.25\n")

    def test_roboflow_relative_paths_and_classes(self):
        config, summary = audit(self.data)
        self.assertEqual(config["names"], ["Civilian", "Military"])
        self.assertEqual(config["val"], str(self.root / "valid/images"))
        self.assertEqual(summary["train"]["objects"], {"Civilian": 1})

    def test_duplicate_across_splits_rejected(self):
        (self.root / "valid/images/sample.png").write_bytes((self.root / "train/images/sample.png").read_bytes())
        with self.assertRaisesRegex(ValueError, "duplicated across splits"):
            audit(self.data)

    def test_invalid_box_rejected(self):
        (self.root / "train/labels/sample.txt").write_text("0 0.95 0.5 0.4 0.2\n")
        with self.assertRaisesRegex(ValueError, "beyond image"):
            audit(self.data)

    def test_download_hooks_rejected(self):
        self.data.write_text(self.data.read_text() + "download: untrusted-command\n")
        with self.assertRaisesRegex(ValueError, "download hooks"):
            audit(self.data)


if __name__ == "__main__":
    unittest.main()
