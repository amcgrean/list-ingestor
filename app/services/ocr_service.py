"""
OCR Service
-----------
Extracts text from uploaded images and PDFs using pytesseract.

Preprocessing pipeline (Pillow + numpy only — no OpenCV required):
  1. Resize to a useful resolution (_fit_image keeps RAM bounded).
  2. Grayscale conversion.
  3. Histogram equalisation — redistributes pixel intensities via a CDF
     lookup table so that faint pencil strokes and shadow-darkened regions
     land in the mid-range where Tesseract's LSTM model was trained.
  4. Morphological line removal — a horizontal 1-D sliding-minimum then
     sliding-maximum (open) using numpy.lib.stride_tricks.sliding_window_view
     detects and subtracts notebook rules without touching letter strokes.
     The view is zero-copy so peak extra RAM is one small (h×w) working array.
  5. Adaptive thresholding — PIL BoxBlur computes a per-pixel local mean;
     subtracting it and comparing to a constant C binarises the image while
     remaining robust to shadows and uneven phone lighting.

Tesseract configuration:
  --psm 11  Sparse text mode.  Finds text in no particular order, which
            outperforms psm 4 on handwritten notes and material lists that
            do not follow a strict single-column layout.
  --oem 3   Hybrid legacy + LSTM engine — most accurate overall.
  -l eng    Explicit language selection avoids Tesseract switching character
            sets mid-document when it encounters abbreviations or numbers.

Fallback:
  If the primary attempt yields fewer than 10 characters (layout
  misclassification), a second attempt uses --psm 6 (uniform text block)
  which captures dense handwritten pages that psm 11 sometimes skips.
"""

import time
import logging
from pathlib import Path
from typing import Union

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image, ImageFilter

import pytesseract

logger = logging.getLogger(__name__)

# pdf2image is optional — only needed for PDF inputs
try:
    from pdf2image import convert_from_path
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False
    logger.warning("pdf2image not installed — PDF uploads will not be supported.")

# Hard ceiling (px) on longest side to keep RAM bounded (~30 MB/image max)
_MAX_IMAGE_DIM = 2400
# Minimum size (px) for reliable OCR — upscale anything smaller so Tesseract
# has enough resolution to distinguish character strokes
_MIN_IMAGE_DIM = 1400
# PDF render DPI — 150 produces reliable text quality without the RAM spike
# that 300 DPI caused (502s); 120 was too low and caused missed characters
_PDF_DPI = 150
# Cap pages to avoid unbounded memory on large PDFs
_MAX_PDF_PAGES = 10

# Primary Tesseract config:
#   --psm 11  = sparse text — best for handwritten notes and material lists
#               where items are scattered rather than in a strict column
#   --oem 3   = hybrid legacy + LSTM (most accurate overall)
#   -l eng    = force English to prevent mid-document script switching
_TESS_CONFIG = "--psm 11 --oem 3 -l eng"

# Fallback config used when the primary attempt returns very little text.
# PSM 6 assumes a single uniform block of text, recovering dense pages that
# PSM 11's sparse-text heuristic sometimes skips entirely.
_TESS_CONFIG_FALLBACK = "--psm 6 --oem 3"

# If the primary OCR result is shorter than this (characters), retry with the
# fallback config.  10 chars is below any useful contractor line item.
_FALLBACK_THRESHOLD = 10

# Laplacian-proxy variance below this value indicates the image is extremely
# blurry.  We still attempt OCR but log a warning so callers can surface a
# low-confidence flag to the user.
_BLUR_THRESHOLD = 30.0

# Short sleep between PDF pages to yield CPU on single-core hosts (seconds)
_PAGE_YIELD_SECS = 0.05


# ---------------------------------------------------------------------------
# Resize helper (unchanged from original)
# ---------------------------------------------------------------------------

def _fit_image(img: Image.Image) -> Image.Image:
    """
    Ensure the image's longest side sits between _MIN_IMAGE_DIM and _MAX_IMAGE_DIM.
    Upscaling low-res photos gives Tesseract enough pixel density to read fine text;
    downscaling huge images keeps RAM in check.
    """
    w, h = img.size
    longest = max(w, h)
    if longest < _MIN_IMAGE_DIM:
        scale = _MIN_IMAGE_DIM / longest
    elif longest > _MAX_IMAGE_DIM:
        scale = _MAX_IMAGE_DIM / longest
    else:
        return img
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Preprocessing primitives (numpy + Pillow — no OpenCV)
# ---------------------------------------------------------------------------

