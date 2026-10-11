# PlantWatch

Visual plant condition model for [Solar Tracker](../../README.md).

PlantWatch classifies houseplant photos as `healthy` or `wilted` to support care alerts, watering decisions and shading adjustments. MobileNetV3-Small starts from ImageNet weights; its feature extractor is frozen and its classifier is trained on [bhargob11/houseplants](https://huggingface.co/datasets/bhargob11/houseplants). Visual wilting should be combined with sensor readings before deciding on irrigation.

The source dataset contains 971 images. Removing 51 exact duplicates and excluding 4 images with contradictory labels leaves **916 usable images**, split into **642 training, 137 validation and 137 test images**. The best checkpoint is selected using validation accuracy and evaluated on the test split after training.

| Characteristic | Value |
| --- | --- |
| Architecture | MobileNetV3-Small |
| Output classes | 2: `healthy`, `wilted` |
| Test accuracy | **79.56%** — 109/137 |
| Recall for `wilted` | **85.29%** — 58/68 |
| ONNX file size | **6.09 MB** |

The ONNX export was checked against PyTorch and produced the same confusion matrix on all test images. Execution on Arduino UNO Q remains to be tested.

## Usage

Run these commands from this folder. The dataset and trained model are already prepared.

```bash
# Classify a photo with the saved ONNX.
.venv/bin/python export_onnx.py --image "/path/to/photo.jpg"

# Train on the existing splits and evaluate the best checkpoint.
.venv/bin/python train.py --device cpu

# Export the checkpoint and check agreement with PyTorch.
.venv/bin/python export_onnx.py
```

For a fresh environment, run `python3 -m venv .venv` and `.venv/bin/python -m pip install -r requirements.txt`. To recreate the dataset, choose a new destination; preparation refuses to overwrite an existing folder:

```bash
.venv/bin/python train.py --prepare-only \
  --revision 0ff49e9f502af32b3204455e65166f409ffb1553 \
  --dataset datasets/houseplants-new
```

Use `--dataset datasets/houseplants-new` when training or evaluating that recreated dataset. `train.py --check-only` audits images and split overlap; `--evaluate-only` evaluates the saved checkpoint. Plant/session identities are unavailable, so visually similar photos may remain across splits.

Model files, [test metrics](models/houseplants/test_metrics.json) and [inference metadata](models/houseplants/plantwatch.json) are in `models/houseplants/`. Dataset provenance and exclusions are recorded in `datasets/houseplants/manifest.json`. Prediction scores are not calibrated probabilities.
