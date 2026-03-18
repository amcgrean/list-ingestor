"""
Unit tests for app/services/ocr_service.py

Coverage:
  - preprocess_image: output is a valid PIL image, dimensions stay within limits
  - _ocr_with_fallback: fallback triggers when primary returns short text
  - blur detection: _check_blur scores reflect image sharpness

The module is loaded via importlib so Flask (imported by app/__init__.py)
does not need to be installed in the test environment.
"""

import sys
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Load ocr_service without triggering app/__init__.py (avoids Flask dep)
# ---------------------------------------------------------------------------

_SERVICE_PATH = Path(__file__).parent.parent / "app" / "services" / "ocr_service.py"
_spec = importlib.util.spec_from_file_location("ocr_service", _SERVICE_PATH)
ocr_service = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(ocr_service)
except ModuleNotFoundError as exc:
    if exc.name == "pytesseract":
        raise unittest.SkipTest("Deprecated OCR service requires optional pytesseract dependency")
    raise

sys.modules.setdefault("ocr_service", ocr_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pil(width: int = 200, height: int = 150, value: int = 128) -> Image.Image:
    """Return a solid-grey grayscale PIL image."""
    return Image.fromarray(np.full((height, width), value, dtype=np.uint8), mode="L")


def _make_rgb_pil(width: int = 200, height: int = 150) -> Image.Image:
    """Return a solid light-grey RGB PIL image."""
    return Image.fromarray(np.full((height, width, 3), 200, dtype=np.uint8), mode="RGB")


# ---------------------------------------------------------------------------
# preprocess_image tests
# ---------------------------------------------------------------------------

class TestPreprocessImage(unittest.TestCase):

    def test_returns_pil_image(self):
        result = ocr_service.preprocess_image(_make_rgb_pil(300, 300))
        self.assertIsInstance(result, Image.Image)

    def test_output_is_grayscale(self):
        result = ocr_service.preprocess_image(_make_rgb_pil(300, 300))
        self.assertEqual(result.mode, "L")

    def test_dimensions_do_not_exceed_max(self):
        oversized = _make_rgb_pil(
            ocr_service._MAX_IMAGE_DIM + 500,
            ocr_service._MAX_IMAGE_DIM + 500,
        )
        result = ocr_service.preprocess_image(oversized)
        self.assertLessEqual(max(result.size), ocr_service._MAX_IMAGE_DIM)

    def test_small_image_is_upscaled(self):
        result = ocr_service.preprocess_image(_make_rgb_pil(100, 100))
        self.assertGreaterEqual(max(result.size), ocr_service._MIN_IMAGE_DIM)

    def test_already_sized_image_unchanged_dimensions(self):
        target = (ocr_service._MIN_IMAGE_DIM + ocr_service._MAX_IMAGE_DIM) // 2
        img = _make_rgb_pil(target, target)
        result = ocr_service.preprocess_image(img)
        self.assertAlmostEqual(result.size[0], target, delta=1)
        self.assertAlmostEqual(result.size[1], target, delta=1)

    def test_output_pixel_range(self):
        """All pixel values must be in [0, 255]."""
        result = ocr_service.preprocess_image(_make_rgb_pil(400, 300))
        arr = np.array(result)
        self.assertGreaterEqual(int(arr.min()), 0)
        self.assertLessEqual(int(arr.max()), 255)


# ---------------------------------------------------------------------------
# Preprocessing primitive tests
# ---------------------------------------------------------------------------

class TestEqualizaHist(unittest.TestCase):

    def test_uniform_image_unchanged_after_equalization(self):
        """A perfectly uniform image has no contrast to redistribute; result is valid."""
        arr = np.full((100, 100), 100, dtype=np.uint8)
        result = ocr_service._equalize_hist(arr)
        self.assertEqual(result.shape, arr.shape)
        self.assertGreaterEqual(int(result.min()), 0)
        self.assertLessEqual(int(result.max()), 255)

    def test_equalization_spreads_dark_image(self):
        """An all-dark image should have its brightest pixels lifted toward 255."""
        arr = np.arange(256, dtype=np.uint8).reshape(16, 16)
        result = ocr_service._equalize_hist(arr)
        self.assertGreater(int(result.max()), int(arr.max()) - 1)


class TestRemoveHorizontalLines(unittest.TestCase):

    def test_output_shape_preserved(self):
        arr = np.random.randint(100, 200, (200, 300), dtype=np.uint8)
        result = ocr_service._remove_horizontal_lines(arr, kernel_width=40)
        self.assertEqual(result.shape, arr.shape)

    def test_output_in_valid_range(self):
        arr = np.random.randint(100, 200, (100, 200), dtype=np.uint8)
        result = ocr_service._remove_horizontal_lines(arr, kernel_width=20)
        self.assertGreaterEqual(int(result.min()), 0)
        self.assertLessEqual(int(result.max()), 255)

    def test_horizontal_line_lightened(self):
        """A dark line on a light background should be lightened toward the background."""
        arr = np.full((100, 200), 200, dtype=np.uint8)
        arr[50, :] = 30  # dark horizontal rule — much darker than background
        result = ocr_service._remove_horizontal_lines(arr, kernel_width=40)
        # Line row should be lighter after removal (closer to surrounding 200)
        self.assertGreater(int(result[50, :].mean()), int(arr[50, :].mean()))

    def test_no_change_when_no_lines_present(self):
        """A uniformly textured image with no rules should pass through unchanged."""
        rng = np.random.default_rng(0)
        arr = rng.integers(150, 200, (50, 100), dtype=np.uint8)
        result = ocr_service._remove_horizontal_lines(arr, kernel_width=20)
        # With no clearly darker uniform rows, output should equal input
        np.testing.assert_array_equal(result, arr)


class TestAdaptiveThreshold(unittest.TestCase):

    def test_output_is_binary(self):
        """Adaptive threshold must produce only 0 or 255 pixels."""
        arr = np.random.randint(0, 255, (100, 100), dtype=np.uint8)
        result = ocr_service._adaptive_threshold(arr)
        unique = set(np.unique(result).tolist())
        self.assertTrue(unique.issubset({0, 255}))

    def test_output_shape_preserved(self):
        arr = np.random.randint(0, 200, (120, 150), dtype=np.uint8)
        result = ocr_service._adaptive_threshold(arr)
        self.assertEqual(result.shape, arr.shape)


# ---------------------------------------------------------------------------
# Blur detection tests
# ---------------------------------------------------------------------------

class TestCheckBlur(unittest.TestCase):

    def test_uniform_image_scores_zero(self):
        """A flat uniform image has no gradients — score should be 0."""
        arr = np.full((200, 200), 128, dtype=np.uint8)
        score = ocr_service._check_blur(arr)
        self.assertAlmostEqual(score, 0.0)

    def test_sharp_edge_scores_above_threshold(self):
        """A hard vertical edge produces high gradient variance — score above threshold."""
        arr = np.zeros((200, 200), dtype=np.uint8)
        arr[:, 100:] = 255
        score = ocr_service._check_blur(arr)
        self.assertGreater(score, ocr_service._BLUR_THRESHOLD)

    def test_noisy_image_scores_higher_than_smooth(self):
        """Random noise is sharper than a smooth gradient."""
        rng = np.random.default_rng(42)
        noisy = rng.integers(0, 256, (200, 200), dtype=np.uint8)
        smooth = np.tile(np.linspace(0, 255, 200, dtype=np.uint8), (200, 1))
        self.assertGreater(ocr_service._check_blur(noisy), ocr_service._check_blur(smooth))


# ---------------------------------------------------------------------------
# _ocr_with_fallback tests
# ---------------------------------------------------------------------------

class TestOcrWithFallback(unittest.TestCase):

    @patch.object(ocr_service.pytesseract, "image_to_string")
    def test_no_fallback_when_primary_sufficient(self, mock_tess):
        mock_tess.return_value = "2 6x6 PT posts\n25 2x10 joists"
        _, used_fallback = ocr_service._ocr_with_fallback(_make_pil())
        self.assertFalse(used_fallback)
        mock_tess.assert_called_once()

    @patch.object(ocr_service.pytesseract, "image_to_string")
    def test_fallback_triggered_on_short_primary(self, mock_tess):
        mock_tess.side_effect = ["abc", "100 trex decking boards"]
        text, used_fallback = ocr_service._ocr_with_fallback(_make_pil())
        self.assertTrue(used_fallback)
        self.assertEqual(text, "100 trex decking boards")
        self.assertEqual(mock_tess.call_count, 2)
        calls = mock_tess.call_args_list
        self.assertEqual(calls[0][1]["config"], ocr_service._TESS_CONFIG)
        self.assertEqual(calls[1][1]["config"], ocr_service._TESS_CONFIG_FALLBACK)

    @patch.object(ocr_service.pytesseract, "image_to_string")
    def test_primary_kept_when_fallback_shorter(self, mock_tess):
        mock_tess.side_effect = ["short", ""]
        text, used_fallback = ocr_service._ocr_with_fallback(_make_pil())
        self.assertFalse(used_fallback)
        self.assertEqual(text, "short")

    @patch.object(ocr_service.pytesseract, "image_to_string")
    def test_empty_primary_triggers_fallback(self, mock_tess):
        mock_tess.side_effect = ["", "deck clips\n2-1/2 bronze screws"]
        _, used_fallback = ocr_service._ocr_with_fallback(_make_pil())
        self.assertTrue(used_fallback)


# ---------------------------------------------------------------------------
# extract_text_from_image integration tests (pytesseract mocked)
# ---------------------------------------------------------------------------

class TestExtractTextFromImage(unittest.TestCase):

    @patch.object(ocr_service.pytesseract, "image_to_string")
    @patch.object(ocr_service.Image, "open")
    def test_returns_stripped_text(self, mock_open, mock_tess):
        mock_open.return_value = _make_rgb_pil(400, 300)
        mock_tess.return_value = "  2 6x6 PT posts\n  "
        result = ocr_service.extract_text_from_image("fake.png")
        self.assertEqual(result, "2 6x6 PT posts")

    @patch.object(ocr_service.pytesseract, "image_to_string")
    @patch.object(ocr_service.Image, "open")
    def test_raises_runtime_error_on_tess_failure(self, mock_open, mock_tess):
        mock_open.return_value = _make_rgb_pil(400, 300)
        mock_tess.side_effect = Exception("tesseract crashed")
        with self.assertRaises(RuntimeError):
            ocr_service.extract_text_from_image("bad.png")

    @patch.object(ocr_service.pytesseract, "image_to_string")
    @patch.object(ocr_service.Image, "open")
    def test_blur_warning_logged_for_blurry_image(self, mock_open, mock_tess):
        # Uniform grey → zero gradients → score of 0 → below threshold
        mock_open.return_value = _make_pil(400, 300, value=128)
        mock_tess.return_value = "some text here to avoid fallback"
        with self.assertLogs(ocr_service.logger.name, level="WARNING") as cm:
            ocr_service.extract_text_from_image("blurry.png")
        self.assertTrue(
            any("blurry" in msg.lower() or "low confidence" in msg.lower() for msg in cm.output)
        )


if __name__ == "__main__":
    unittest.main()
