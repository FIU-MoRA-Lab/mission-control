# Ground-station perception boats/heron/experiments

This is an independent Python environment. It does not load models into the
Mission Control backend or issue vehicle commands. The first experiment is a
small YOLO11n bounding-box detector; segmentation and distance estimation are
separate future work.

## Dataset status

Candidate: [USV2 version 1](https://universe.roboflow.com/usv-f5cxy/usv2/dataset/1),
published by USV on Roboflow under CC BY 4.0. Attribute the source when using it.
The page lists 413 images: 289 train, 83 validation, 41 test; two classes,
`Civilian` and `Military`; preprocessing stretches images to 512 × 512.

Inspection on 2026-10-21: locally extracted training samples show rendered water
scenes and conventional vessels, rather than a verified collection of target USVs.
The full export passes image decoding, label and exact-duplicate checks: train
contains 176 Civilian / 211 Military boxes, validation 55 / 60, and test 29 / 24.
There are no background-only images in any split. Nearby
numbered video frames appear in different splits, so temporal leakage is a
concern even if exact files differ. The author provides no dataset description.
Do not interpret these classes as verified USV identities or rename them to
`usv` without examining the full collection. Treat this as a pipeline experiment,
not evidence of real-world USV detection accuracy. Roboflow requires sign-in to
export the data. The local export is under `data/usv2/`; datasets and checkpoints
are not committed to Git.

## Setup and export

### First local smoke run (2026-10-21)

Completed one epoch on Apple M1 / MPS with YOLO11n, batch 2 and 512-pixel input.
The epoch plus its validation took about 45 seconds (excluding environment setup,
model download and startup). Final validation reported 14.6 ms inference and
8.5 ms postprocessing per image, using validation batches of four. This is not a
single-frame live camera benchmark, and Mission Control was not benchmarked
alongside it. Validation mAP50 was only 0.00733 after this one epoch; the smoke
checkpoint is not a useful detector. Test images were audited but not evaluated.
Artifacts are under `runs/usv2-smoke/`, including `weights/best.pt`, `results.csv`,
and validation plots. `runs/usv2-training-samples.jpg` shows sampled training
images with their boxes. The pipeline works; this dataset is not verified as
suitable for the intended real-world small-USV targets.

### Full baseline (2026-10-21)

Ran the 50-epoch configuration with early stopping; it completed 33 epochs in
749 seconds (about 12.5 minutes) and selected epoch 23. Best validation mAP50 was
about 81.6%, with mAP50–95 52.2%. Evaluated the selected checkpoint once on the
41-image / 53-object test split: mAP50 82.4%, mAP50–95 53.6%, precision 92.0%,
recall 69.5% at the evaluator's selected operating point. These are within-dataset
results with possible temporal leakage, not verified real-USV field performance.
The qualitative validation sample includes predictions on largely occluded
targets/waves, so score alone does not establish useful avoidance observations.

The checkpoint is `runs/usv2-baseline/weights/best.pt` (about 5.2 MiB).
Machine-readable results are in `runs/usv2-baseline/summary.json`, training history
in `results.csv`, and the rendered sample in `validation-predictions.jpg`.
Test plots are under `runs/usv2-baseline-test/`. Test evaluation with batch 2
reported 22.0 ms inference, 1.7 ms preprocessing and 7.5 ms postprocessing per
image. This is an offline batched benchmark without the camera, network or
Mission Control running alongside it. No depth, segmentation or avoidance model
was trained or integrated.

From this directory:

```sh
uv sync --locked
```

Open the dataset link, sign in, and export in YOLOv11 format. Extract it into
`data/usv2/` so `data/usv2/data.yaml`, `train/images`, `train/labels`,
`valid/images`, and `valid/labels` exist. Preserve its README and attribution.
Images, environments, weights, and run outputs are ignored by Git.

## Audit, smoke test, train, validate

```sh
uv run train.py audit --data data/usv2/data.yaml
uv run train.py train --data data/usv2/data.yaml --smoke --name usv2-smoke
uv run train.py train --data data/usv2/data.yaml
uv run train.py validate --data data/usv2/data.yaml --model runs/usv2-baseline/weights/best.pt --name usv2-validation
```

Defaults: pretrained YOLO11n, MPS, 512-pixel input, batch 2, zero loader workers,
no RAM cache, mixed precision disabled, 50 epochs with early stopping. These are
starting settings for an 8 GB Apple Silicon Mac, not measured performance claims.
Close other memory-heavy apps and reduce batch to 1 if necessary. Use
`--device cpu` or `--device 0` for CPU or CUDA. Initial training downloads the
pretrained checkpoint. Ultralytics uses AGPL-3.0 or its enterprise license; review
[its licensing](https://www.ultralytics.com/license) for your intended distribution.

The audit checks normalized bounding boxes, missing labels and exact duplicate
images across splits. It preserves class names and writes a local, absolute-path
dataset YAML without download hooks. It cannot detect all near-duplicates or
verify class meaning. Before reporting accuracy, group data by source video and
keep complete sequences in one split; evaluate on separate real camera outings.
Keep the test split untouched during tuning. Training results, plots and weights
are saved under `runs/`; if a run name already exists, Ultralytics may suffix it.

For a real USV detector, add labeled target-USV camera footage, including empty
water and confusing objects, with sequence-separated validation/test sets. Range
estimation needs calibration and measured-distance validation beyond this dataset.

References: [YOLO11](https://docs.ultralytics.com/models/yolo11/),
[training including MPS](https://docs.ultralytics.com/modes/train/).
