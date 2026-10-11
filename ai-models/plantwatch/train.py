"""Prepare Houseplants data, train PlantWatch (MobileNetV3-Small), and evaluate saved checkpoints."""
import argparse
import hashlib
import json
import os
import random
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import torch
from PIL import Image, ImageOps
from torch import nn
from torch.utils.data import DataLoader
from torchvision import datasets, models, transforms

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "datasets" / "houseplants"
MODELS = ROOT / "models" / "houseplants"
CLASSES = ["healthy", "wilted"]
SIZE = 224
MEAN = [0.485, 0.456, 0.406]
STD = [0.229, 0.224, 0.225]
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def preprocess(image):
    image = ImageOps.exif_transpose(image).convert("RGB")
    image = image.resize((SIZE, SIZE), Image.Resampling.BILINEAR)
    array = np.asarray(image, dtype=np.float32) / 255.0
    array = (array - np.array(MEAN, dtype=np.float32)) / np.array(STD, dtype=np.float32)
    return np.ascontiguousarray(array.transpose(2, 0, 1), dtype=np.float32)


def metrics(confusion, classes=CLASSES):
    matrix = np.asarray(confusion)
    result = {"accuracy": float(np.trace(matrix) / matrix.sum()),
              "confusion_matrix": matrix.tolist(), "rows": "actual", "columns": "predicted"}
    result["classes"] = {}
    for i, name in enumerate(classes):
        tp = int(matrix[i, i])
        precision = tp / int(matrix[:, i].sum()) if matrix[:, i].sum() else 0.0
        recall = tp / int(matrix[i].sum()) if matrix[i].sum() else 0.0
        result["classes"][name] = {"precision": precision, "recall": recall,
                                  "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
                                  "support": int(matrix[i].sum())}
    return result


def transform(image):
    return torch.from_numpy(preprocess(image))


def load_dataset(root, split, augment=False):
    path = Path(root) / split
    if not all((path / name).is_dir() for name in CLASSES):
        raise ValueError(f"Create {path}/healthy and {path}/wilted and add images to both folders.")
    operations = [transforms.RandomHorizontalFlip(), transforms.RandomRotation(10)] if augment else []
    try:
        ds = datasets.ImageFolder(path, transforms.Compose(operations + [transform]))
    except FileNotFoundError as error:
        raise ValueError(f"Incomplete dataset: add images to {path}/healthy and {path}/wilted.") from error
    if ds.classes != CLASSES:
        raise ValueError(f"Expected classes: {CLASSES}; found: {ds.classes}")
    return ds


def audit_splits(root, splits):
    """Detect corrupt images and identical decoded images across splits."""
    seen = {}
    counts = {}
    for split in splits:
        ds = load_dataset(root, split)
        counts[split] = {name: ds.targets.count(i) for i, name in enumerate(CLASSES)}
        for path, label in ds.samples:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                digest = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()
            if digest in seen:
                old_split, old_label, old_path = seen[digest]
                if old_split != split or old_label != label:
                    raise ValueError(f"Duplicate image across splits or labels: {old_path} and {path}")
            else:
                seen[digest] = split, label, path
    return counts


def make_model(pretrained=False):
    if pretrained:
        torch.hub.set_dir(str(ROOT / ".cache" / "torch" / "hub"))
    weights = models.MobileNet_V3_Small_Weights.DEFAULT if pretrained else None
    model = models.mobilenet_v3_small(weights=weights)
    model.classifier[3] = nn.Linear(model.classifier[3].in_features, len(CLASSES))
    return model


def load_checkpoint(path):
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    if checkpoint["classes"] != CLASSES:
        raise ValueError("Incompatible checkpoint classes")
    model = make_model()
    model.load_state_dict(checkpoint["state_dict"])
    return model.eval(), checkpoint


def choose_device(name):
    if name == "auto":
        name = "mps" if torch.backends.mps.is_available() else "cpu"
    if name == "mps" and not torch.backends.mps.is_available():
        raise ValueError("MPS is unavailable; use --device cpu")
    return torch.device(name)


