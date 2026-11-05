"""Audit a local YOLO detection export and train a small baseline."""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path

import yaml
from PIL import Image


def audit(data_file):
    """Check directory-based YOLO exports without executing YAML download hooks."""
    data_file = Path(data_file).resolve(strict=True)
    config = yaml.safe_load(data_file.read_text())
    if not isinstance(config, dict) or "download" in config:
        raise ValueError("Expected a local dataset YAML without download hooks")
    names = config.get("names")
    if isinstance(names, dict):
        if set(names) != set(range(len(names))):
            raise ValueError("Class IDs must be contiguous integers starting at zero")
        names = [names[index] for index in range(len(names))]
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ValueError("Dataset needs a nonempty names list or integer-keyed mapping")
    root = (data_file.parent / config.get("path", ".")).resolve()
    output = {"path": str(root), "names": names}
    summary = {}
    hashes = {}
    for split in ("train", "val", "test"):
        relative = config.get(split)
        if not relative:
            if split != "test":
                raise ValueError(f"Missing {split} split")
            continue
        if not isinstance(relative, str):
            raise ValueError("This baseline expects one image directory per split")
        images_dir = (root / relative).resolve()
        # Roboflow exports commonly use ../train/images in a root-level YAML.
        if not images_dir.is_dir() and relative.startswith("../"):
            images_dir = (root / relative[3:]).resolve()
        if not images_dir.is_dir() or images_dir.name != "images":
            raise ValueError(f"{split}: expected an existing images directory: {images_dir}")
        images = sorted(p for p in images_dir.rglob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"})
        if not images:
            raise ValueError(f"{split}: no images found")
        counts = Counter()
        backgrounds = 0
        for image in images:
            with Image.open(image) as decoded:
                decoded.verify()
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            if digest in hashes and hashes[digest][0] != split:
                raise ValueError(f"Image duplicated across splits: {image} and {hashes[digest][1]}")
            hashes[digest] = (split, str(image))
            label = images_dir.parent / "labels" / image.relative_to(images_dir).with_suffix(".txt")
            if not label.is_file():
                raise ValueError(f"Missing label: {label}; use an empty file for a verified background")
            lines = label.read_text().splitlines()
            backgrounds += not any(line.strip() for line in lines)
            for number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                values = line.split()
                if len(values) != 5:
                    raise ValueError(f"{label}:{number}: expected class x_center y_center width height")
                class_id = int(values[0])
                x, y, width, height = map(float, values[1:])
                if not 0 <= class_id < len(names) or not all(math.isfinite(v) for v in (x, y, width, height)):
                    raise ValueError(f"{label}:{number}: invalid class or non-finite box")
                if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < width <= 1 and 0 < height <= 1):
                    raise ValueError(f"{label}:{number}: box coordinates must be normalized")
                if min(x - width / 2, y - height / 2) < -0.001 or max(x + width / 2, y + height / 2) > 1.001:
                    raise ValueError(f"{label}:{number}: box extends beyond image")
                counts[names[class_id]] += 1
        if split in {"train", "val"} and not counts:
            raise ValueError(f"{split}: no annotated objects")
        summary[split] = {"images": len(images), "backgrounds": backgrounds, "objects": dict(counts)}
        output[split] = str(images_dir)
    return output, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("audit", "train", "validate"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--model", default="yolo11n.pt")
    parser.add_argument("--device", default="mps", help="mps on Apple Silicon; cpu or 0 for other machines")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--imgsz", type=int, default=512)
    parser.add_argument("--batch", type=int, default=2)
    parser.add_argument("--name", default="usv2-baseline")
    parser.add_argument("--smoke", action="store_true", help="One epoch to verify the training path")
    args = parser.parse_args()
    config, summary = audit(args.data)
    print(json.dumps({"classes": config["names"], "splits": summary}, indent=2))
    if args.action == "audit":
        return
    if min(args.epochs, args.imgsz, args.batch) < 1:
        parser.error("epochs, imgsz and batch must be positive")
    os.environ.setdefault("YOLO_CONFIG_DIR", str(Path(__file__).resolve().parent / ".ultralytics"))
    from ultralytics import YOLO

    output = Path(__file__).resolve().parent / "runs"
    output.mkdir(exist_ok=True)
    normalized = output / "dataset.yaml"
    normalized.write_text(yaml.safe_dump(config, sort_keys=False))
    model = YOLO(args.model)
    common = dict(data=str(normalized), device=args.device, imgsz=args.imgsz,
                  batch=args.batch, workers=0, project=str(output), name=args.name)
    if args.action == "train":
        model.train(**common, epochs=1 if args.smoke else args.epochs,
                    cache=False, amp=False, seed=42, patience=10, close_mosaic=0 if args.smoke else 10)
    else:
        model.val(**common, split="val")


if __name__ == "__main__":
    main()
