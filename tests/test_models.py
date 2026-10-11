"""Model integration checks: python tests/test_models.py (Python 3.10+)."""

import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import colleague_model
import config
from ai_model import ModelHub
from webui import decode_image


def frame():
    image = np.zeros((240, 640, 3), dtype=np.uint8)
    image[:] = (0, 255, 0)
    image[:, :160] = (255, 0, 0)
    image[:, -160:] = (0, 0, 255)
    return image


class ModelTests(unittest.TestCase):
    def captured_input(self, direct_resize):
        captured = []

        def run(image):
            captured.append(image)
            return np.array([[3.0, 0.0]])

        with patch.object(colleague_model, "_load_runner", return_value=(run, "test")):
            model = colleague_model.OnnxClassifier("unused", ("healthy", "wilted"),
                                                   direct_resize=direct_resize)
        model(frame())
        return captured[0]

    def normalized(self, color):
        return (np.array(color, np.float32) / 255 - np.array(config.MODEL_MEAN, np.float32)) / np.array(config.MODEL_STD, np.float32)

    def test_health_keeps_both_edges_of_a_wide_photo(self):
        image = self.captured_input(True)
        self.assertEqual(image.shape, (1, 3, 224, 224))
        np.testing.assert_allclose(image[0, :, 112, 0], self.normalized((255, 0, 0)))
        np.testing.assert_allclose(image[0, :, 112, -1], self.normalized((0, 0, 255)))

    def test_species_keeps_its_center_crop(self):
        image = self.captured_input(False)
        expected = np.broadcast_to(self.normalized((0, 255, 0))[:, None, None], (3, 224, 224))
        np.testing.assert_allclose(image[0], expected)

    def test_metadata_and_health_flags(self):
        metadata = json.loads((ROOT / "python/models/plantwatch.json").read_text())
        self.assertEqual(tuple(metadata["classes"]), config.HEALTH_MODEL_LABELS)
        self.assertEqual(metadata["preprocessing"]["size"], config.MODEL_INPUT_SIZE)
        self.assertEqual(tuple(metadata["preprocessing"]["mean"]), config.MODEL_MEAN)
        self.assertEqual(tuple(metadata["preprocessing"]["std"]), config.MODEL_STD)
        for logits, label, healthy in [([3, 0], "healthy", True), ([0, 3], "wilted", False)]:
            with self.subTest(label=label), patch.object(colleague_model, "_load_runner",
                    return_value=(lambda image: np.array([logits]), "test")):
                models = colleague_model.create()
                self.assertTrue(models.assess_health.direct_resize)
                self.assertFalse(models.classify_plant.direct_resize)
                result = ModelHub(models).assess_health(frame())
                self.assertEqual(result.label, label)
                self.assertEqual(result.healthy, healthy)
                self.assertEqual(result.needs_water, not healthy)
                self.assertFalse(result.simulated)

    def test_photo_upload_applies_exif_orientation(self):
        image = Image.fromarray(frame()[:, :300])
        exif = Image.Exif()
        exif[274] = 6
        data = io.BytesIO()
        image.save(data, format="JPEG", exif=exif)
        with Image.open(io.BytesIO(data.getvalue())) as photo:
            expected = np.asarray(ImageOps.exif_transpose(photo).convert("RGB"))
        decoded = decode_image("data:image/jpeg;base64," + base64.b64encode(data.getvalue()).decode())
        np.testing.assert_array_equal(decoded, expected)
        self.assertEqual(decoded.shape, (300, 240, 3))

    def test_real_app_matches_the_standalone_plantwatch_model(self):
        package = ROOT / "ai-models/plantwatch"
        spec = importlib.util.spec_from_file_location("plantwatch_reference", package / "export_onnx.py")
        reference = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reference)
        hub = ModelHub(colleague_model.create())
        self.assertTrue(hub.is_real("assess_health"))
        self.assertTrue(hub.is_real("classify_plant"))
        rng = np.random.default_rng(42)
        with tempfile.TemporaryDirectory() as temporary:
            for height, width in [(150, 400), (400, 150)]:
                with self.subTest(height=height, width=width):
                    image = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
                    photo = Path(temporary) / "photo.png"
                    Image.fromarray(image).save(photo)
                    with contextlib.redirect_stdout(io.StringIO()):
                        expected = reference.predict_image(package / "models/houseplants/plantwatch.onnx", photo)
                    actual = hub.assess_health(image)
                    self.assertEqual(actual.label, expected["condition"])
                    self.assertAlmostEqual(actual.confidence, expected["scores"][actual.label], places=6)
                    self.assertFalse(actual.simulated)


if __name__ == "__main__":
    unittest.main()
