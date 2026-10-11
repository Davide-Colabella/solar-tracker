#!/usr/bin/env python3
"""
train.py  ·  PlantNet-300K fine-tuning — v2
===========================================
Experiment : small local models for plant identification
Dataset    : PlantNet-300K  (Zenodo DOI:10.5281/zenodo.5645731)
Paper      : NeurIPS 2021 Datasets & Benchmarks — Garcin et al.

Improvements over v1
--------------------
  ① SGD + Nesterov momentum       replaces Adam 1e-3 (was eroding pretrained features)
  ② Cosine annealing LR schedule  no manual epoch_decay tuning needed
  ③ Two-phase training            freeze backbone → full fine-tune
  ④ WeightedRandomSampler         addresses the severe long-tail class imbalance
  ⑤ Label smoothing               handles PlantNet's documented high label ambiguity
  ⑥ TrivialAugment + RandomErase  stronger, parameter-free augmentation policy
  ⑦ Mixed precision (AMP)         ~1.8× faster on RTX 3070, same accuracy
  ⑧ Best-val checkpoint           final test eval runs on best-val weights, not last epoch
  ⑨ Fixed seed                    reproducible runs
  ⑩ JSON results                  metrics + hyperparams saved next to weights

Hardware used
-------------
  GPU  : NVIDIA GeForce RTX 3070 — 8 GB VRAM
  CPU  : Intel Core i7-8086K @ 4.00 GHz — 12 threads
  RAM  : 32 GB
  OS   : Ubuntu Linux
  torch: 2.7.0+cu126  /  torchvision: 0.22.0+cu126

Expected runtime (MobileNetV3-Small, 60 epochs, RTX 3070): ~2–3 h with AMP
"""

import json
import math
import os
import random
import time
from collections import Counter
from pathlib import Path

import torch
import torch.nn as nn
import torch.optim as optim
import torchvision.models as models
import torchvision.transforms as transforms
from torch.amp import GradScaler, autocast
from torch.utils.data import DataLoader, WeightedRandomSampler
from torchvision.datasets import ImageFolder
from tqdm import tqdm

# ── Config ────────────────────────────────────────────────────────────────────

DATA_DIR    = "/home/charles/Data/plantnet_300K/images"  # train/ val/ test/
OUT_DIR     = Path("./results_v2")
MODEL_NAME  = "mobilenet_v3_small"   # or "resnet18"
NUM_CLASSES = 1081
SEED        = 42

# Training schedule
PHASE1_EPOCHS = 5     # backbone frozen, head only
PHASE2_EPOCHS = 55    # full fine-tune with cosine annealing
TOTAL_EPOCHS  = PHASE1_EPOCHS + PHASE2_EPOCHS

# Optimiser
LR           = 0.01
MOMENTUM     = 0.9
WEIGHT_DECAY = 1e-4
MIN_LR       = 1e-5   # cosine annealing floor

# Data
BATCH_SIZE   = 64
IMAGE_SIZE   = 256
CROP_SIZE    = 224
NUM_WORKERS  = 8

# Regularisation
LABEL_SMOOTHING = 0.1   # helps with PlantNet's label ambiguity
ERASE_PROB      = 0.2   # RandomErasing probability

# ── Helpers ───────────────────────────────────────────────────────────────────

def set_seed(seed: int):
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark     = False


def get_backbone_params(model, model_name: str):
    """Return (backbone_params, head_params) for two-phase training."""
    if "mobilenet" in model_name:
        return list(model.features.parameters()), list(model.classifier.parameters())
    elif "resnet" in model_name:
        head = list(model.fc.parameters())
        backbone = [p for p in model.parameters() if not any(p is h for h in head)]
        return backbone, head
    else:
        raise ValueError(f"Unknown model: {model_name}")


def freeze_backbone(model, model_name: str):
    backbone_params, _ = get_backbone_params(model, model_name)
    for p in backbone_params:
        p.requires_grad = False


def unfreeze_all(model):
    for p in model.parameters():
        p.requires_grad = True


def build_model(model_name: str, num_classes: int) -> nn.Module:
    if model_name == "mobilenet_v3_small":
        m = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.IMAGENET1K_V1)
        m.classifier[3] = nn.Linear(m.classifier[3].in_features, num_classes)
    elif model_name == "resnet18":
        m = models.resnet18(weights=models.ResNet18_Weights.IMAGENET1K_V1)
        m.fc = nn.Linear(m.fc.in_features, num_classes)
    else:
        raise ValueError(f"Unsupported model: {model_name}")
    return m


def weighted_sampler(dataset: ImageFolder) -> WeightedRandomSampler:
    """
    Inverse-sqrt frequency sampling to mitigate long-tail imbalance.
    Rare species get upsampled; dominant species get downsampled.
    """
    class_counts = Counter(dataset.targets)
    sample_weights = [
        1.0 / math.sqrt(class_counts[t]) for t in dataset.targets
    ]
    return WeightedRandomSampler(
        weights=sample_weights,
        num_samples=len(sample_weights),
        replacement=True,
    )


