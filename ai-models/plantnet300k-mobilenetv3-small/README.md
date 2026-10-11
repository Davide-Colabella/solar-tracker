---
language: en
license: openrail
library_name: pytorch
tags:
  - vision
  - image-classification
  - plant-recognition
  - plantnet
datasets:
  - cpoisson/plantnet300k
metrics:
  - accuracy
---

# PlantNet-300K MobileNetV3-Small (v2 Improved)

Species recognition model used by [Solar Tracker](../../README.md); shared growing profiles are defined in `python/plants.py` at the project root.

Fine-tuned plant species classifier trained on **1,081 plant species** from the PlantNet-300K dataset.

## Model Details

- **Architecture**: MobileNetV3-Small
- **Parameters**: ~2.5M (10 MB ONNX fp32)
- **Input**: 224×224 RGB images
- **Training Dataset**: PlantNet-300K (306,406 training images, 1,081 species)
- **Version**: v2 (improved hyperparameters)

## Performance

### v2 (This Model) — Improved
| Metric | Train | Val | Test |
|--------|-------|-----|------|
| **Top-1 Accuracy** | 74.20% | 75.56% | **75.45%** ⭐ |
| **Top-5 Accuracy** | 93.17% | — | **93.81%** |
| **Loss** | 2.0298 | — | 1.9623 |

### v1 (Previous)
| Metric | Test |
|--------|------|
| **Top-1 Accuracy** | 73.89% |
| **Top-5 Accuracy** | 91.86% |

**Improvement**: +1.56 percentage points top-1 accuracy 🚀

## Training Details

**v2 Configuration** (improved):
- **Optimizer**: SGD with momentum 0.9, Nesterov=True
- **LR Schedule**: Cosine annealing from 0.01 → 1e-5
- **Augmentation**: TrivialAugment + RandomErasing
- **Class Balancing**: WeightedRandomSampler
- **Label Smoothing**: 0.1
- **Batch Size**: 256
- **Total Epochs**: 60 (Phase 1: 5 frozen, Phase 2: 55 full)

**Key Improvements**:
- Better LR schedule (cosine vs fixed)
- Stronger augmentation strategy
- Class-weighted sampling for imbalanced data
- Label smoothing for regularization

## Usage

```python
from PIL import Image
import torch
import torchvision.models as models
import torchvision.transforms as transforms

# Load model
model = models.mobilenet_v3_small(weights=None, num_classes=1081)
model.load_state_dict(torch.hub.load_state_dict_from_url(
    'https://huggingface.co/cpoisson/plantnet300k-mobilenetv3-small/resolve/main/mobilenetv3_small_v2.pth'
))
model.eval()

# Prepare image
transform = transforms.Compose([
    transforms.Resize(256),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize(
        mean=[0.485, 0.456, 0.406],
        std=[0.229, 0.224, 0.225]
    )
])

image = transform(Image.open('plant.jpg')).unsqueeze(0)

# Predict
with torch.no_grad():
    logits = model(image)
    top_k = torch.topk(logits, 5)
    probs = torch.softmax(logits, dim=1)

print(f"Top-1: {probs.max().item():.2%}")
```

## Files

- **mobilenetv3_small_v2.pth** — v2 PyTorch weights (best checkpoint from epoch 59)
- **mobilenetv3_small_v2.onnx** — v2 ONNX fp32 export (10 MB, browser-compatible)

## Dataset

**PlantNet-300K**: 306,406 high-quality plant images across 1,081 species, collected via the PlantNet mobile app.

- **Train**: 245,402 images
- **Val**: 29,892 images
- **Test**: 31,112 images

[Download Dataset](https://huggingface.co/datasets/cpoisson/plantnet300k)

## Related Resources

- **Gradio Demo**: [cpoisson/plantnet300k](https://huggingface.co/spaces/cpoisson/plantnet300k)
- **Offline Browser Demo**: [plantnet300k-offline](https://huggingface.co/spaces/cpoisson/plantnet300k-offline)
- **v1 Model**: [plantnet300k-mobilenetv3-small](https://huggingface.co/cpoisson/plantnet300k-mobilenetv3-small)
- **ResNet18 v1**: [plantnet300k-resnet18](https://huggingface.co/cpoisson/plantnet300k-resnet18)

## Citation

If you use this model, please cite:

```bibtex
@article{plantnet2017,
  title={PlantNet: A Large-Scale Continuous Ecosystem for Plant Image Classification},
  author={Cole et al.},
  year={2017}
}
```

## License

OpenRAIL — Free for research and commercial use.