def prepare_dataset(source, output, seed=42, revision=None):
    """Download Houseplants and create fixed, stratified train/val/test splits."""
    os.environ.setdefault("HF_HOME", str(ROOT / '.cache' / 'huggingface'))
    from datasets import load_dataset as load_hf_dataset
    from huggingface_hub import HfApi

    output = Path(output)
    if output.exists():
        raise ValueError(f'Output already exists: {output}; choose a new folder')
    # Pin the resolved revision in the manifest to make this experiment reproducible.
    revision = HfApi().dataset_info(source, revision=revision).sha
    data = load_hf_dataset(source, revision=revision,
                           cache_dir=str(ROOT / '.cache' / 'huggingface' / 'datasets'))
    if list(data) != ['train']:
        raise ValueError(f'Expected a single source split, found {list(data)}')
    rows = data['train']
    label_names = rows.features['label'].names
    if sorted(label_names) != CLASSES:
        raise ValueError(f'Unexpected source labels: {label_names}')
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.houseplants-', dir=output.parent) as temporary:
        temporary = Path(temporary)
        pool = temporary / 'pool'
        prepared = temporary / 'prepared'
        pool.mkdir()
        groups = defaultdict(list)
        seen = {}
        duplicates, invalid, records, conflicts = [], [], [], []
        conflicting_indices = set()
        for index in range(len(rows)):
            try:
                row = rows[index]
                label = label_names[row['label']]
                image = ImageOps.exif_transpose(row['image']).convert('RGB')
                image.load()
                raw_hash = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()
                image = image.resize((SIZE, SIZE), Image.Resampling.BILINEAR)
                digest = hashlib.sha256(image.tobytes()).hexdigest()
            except (OSError, SyntaxError) as error:
                invalid.append({'source_index': index, 'reason': str(error)})
                continue
            previous = seen.get(raw_hash) or seen.get(digest)
            if previous:
                old_index, old_label = previous
                if label != old_label:
                    conflicting_indices.update([old_index, index])
                    conflicts.append({'source_index': index, 'label': label,
                                      'conflicts_with': old_index, 'other_label': old_label})
                    continue
                duplicates.append({'source_index': index, 'duplicate_of': old_index})
                continue
            seen[raw_hash] = seen[digest] = (index, label)
            filename = f'{index:04d}_{digest[:16]}.png'
            image.save(pool / filename)
            groups[label].append({'source_index': index, 'label': label,
                                  'filename': filename, 'sha256_rgb224': digest})
        groups = {label: [item for item in items if item['source_index'] not in conflicting_indices]
                  for label, items in groups.items()}
        if set(groups) != set(CLASSES) or any(len(items) < 3 for items in groups.values()):
            raise ValueError('At least three distinct images are required for each class')
        rng = random.Random(seed)
        counts = {split: {label: 0 for label in CLASSES} for split in ['train', 'val', 'test']}
        for split in counts:
            for label in CLASSES:
                (prepared / split / label).mkdir(parents=True)
        for label in CLASSES:
            items = groups[label]
            rng.shuffle(items)
            n_val = max(1, round(len(items) * .15))
            n_test = max(1, round(len(items) * .15))
            partitions = {'train': items[n_val + n_test:], 'val': items[:n_val],
                          'test': items[n_val:n_val + n_test]}
            for split, partition in partitions.items():
                for item in partition:
                    target = prepared / split / label / item.pop('filename')
                    (pool / target.name).rename(target)
                    records.append(dict(item, split=split, prepared=str(target.relative_to(prepared))))
                    counts[split][label] += 1
        manifest = {'source': source, 'revision': revision, 'source_rows': len(rows), 'seed': seed,
                    'mapping': {name: name for name in CLASSES},
                    'split_method': '70/15/15 stratified by label; exact RGB and resized duplicates removed',
                    'limitation': 'No plant/session IDs available; visually similar images may cross splits. Wilted does not establish drought.',
                    'preparation': 'EXIF orientation, RGB, bilinear resize 224x224, lossless PNG',
                    'counts': counts, 'duplicates_removed': duplicates, 'invalid_images': invalid,
                    'conflicting_labels': conflicts,
                    'excluded_conflicting_source_indices': sorted(conflicting_indices), 'images': records}
        (prepared / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        prepared.rename(output)
    print(f'Source: {len(rows)}; duplicates removed: {len(duplicates)}; invalid: {len(invalid)}; conflicting rows: {len(conflicting_indices)}', flush=True)
    print(json.dumps(counts, indent=2), flush=True)


def train_model(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = choose_device(args.device)
    print(f"Device: {device}", flush=True)
    train_ds = load_dataset(args.dataset, "train", augment=True)
    val_ds = load_dataset(args.dataset, "val")
    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, num_workers=0)
    model = make_model(pretrained=True).to(device)
    for parameter in model.features.parameters():
        parameter.requires_grad = False
    optimizer = torch.optim.Adam(model.classifier.parameters(), lr=args.lr)
    criterion = nn.CrossEntropyLoss()
    best_accuracy = -1.0
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.dataset) / "manifest.json"
    dataset_metadata = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    for epoch in range(args.epochs):
        model.train()
        # Frozen backbone: BatchNorm statistics must stay fixed too.
        model.features.eval()
        loss_sum = 0.0
        for images, labels in train_loader:
            images, labels = images.to(device), labels.to(device)
            optimizer.zero_grad()
            loss = criterion(model(images), labels)
            loss.backward()
            optimizer.step()
            loss_sum += loss.item() * labels.size(0)
        model.eval()
        confusion = np.zeros((2, 2), dtype=int)
        with torch.inference_mode():
            for images, labels in val_loader:
                predictions = model(images.to(device)).argmax(1).cpu().numpy()
                np.add.at(confusion, (labels.numpy(), predictions), 1)
        report = metrics(confusion)
        report.update(epoch=epoch + 1, train_loss=loss_sum / len(train_ds))
        print(f"Epoch {epoch + 1}/{args.epochs}: loss={report['train_loss']:.4f}, val accuracy={report['accuracy']:.2%}", flush=True)
        if report["accuracy"] > best_accuracy:
            best_accuracy = report["accuracy"]
            torch.save({"state_dict": {k: v.detach().cpu().clone() for k, v in model.state_dict().items()},
                        "classes": CLASSES, "architecture": "mobilenet_v3_small",
                        "preprocessing": {"size": SIZE, "mean": MEAN, "std": STD, "resize": "bilinear"},
                        "epoch": epoch + 1, "validation": report, "seed": args.seed,
                        "dataset": str(Path(args.dataset).resolve()),
                        "label_mapping": dataset_metadata.get("mapping", {name: name for name in CLASSES})}, output)
    print(f"Best model saved: {output}; validation accuracy={best_accuracy:.2%}")