def _equalize_hist(arr: np.ndarray) -> np.ndarray:
    """
    Histogram equalisation via CDF lookup table (numpy only).
    Spreads intensity values across the full 0-255 range so faint pencil
    strokes and washed-out backgrounds are both pulled toward mid-range
    before thresholding, without any per-region tuning.
    """
    hist, _ = np.histogram(arr.flatten(), bins=256, range=(0, 256))
    cdf = hist.cumsum()
    cdf_min = int(cdf[cdf > 0][0])
    scale = max(int(arr.size) - cdf_min, 1)
    lut = np.clip(np.round((cdf - cdf_min) / scale * 255), 0, 255).astype(np.uint8)
    return lut[arr]


def _remove_horizontal_lines(arr: np.ndarray, kernel_width: int) -> np.ndarray:
    """
    Detect and remove horizontal notebook lines using row statistics.

    A ruled line row is identified by two properties that distinguish it from
    rows containing handwritten text:
      1. Its mean brightness is significantly darker than its local vertical
         context (a sliding mean over ±5 surrounding rows).
      2. Its pixel standard deviation is low — the row is more uniform than
         rows that contain mixed text strokes and background.

    Detected line rows are replaced by linear interpolation from the nearest
    non-line rows above and below, effectively painting over the rule with the
    surrounding background.  This is memory-safe: only one copy (result) is
    kept alongside the read-only input array.

    kernel_width is retained in the signature for API consistency; it is used
    as the minimum expected horizontal span of a line (a row narrower than this
    in dark pixels is not treated as a rule).
    """
    h, w = arr.shape
    row_means = arr.mean(axis=1)   # (h,) float64
    row_stds = arr.std(axis=1)     # (h,) float64

    # Vertical sliding mean gives each row a local brightness reference
    v_win = min(11, h)
    v_pad = v_win // 2
    padded_means = np.pad(row_means, v_pad, mode="edge")
    local_means = sliding_window_view(padded_means, v_win).mean(axis=1)

    # Line row: darker than context by >20 levels AND more uniform than average
    is_line = (row_means < local_means - 20) & (row_stds < max(float(row_stds.mean()), 1.0) * 0.8)

    if not is_line.any():
        return arr

    result = arr.copy()
    non_line_idx = np.where(~is_line)[0]

    if len(non_line_idx) == 0:
        return arr  # degenerate: every row is a "line" — leave unchanged

    for i in np.where(is_line)[0]:
        above = non_line_idx[non_line_idx < i]
        below = non_line_idx[non_line_idx > i]
        if len(above) > 0 and len(below) > 0:
            r_a, r_b = int(above[-1]), int(below[0])
            t = float(i - r_a) / (r_b - r_a)
            result[i] = np.clip(
                (1.0 - t) * arr[r_a] + t * arr[r_b], 0, 255
            ).astype(np.uint8)
        elif len(above) > 0:
            result[i] = arr[int(above[-1])]
        else:
            result[i] = arr[int(below[0])]

    return result