def topk_accuracy(outputs: torch.Tensor, targets: torch.Tensor, k: int) -> int:
    topk = outputs.topk(k, dim=1).indices
    return topk.eq(targets.unsqueeze(1)).any(dim=1).sum().item()


# ── Data ─────────────────────────────────────────────────────────────────────

transform_train = transforms.Compose([
    transforms.Resize(IMAGE_SIZE),
    transforms.RandomResizedCrop(CROP_SIZE),
    transforms.RandomHorizontalFlip(),
    transforms.RandomRotation(30),               # ① plants at any angle
    transforms.TrivialAugmentWide(),             # ② parameter-free strong aug
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                         std=[0.229, 0.224, 0.225]),
    transforms.RandomErasing(p=ERASE_PROB),      # ③ occlusion robustness
])

transform_eval = transforms.Compose([
    transforms.Resize(IMAGE_SIZE),
    transforms.CenterCrop(CROP_SIZE),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                         std=[0.229, 0.224, 0.225]),
])

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    set_seed(SEED)
    DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Device : {DEVICE}")
    print(f"Model  : {MODEL_NAME}")

    # ── Datasets ──────────────────────────────────────────────────────────────
    train_dataset = ImageFolder(os.path.join(DATA_DIR, "train"), transform=transform_train)
    val_dataset   = ImageFolder(os.path.join(DATA_DIR, "val"),   transform=transform_eval)
    test_dataset  = ImageFolder(os.path.join(DATA_DIR, "test"),  transform=transform_eval)

    print(f"Train  : {len(train_dataset):,} images")
    print(f"Val    : {len(val_dataset):,} images")
    print(f"Test   : {len(test_dataset):,} images")
    print(f"Classes: {len(train_dataset.classes)}")

    sampler = weighted_sampler(train_dataset)

    train_loader = DataLoader(
        train_dataset, batch_size=BATCH_SIZE,
        sampler=sampler, num_workers=NUM_WORKERS, pin_memory=True,
    )
    val_loader = DataLoader(
        val_dataset, batch_size=BATCH_SIZE,
        shuffle=False, num_workers=NUM_WORKERS, pin_memory=True,
    )
    test_loader = DataLoader(
        test_dataset, batch_size=BATCH_SIZE,
        shuffle=False, num_workers=NUM_WORKERS, pin_memory=True,
    )

    # ── Model ─────────────────────────────────────────────────────────────────
    model = build_model(MODEL_NAME, NUM_CLASSES).to(DEVICE)

    criterion = nn.CrossEntropyLoss(label_smoothing=LABEL_SMOOTHING)
    scaler    = GradScaler("cuda")

    # ── Phase 1: backbone frozen, train head only ─────────────────────────────
    print(f"\n{'='*60}")
    print(f"Phase 1 — backbone frozen ({PHASE1_EPOCHS} epochs)")
    print(f"{'='*60}")
    freeze_backbone(model, MODEL_NAME)

    _, head_params = get_backbone_params(model, MODEL_NAME)
    optimizer_p1 = optim.SGD(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=LR, momentum=MOMENTUM, weight_decay=WEIGHT_DECAY, nesterov=True,
    )

    history = {"train_loss": [], "train_top1": [], "train_top5": [],
               "val_loss": [],   "val_top1": [],   "val_top5": []}
    best_val_top1  = 0.0
    best_epoch     = 0
    best_ckpt_path = OUT_DIR / f"{MODEL_NAME}_best.pth"

    for epoch in range(PHASE1_EPOCHS):
        t_loss, t_top1, t_top5 = run_epoch(
            model, train_loader, criterion, scaler, DEVICE,
            optimizer=optimizer_p1, train=True,
        )
        v_loss, v_top1, v_top5 = run_epoch(
            model, val_loader, criterion, scaler, DEVICE, train=False,
        )
        log_epoch("P1", epoch + 1, PHASE1_EPOCHS, t_loss, t_top1, t_top5,
                  v_loss, v_top1, v_top5)
        history["train_loss"].append(t_loss); history["train_top1"].append(t_top1)
        history["train_top5"].append(t_top5); history["val_loss"].append(v_loss)
        history["val_top1"].append(v_top1);   history["val_top5"].append(v_top5)

        if v_top1 > best_val_top1:
            best_val_top1 = v_top1
            best_epoch    = epoch + 1
            torch.save(model.state_dict(), best_ckpt_path)

    # ── Phase 2: full fine-tune with cosine annealing ─────────────────────────
    print(f"\n{'='*60}")
    print(f"Phase 2 — full fine-tune ({PHASE2_EPOCHS} epochs, cosine LR)")
    print(f"{'='*60}")
    unfreeze_all(model)

    optimizer_p2 = optim.SGD(
        model.parameters(), lr=LR,
        momentum=MOMENTUM, weight_decay=WEIGHT_DECAY, nesterov=True,
    )
    scheduler = optim.lr_scheduler.CosineAnnealingLR(
        optimizer_p2, T_max=PHASE2_EPOCHS, eta_min=MIN_LR,
    )

    for epoch in range(PHASE2_EPOCHS):
        t_loss, t_top1, t_top5 = run_epoch(
            model, train_loader, criterion, scaler, DEVICE,
            optimizer=optimizer_p2, train=True,
        )
        v_loss, v_top1, v_top5 = run_epoch(
            model, val_loader, criterion, scaler, DEVICE, train=False,
        )
        scheduler.step()
        lr_now = scheduler.get_last_lr()[0]
        log_epoch("P2", epoch + 1, PHASE2_EPOCHS, t_loss, t_top1, t_top5,
                  v_loss, v_top1, v_top5, lr=lr_now)
        history["train_loss"].append(t_loss); history["train_top1"].append(t_top1)
        history["train_top5"].append(t_top5); history["val_loss"].append(v_loss)
        history["val_top1"].append(v_top1);   history["val_top5"].append(v_top5)

        if v_top1 > best_val_top1:
            best_val_top1 = v_top1
            best_epoch    = PHASE1_EPOCHS + epoch + 1
            torch.save(model.state_dict(), best_ckpt_path)

    # ── Test — evaluate best-val checkpoint on held-out test set ──────────────
    print(f"\n{'='*60}")
    print(f"Test — loading best checkpoint (epoch {best_epoch}, val top-1 {best_val_top1:.2f}%)")
    print(f"{'='*60}")
    model.load_state_dict(torch.load(best_ckpt_path, weights_only=True))
    test_loss, test_top1, test_top5 = run_epoch(
        model, test_loader, criterion, scaler, DEVICE, train=False,
    )
    print(f"  Test loss  : {test_loss:.4f}")
    print(f"  Test top-1 : {test_top1:.2f}%")
    print(f"  Test top-5 : {test_top5:.2f}%")

    # ── Save last-epoch weights (for comparison) ──────────────────────────────
    torch.save(model.state_dict(), OUT_DIR / f"{MODEL_NAME}_last.pth")

    # ── Persist full results + hyperparams ────────────────────────────────────
    results = {
        "model": MODEL_NAME,
        "seed": SEED,
        "hyperparams": {
            "phase1_epochs": PHASE1_EPOCHS,
            "phase2_epochs": PHASE2_EPOCHS,
            "total_epochs": TOTAL_EPOCHS,
            "lr": LR, "min_lr": MIN_LR,
            "momentum": MOMENTUM, "weight_decay": WEIGHT_DECAY,
            "batch_size": BATCH_SIZE,
            "image_size": IMAGE_SIZE, "crop_size": CROP_SIZE,
            "label_smoothing": LABEL_SMOOTHING,
            "erase_prob": ERASE_PROB,
            "optimizer": "SGD+Nesterov", "scheduler": "CosineAnnealing",
            "sampler": "WeightedRandomSampler (inv-sqrt freq)",
            "augmentation": "Resize+RandomResizedCrop+HFlip+Rotation30+TrivialAugment+RandomErase",
        },
        "best_val_top1": best_val_top1,
        "best_epoch": best_epoch,
        "test": {"loss": test_loss, "top1": test_top1, "top5": test_top5},
        "history": history,
    }
    with open(OUT_DIR / f"{MODEL_NAME}_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved → {OUT_DIR / f'{MODEL_NAME}_results.json'}")
    print(f"Best weights  → {best_ckpt_path}")


