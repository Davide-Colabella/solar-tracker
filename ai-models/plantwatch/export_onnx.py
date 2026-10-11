"""Export a trained PlantWatch checkpoint to ONNX or classify a photo with an existing ONNX."""
import argparse
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models" / "houseplants"
CLASSES = ["healthy", "wilted"]


def make_model(pretrained=False):
    from torch import nn
    from torchvision import models

    weights = models.MobileNet_V3_Small_Weights.DEFAULT if pretrained else None
    model = models.mobilenet_v3_small(weights=weights)
    model.classifier[3] = nn.Linear(model.classifier[3].in_features, len(CLASSES))
    return model


def load_checkpoint(path):
    import torch

    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    if checkpoint["classes"] != CLASSES:
        raise ValueError("Incompatible checkpoint classes")
    model = make_model()
    model.load_state_dict(checkpoint["state_dict"])
    return model.eval(), checkpoint


def export_model(checkpoint_path, output_path):
    import onnx
    import torch

    torch.set_num_threads(2)
    model, checkpoint = load_checkpoint(checkpoint_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.randn(1, 3, 224, 224)
    torch.onnx.export(model, (dummy,), str(output), input_names=["image"],
                      output_names=["logits"], opset_version=17, dynamo=False)
    onnx.checker.check_model(onnx.load(str(output)))
    session = ort.InferenceSession(str(output), providers=["CPUExecutionProvider"])
    for seed in range(3):
        generator = torch.Generator().manual_seed(seed)
        sample = torch.randn(1, 3, 224, 224, generator=generator)
        with torch.inference_mode():
            expected = model(sample).numpy()
        actual = session.run(None, {"image": sample.numpy()})[0]
        np.testing.assert_allclose(actual, expected, rtol=1e-4, atol=1e-5)
    metadata = {"classes": checkpoint["classes"], "preprocessing": checkpoint["preprocessing"],
                "input": "image", "output": "logits"}
    output.with_suffix(".json").write_text(json.dumps(metadata, indent=2))
    print(f"ONNX verified: {output} ({output.stat().st_size / 1024**2:.2f} MiB)")


def predict_image(model_path, image_path):
    """Run offline inference without exporting a model or importing PyTorch."""
    model_path = Path(model_path)
    metadata = json.loads(model_path.with_suffix(".json").read_text())
    expected = {"size": 224, "mean": [0.485, 0.456, 0.406],
                "std": [0.229, 0.224, 0.225], "resize": "bilinear"}
    if metadata["classes"] != CLASSES or metadata["preprocessing"] != expected:
        raise ValueError("Model metadata is incompatible with RGB preprocessing")
    with Image.open(image_path) as photo:
        photo = ImageOps.exif_transpose(photo).convert("RGB")
        photo = photo.resize((224, 224), Image.Resampling.BILINEAR)
        image = np.asarray(photo, dtype=np.float32) / 255.0
    image = (image - np.array(expected["mean"], dtype=np.float32)) / np.array(expected["std"], dtype=np.float32)
    image = np.ascontiguousarray(image.transpose(2, 0, 1), dtype=np.float32)[None]
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    logits = session.run([metadata["output"]], {metadata["input"]: image})[0][0]
    scores = np.exp(logits - logits.max())
    scores /= scores.sum()
    result = {"condition": CLASSES[int(scores.argmax())],
              "scores": dict(zip(CLASSES, map(float, scores)))}
    print(f"Prediction: {result['condition']}")
    for label, score in result["scores"].items():
        print(f"{label}: {score:.1%}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=MODELS / "plantwatch.pth")
    parser.add_argument("--output", type=Path, default=MODELS / "plantwatch.onnx")
    parser.add_argument("--image", type=Path, help="Classify a photo using an existing ONNX; skip export")
    parser.add_argument("--model", type=Path, default=MODELS / "plantwatch.onnx",
                        help="ONNX used with --image; defaults to the Houseplants model")
    args = parser.parse_args()
    try:
        if args.image is not None:
            predict_image(args.model, args.image)
        else:
            export_model(args.checkpoint, args.output)
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