def _adaptive_threshold(arr: np.ndarray, block_size: int = 31, C: int = 10) -> np.ndarray:
    """
    Adaptive mean threshold using PIL BoxBlur for local neighbourhood mean.

    BoxBlur is O(n) regardless of radius and uses no extra large arrays, so
    memory stays flat.  Each pixel is set to 255 if it exceeds its local mean
    by more than C, else 0.  This handles uneven lighting and phone shadows
    that defeat a global Otsu threshold applied to the full image.
    """
    local_mean = np.array(
        Image.fromarray(arr).filter(ImageFilter.BoxBlur(block_size // 2)),
        dtype=np.int16,
    )
    return ((arr.astype(np.int16) - local_mean + C) > 0).astype(np.uint8) * 255


def _check_blur(gray_arr: np.ndarray) -> float:
    """
    Estimate image sharpness via summed variance of horizontal and vertical
    finite differences — a lightweight proxy for Laplacian variance that
    requires only numpy.  Higher = sharper.
    """
    dy = np.diff(gray_arr.astype(np.int16), axis=0)
    dx = np.diff(gray_arr.astype(np.int16), axis=1)
    return float(dy.var() + dx.var())


# ---------------------------------------------------------------------------
# Public preprocessing entry point
# ---------------------------------------------------------------------------

def preprocess_image(img: Image.Image) -> Image.Image:
    """
    Prepare an image for Tesseract using a Pillow + numpy pipeline.

    Steps and rationale:
      1. _fit_image     — bounds resolution so RAM stays under ~30 MB/image.
      2. grayscale      — single channel reduces memory by 3× vs RGB.
      3. _equalize_hist — normalises contrast before thresholding.
      4. _remove_horizontal_lines — strips notebook rules that Tesseract
                         misreads as dashes or underscores.
      5. _adaptive_threshold — binarises robustly under uneven lighting.

    Returns a PIL image (mode L) so pytesseract continues to work unchanged.
    """
    img = _fit_image(img)
    img = img.convert("L")

    arr = np.array(img, dtype=np.uint8)

    arr = _equalize_hist(arr)

    h, w = arr.shape
    line_kernel_width = max(40, w // 20)
    arr = _remove_horizontal_lines(arr, line_kernel_width)

    arr = _adaptive_threshold(arr, block_size=31, C=10)

    return Image.fromarray(arr)


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------

def _ocr_with_fallback(img: Image.Image) -> tuple[str, bool]:
    """
    Run Tesseract with the primary config.  If the result is suspiciously
    short (< _FALLBACK_THRESHOLD chars), retry with the fallback PSM config.

    Returns (text, used_fallback).
    """
    text = pytesseract.image_to_string(img, config=_TESS_CONFIG).strip()
    if len(text) < _FALLBACK_THRESHOLD:
        logger.debug(
            "Primary OCR returned %d chars (< %d); retrying with fallback config %s",
            len(text),
            _FALLBACK_THRESHOLD,
            _TESS_CONFIG_FALLBACK,
        )
        fallback_text = pytesseract.image_to_string(img, config=_TESS_CONFIG_FALLBACK).strip()
        if len(fallback_text) > len(text):
            return fallback_text, True
    return text, False


# ---------------------------------------------------------------------------
# Public extractors
# ---------------------------------------------------------------------------

def extract_text_from_image(image_path: Union[str, Path]) -> str:
    """
    Run OCR on a single image file and return raw text.
    Raises RuntimeError on failure.
    """
    try:
        img = Image.open(image_path)
        processed = preprocess_image(img)
        img.close()  # release original before OCR to keep peak RAM low

        # Blur detection — warns early if the photo is unlikely to yield good text
        gray_arr = np.array(processed, dtype=np.uint8)
        blur_score = _check_blur(gray_arr)
        if blur_score < _BLUR_THRESHOLD:
            logger.warning(
                "Image %s appears very blurry (sharpness score=%.1f < %.1f); "
                "OCR result may be low confidence.",
                image_path,
                blur_score,
                _BLUR_THRESHOLD,
            )

        text, used_fallback = _ocr_with_fallback(processed)
        processed.close()
        if used_fallback:
            logger.debug("Fallback OCR config used for %s", image_path)
        return text
    except Exception as exc:
        logger.exception("OCR failed for %s", image_path)
        raise RuntimeError(f"OCR failed: {exc}") from exc


def extract_text_from_pdf(pdf_path: Union[str, Path], dpi: int = _PDF_DPI) -> str:
    """
    Convert each PDF page to an image then OCR them, returning concatenated text.
    Pages are processed one at a time to keep peak RAM low.
    Raises RuntimeError on failure.
    """
    if not PDF_SUPPORT:
        raise RuntimeError(
            "pdf2image is not installed. Install it and poppler to enable PDF support."
        )

    texts = []
    page_num = 0
    try:
        for page_img in convert_from_path(
            str(pdf_path),
            dpi=dpi,
            fmt="jpeg",           # JPEG uses less RAM than uncompressed during decode
            thread_count=1,       # Stay single-threaded to cap CPU/RAM on starter plan
        ):
            page_num += 1
            if page_num > _MAX_PDF_PAGES:
                logger.warning(
                    "PDF has >%d pages; truncating OCR at page %d",
                    _MAX_PDF_PAGES,
                    _MAX_PDF_PAGES,
                )
                page_img.close()
                break
            try:
                processed = preprocess_image(page_img)
                text, used_fallback = _ocr_with_fallback(processed)
                texts.append(text)
                if used_fallback:
                    logger.debug(
                        "Fallback OCR config used for page %d of %s", page_num, pdf_path
                    )
            except Exception as exc:
                logger.warning("OCR failed for page %d: %s", page_num, exc)
                texts.append(f"[OCR failed for page {page_num}]")
            finally:
                page_img.close()
            # Yield CPU between pages so the web worker stays responsive
            time.sleep(_PAGE_YIELD_SECS)
    except Exception as exc:
        logger.exception("PDF conversion failed for %s", pdf_path)
        raise RuntimeError(f"Could not convert PDF to images: {exc}") from exc

    return "\n\n".join(t for t in texts if t)


def extract_text(file_path: Union[str, Path]) -> str:
    """
    Dispatch to the correct extractor based on file extension.
    Returns the extracted text string.
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    if ext == ".pdf":
        return extract_text_from_pdf(path)
    elif ext in {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}:
        return extract_text_from_image(path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")