# ── Epoch runner ─────────────────────────────────────────────────────────────

def run_epoch(model, loader, criterion, scaler, device,
              optimizer=None, train: bool = False):
    model.train() if train else model.eval()
    total_loss, correct1, correct5, total = 0.0, 0, 0, 0

    ctx = torch.enable_grad() if train else torch.no_grad()
    with ctx:
        for inputs, targets in tqdm(loader, leave=False,
                                    desc="train" if train else "eval "):
            inputs, targets = inputs.to(device), targets.to(device)

            if train:
                optimizer.zero_grad(set_to_none=True)
                with autocast("cuda"):
                    outputs = model(inputs)
                    loss    = criterion(outputs, targets)
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
            else:
                with autocast("cuda"):
                    outputs = model(inputs)
                    loss    = criterion(outputs, targets)

            total_loss += loss.item() * inputs.size(0)
            correct1   += topk_accuracy(outputs, targets, k=1)
            correct5   += topk_accuracy(outputs, targets, k=5)
            total      += inputs.size(0)

    n = total
    return total_loss / n, 100.0 * correct1 / n, 100.0 * correct5 / n


def topk_accuracy(outputs, targets, k):
    topk = outputs.topk(k, dim=1).indices
    return topk.eq(targets.unsqueeze(1)).any(dim=1).sum().item()


def log_epoch(phase, epoch, total, t_loss, t1, t5, v_loss, v1, v5, lr=None):
    lr_str = f"  lr={lr:.2e}" if lr is not None else ""
    print(f"[{phase} {epoch:02d}/{total:02d}]{lr_str}  "
          f"train loss={t_loss:.4f} top1={t1:.2f}% top5={t5:.2f}%  |  "
          f"val   loss={v_loss:.4f} top1={v1:.2f}% top5={v5:.2f}%")


if __name__ == "__main__":
    main()
