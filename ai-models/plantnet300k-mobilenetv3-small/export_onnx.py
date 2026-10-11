#!/usr/bin/env python3
"""
export_onnx.py  ·  PlantNet-300K — PyTorch → ONNX export
=========================================================
Exports both MobileNetV3-Small and ResNet18 to:
  - Full float32 ONNX  (faithful to training)
  - INT8 dynamically quantized ONNX  (~3x smaller, ~0.5-1pp accuracy drop)

Outputs (written next to this script):
  plantnet_mobilenetv3.onnx
  plantnet_mobilenetv3_int8.onnx
  plantnet_resnet18.onnx
  plantnet_resnet18_int8.onnx

Requirements:
  pip install torch torchvision onnx onnxruntime
"""

import json
from pathlib import Path

import torch
import torchvision.models as models
from onnxruntime.quantization import QuantType, quantize_dynamic

MODELS_DIR  = Path(__file__).parent
OUT_DIR     = Path(__file__).parent
NUM_CLASSES = 1081
OPSET       = 17
DUMMY_INPUT = torch.randn(1, 3, 224, 224)

EXPORTS = [
    {
        "name":    "mobilenet_v3_small",
        "weights": MODELS_DIR / "plantnet_mobilenetv3.pth",
        "out_fp32": OUT_DIR  / "plantnet_mobilenetv3.onnx",
        "out_int8": OUT_DIR  / "plantnet_mobilenetv3_int8.onnx",
        "build": lambda: _mobilenet(),
    },
    {
        "name":    "resnet18",
        "weights": MODELS_DIR / "plantnet_resnet18.pth",
        "out_fp32": OUT_DIR  / "plantnet_resnet18.onnx",
        "out_int8": OUT_DIR  / "plantnet_resnet18_int8.onnx",
        "build": lambda: _resnet18(),
    },
]


def _mobilenet():
    m = models.mobilenet_v3_small(weights=None, num_classes=NUM_CLASSES)
    return m


def _resnet18():
    m = models.resnet18(weights=None, num_classes=NUM_CLASSES)
    return m


def export(cfg: dict):
    name     = cfg["name"]
    weights  = cfg["weights"]
    out_fp32 = cfg["out_fp32"]
    out_int8 = cfg["out_int8"]

    print(f"\n{'='*60}")
    print(f"Exporting {name}")
    print(f"{'='*60}")

    # ── Load model ────────────────────────────────────────────
    model = cfg["build"]()
    model.load_state_dict(
        torch.load(weights, map_location="cpu", weights_only=True)
    )
    model.eval()
    print(f"  Loaded weights from {weights}")

    # ── Export fp32 ONNX ──────────────────────────────────────
    torch.onnx.export(
        model,
        DUMMY_INPUT,
        str(out_fp32),
        input_names=["image"],
        output_names=["logits"],
        dynamic_axes={"image": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=OPSET,
    )
    size_fp32 = out_fp32.stat().st_size / 1e6
    print(f"  ✓ fp32  → {out_fp32.name}  ({size_fp32:.1f} MB)")

    # ── Verify fp32 with onnxruntime ──────────────────────────
    import onnxruntime as ort
    import numpy as np
    sess = ort.InferenceSession(str(out_fp32), providers=["CPUExecutionProvider"])
    out  = sess.run(None, {"image": DUMMY_INPUT.numpy()})
    assert out[0].shape == (1, NUM_CLASSES), f"Unexpected output shape: {out[0].shape}"
    print(f"  ✓ fp32 inference check passed  (output shape: {out[0].shape})")

    # ── Export INT8 dynamic quantization ─────────────────────
    quantize_dynamic(
        str(out_fp32),
        str(out_int8),
        weight_type=QuantType.QInt8,
    )
    size_int8 = out_int8.stat().st_size / 1e6
    print(f"  ✓ int8  → {out_int8.name}  ({size_int8:.1f} MB)  "
          f"({100*(1 - size_int8/size_fp32):.0f}% smaller)")

    # ── Verify INT8 ───────────────────────────────────────────
    sess8 = ort.InferenceSession(str(out_int8), providers=["CPUExecutionProvider"])
    out8  = sess8.run(None, {"image": DUMMY_INPUT.numpy()})
    assert out8[0].shape == (1, NUM_CLASSES)
    print(f"  ✓ int8 inference check passed")

    return {
        "fp32_mb": round(size_fp32, 1),
        "int8_mb": round(size_int8, 1),
    }


if __name__ == "__main__":
    results = {}
    for cfg in EXPORTS:
        results[cfg["name"]] = export(cfg)

    print(f"\n{'='*60}")
    print("Summary")
    print(f"{'='*60}")
    for name, r in results.items():
        print(f"  {name:30s}  fp32={r['fp32_mb']} MB  int8={r['int8_mb']} MB")
    print("\nAll exports complete.")
