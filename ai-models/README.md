# Solar Tracker — AI for Plant Care

The project uses a **movable solar panel above one or more plants** to protect them from adverse weather and regulate sunlight exposure, especially during hot periods. Images and sensor readings will support the system's decisions, with the AI models intended to run on **Arduino UNO Q**.

## What the models do

**PlantWatch — visual plant condition (`ai-models/plantwatch/`).** This model uses MobileNetV3-Small pretrained on ImageNet and adapted to the [Houseplants dataset](https://huggingface.co/datasets/bhargob11/houseplants). It distinguishes `healthy` from `wilted` in houseplant photos to support care alerts, watering decisions, or shading adjustments. A `wilted` result should be combined with sensor readings: it does not establish a need for water on its own.

**Species recognition — `ai-models/plantnet300k-mobilenetv3-small/`.** This model was already trained on PlantNet-300K and is reused to identify the plant. The recognized species can select a growing profile: for example, allowing more sunlight for a plant with higher light requirements or providing more shade for one with lower requirements. The application maps recognized genera to shared growing profiles in `python/plants.py`.

## Main characteristics

| Characteristic | PlantWatch | Species recognition |
| --- | --- | --- |
| Architecture | MobileNetV3-Small | MobileNetV3-Small |
| Dataset | Houseplants, 916 prepared images | PlantNet-300K |
| Output classes | 2: `healthy`, `wilted` | 1,081 species |
| Test accuracy | **79.56%** — 109/137 images | **75.45% Top-1**, **93.81% Top-5** — reported v2 results |
| ONNX file size | **6.09 MB** | **10.51 MB** FP32; **2.80 MB** INT8 |

Top-1 means the first prediction is correct; Top-5 means the correct species is among the five highest-ranked predictions.

## Folders and usage

The tree, code paths and photo command below are relative to the repository root.

```text
solar-tracker/
├── README.md                         # application and hardware documentation
├── app.yaml                          # Arduino App Lab configuration
├── assets/index.html                 # web interface
├── python/                           # Linux app, profiles, and simulation
├── sketch/                           # microcontroller firmware
└── ai-models/
    ├── README.md                    # AI models overview
    ├── plantwatch/                   # Houseplants dataset, health model, and scripts
    └── plantnet300k-mobilenetv3-small/ # pretrained species classifier
```

The visual condition pipeline uses two scripts: [train.py](plantwatch/train.py) handles dataset preparation, training, and evaluation; [export_onnx.py](plantwatch/export_onnx.py) exports a saved checkpoint to ONNX, checks numerical agreement with PyTorch, and can classify a photo with an existing ONNX. The exported model is in `ai-models/plantwatch/models/houseplants/`; the [model README](plantwatch/README.md) provides preparation and training commands.

`train.py` trains by default and evaluates the best checkpoint if a test split is present. Use `--prepare-only` to download and prepare Houseplants (`--source` selects the Hugging Face dataset ID and `--dataset` the new destination), `--check-only` to audit it, or `--evaluate-only` to evaluate a saved model. Use `--output` for a new training checkpoint, `--checkpoint` for evaluation/export, and `--metrics-output` for evaluation results. Both scripts provide `--help`.

To test a photo from the repository root, use the project's Python environment:

```bash
ai-models/plantwatch/.venv/bin/python ai-models/plantwatch/export_onnx.py --image "/path/to/photo.jpg"
```

This prints the predicted class and both scores without exporting or modifying the model. Use `--model` to select another ONNX with its matching JSON metadata. Scores are not calibrated probabilities.

For species recognition, the [dedicated README](plantnet300k-mobilenetv3-small/README.md) provides a PyTorch example. The application already loads ONNX models through `python/colleague_model.py`, with filenames, labels and preprocessing configured in `python/config.py`; growing profiles are defined in `python/plants.py`.

On UNO Q, inference is intended to run on Linux, with sensors and actuators connected to the control logic. The application and firmware are already present; deployment of the imported PlantWatch model on the board remains to be verified. The species classifier's FP32 ONNX ran locally; the INT8 version encountered a compatibility error with the installed runtime.

The control design combines **species → growing profile**, **visual condition**, and **sensors → potential intervention**. For example, persistent stress and dry soil can support a watering request, while the species profile and environmental conditions can guide shading.

Supporting files: [Houseplants test results](plantwatch/models/houseplants/test_metrics.json), [health model metadata](plantwatch/models/houseplants/plantwatch.json), and [species classifier documentation](plantnet300k-mobilenetv3-small/README.md).

## Application integration status

The app loads PlantWatch from `python/models/plantwatch.onnx`, with a matching `plantwatch.json` metadata file. Output 0 is `healthy`, output 1 is `wilted`; the health classifier uses direct bilinear resizing to 224×224 and ImageNet normalization. The species classifier retains its short-side resize to 256 and center crop to 224. Uploaded photos are oriented using EXIF before inference. `python/requirements.txt` includes Pillow for matching PlantWatch's preprocessing.

The runtime PlantWatch artifacts are copies of `ai-models/plantwatch/models/houseplants/plantwatch.onnx` and its JSON metadata. After retraining and exporting PlantWatch, copy both files into `python/models/` to deploy updated weights. The species ONNX in `python/models/` matches the imported species ONNX.