def evaluate_checkpoint(args):
    """Evaluate a saved checkpoint on the held-out test split."""
    device = choose_device(args.device)
    model, _ = load_checkpoint(args.checkpoint)
    model.to(device)
    confusion = np.zeros((2, 2), dtype=int)
    with torch.inference_mode():
        for images, labels in DataLoader(load_dataset(args.dataset, "test"), batch_size=16):
            predictions = model(images.to(device)).argmax(1).cpu().numpy()
            np.add.at(confusion, (labels.numpy(), predictions), 1)
    report = json.dumps(metrics(confusion), indent=2)
    output = Path(args.metrics_output) if args.metrics_output else Path(args.checkpoint).parent / "test_metrics.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report)
    print(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--prepare-only", action="store_true", help="Prepare --source into --dataset, then exit")
    modes.add_argument("--check-only", action="store_true", help="Audit dataset images and splits, then exit")
    modes.add_argument("--evaluate-only", action="store_true", help="Evaluate --checkpoint on the test split, then exit")
    parser.add_argument("--source", default="bhargob11/houseplants", help="Hugging Face dataset ID")
    parser.add_argument("--revision", help="Optional Hugging Face dataset commit or tag")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, default=MODELS / "plantwatch.pth", help="Training checkpoint destination")
    parser.add_argument("--checkpoint", type=Path, default=MODELS / "plantwatch.pth", help="Checkpoint for --evaluate-only")
    parser.add_argument("--metrics-output", type=Path, help="Test metrics JSON; defaults to the checkpoint's folder")
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=["auto", "cpu", "mps"], default="auto")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("threads must be positive")
    if args.epochs < 1 or args.batch_size < 1 or args.lr <= 0:
        parser.error("epochs, batch-size and lr must be positive")
    torch.set_num_threads(args.threads)
    try:
        if args.prepare_only:
            prepare_dataset(args.source, args.dataset, args.seed, args.revision)
            return
        has_test = any(p.suffix.lower() in EXTENSIONS for p in (args.dataset / "test").rglob("*"))
        splits = ["train", "val", "test"] if args.evaluate_only or has_test else ["train", "val"]
        counts = audit_splits(args.dataset, splits)
        print(json.dumps(counts, indent=2), flush=True)
        if args.check_only:
            return
        if args.evaluate_only:
            evaluate_checkpoint(args)
            return
        train_model(args)
        if has_test:
            # Test only the best validation checkpoint, after training is complete.
            args.checkpoint = args.output
            evaluate_checkpoint(args)
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
