"""
Unit tests for app/services/ocr_service.py

Coverage:
  - preprocess_image: output is a valid PIL image, dimensions stay within limits
  - _ocr_with_fallback: fallback triggers when primary returns short text
  - blur detection: _check_blur returns low score for a uniform (blurry) image
                    and a high score for a sharp synthetic edge image

The module is imported via importlib so that Flask (which app/__init__.py
requires) does not need to be installed in the test environment.
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
_spec.loader.exec_module(ocr_service)

# Make it importable under its real dotted name so patch() resolves correctly
sys.modules.setdefault("ocr_service", ocr_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pil(width: int = 200, height: int = 150, value: int = 128) -> Image.Image:
    """Return a solid-grey grayscale PIL image of the given size."""
    arr = np.full((height, width), value, dtype=np.uint8)
    return Image.fromarray(arr, mode="L")


def _make_rgb_pil(width: int = 200, height: int = 150) -> Image.Image:
    """Return a solid light-grey RGB PIL image."""
    arr = np.full((height, width, 3), 200, dtype=np.uint8)
    return Image.fromarray(arr, mode="RGB")


# ---------------------------------------------------------------------------
# preprocess_image tests
# ---------------------------------------------------------------------------

class TestPreprocessImage(unittest.TestCase):
    """preprocess_image must return a valid PIL image within size limits."""

    def test_returns_pil_image(self):
        result = ocr_service.preprocess_image(_make_rgb_pil(300, 300))
        self.assertIsInstance(result, Image.Image)

    def test_output_is_grayscale(self):
        """preprocess_image should always return a grayscale (mode L) image."""
        result = ocr_service.preprocess_image(_make_rgb_pil(300, 300))
        self.assertEqual(result.mode, "L")

    def test_dimensions_do_not_exceed_max(self):
        """Output longest side must not exceed _MAX_IMAGE_DIM."""
        oversized = _make_rgb_pil(
            ocr_service._MAX_IMAGE_DIM + 500,
            ocr_service._MAX_IMAGE_DIM + 500,
        )
        result = ocr_service.preprocess_image(oversized)
        self.assertLessEqual(max(result.size), ocr_service._MAX_IMAGE_DIM)

    def test_small_image_is_upscaled(self):
        """Images smaller than _MIN_IMAGE_DIM should be upscaled."""
        tiny = _make_rgb_pil(100, 100)
        result = ocr_service.preprocess_image(tiny)
        self.assertGreaterEqual(max(result.size), ocr_service._MIN_IMAGE_DIM)

    def test_already_sized_image_unchanged_dimensions(self):
        """An image already within bounds should not be resized."""
        target = (ocr_service._MIN_IMAGE_DIM + ocr_service._MAX_IMAGE_DIM) // 2
        img = _make_rgb_pil(target, target)
        result = ocr_service.preprocess_image(img)
        # Integer rounding may shift by ±1 px
        self.assertAlmostEqual(result.size[0], target, delta=1)
        self.assertAlmostEqual(result.size[1], target, delta=1)

    def test_output_array_has_valid_pixel_range(self):
        """All pixel values must be in [0, 255]."""
        result = ocr_service.preprocess_image(_make_rgb_pil(400, 300))
        arr = np.array(result)
        self.assertGreaterEqual(int(arr.min()), 0)
        self.assertLessEqual(int(arr.max()), 255)


# ---------------------------------------------------------------------------
# _ocr_with_fallback tests
# ---------------------------------------------------------------------------

class TestOcrWithFallback(unittest.TestCase):
    """_ocr_with_fallback should use the fallback config when primary returns
    fewer than _FALLBACK_THRESHOLD characters."""

    @patch.object(ocr_service.pytesseract, "image_to_string")
    def test_no_fallback_when_primary_returns_enough_text(self, mock_tess):
        mock_tess.return_value = "2 6x6 PT posts\n25 2x10 joists"
        _, used_fallback = ocr_service._ocr_with_fallback(_make_pil())
        self.assertFalse(used_fallback)
        mock_tess.assert_called_once()

    @patch.object(ocr_service.pytesseract, "image_to_string")
    def test_fallback_triggered_on_short_primary_result(self, mock_tess):
        mock_tess.side_effect = ["abc", "100 trex decking boards"]
        text, used_fallback = ocr_service._ocr_with_fallback(_make_pil())
        self.assertTrue(used_fallback)
        self.assertEqual(text, "100 trex decking boards")
        # Confirm both configs were tried
        self.assertEqual(mock_tess.call_count, 2)
        calls = mock_tess.call_args_list
        self.assertEqual(calls[0][1]["config"], ocr_service._TESS_CONFIG)
        self.assertEqual(calls[1][1]["config"], ocr_service._TESS_CONFIG_FALLBACK)

    @patch.object(ocr_service.pytesseract, "image_to_string")
    def test_primary_result_kept_when_fallback_shorter(self, mock_tess):
        """If the fallback yields less text, keep the primary result."""
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
# Blur detection tests
# ---------------------------------------------------------------------------

class TestCheckBlur(unittest.TestCase):
    """_check_blur: uniform (blurry) images score low; sharp-edge images score high."""

    def setUp(self):
        if not ocr_service.CV2_AVAILABLE:
            self.skipTest("OpenCV not installed")

    def test_uniform_image_is_low_blur_score(self):
        """A flat uniform image has zero Laplacian — maximally blurry."""
        arr = np.full((200, 200), 128, dtype=np.uint8)
        score = ocr_service._check_blur(arr)
        self.assertLess(score, 1.0)

    def test_sharp_edge_image_is_high_blur_score(self):
        """An image with a hard edge has high Laplacian variance — sharp."""
        arr = np.zeros((200, 200), dtype=np.uint8)
        arr[:, 100:] = 255
        score = ocr_service._check_blur(arr)
        self.assertGreater(score, ocr_service._BLUR_THRESHOLD)

    def test_returns_inf_without_cv2(self):
        """Without OpenCV, _check_blur must return infinity (safe default)."""
        original = ocr_service.CV2_AVAILABLE
        try:
            ocr_service.CV2_AVAILABLE = False
            score = ocr_service._check_blur(np.zeros((100, 100), dtype=np.uint8))
            self.assertEqual(score, float("inf"))
        finally:
            ocr_service.CV2_AVAILABLE = original


# ---------------------------------------------------------------------------
# extract_text_from_image integration-level tests (pytesseract mocked)
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
        if not ocr_service.CV2_AVAILABLE:
            self.skipTest("OpenCV not installed")
        # A uniform-grey image has zero Laplacian variance → blurry
        mock_open.return_value = _make_pil(400, 300, value=128)
        mock_tess.return_value = "some text here to avoid fallback"
        logger_name = ocr_service.logger.name
        with self.assertLogs(logger_name, level="WARNING") as cm:
            ocr_service.extract_text_from_image("blurry.png")
        self.assertTrue(
            any("blurry" in msg.lower() or "low confidence" in msg.lower() for msg in cm.output)
        )


if __name__ == "__main__":
    unittest.main()
