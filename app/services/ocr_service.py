"""
OCR Service
-----------
Extracts text from uploaded images and PDFs using pytesseract.

Preprocessing pipeline (Pillow + OpenCV):
  1. Resize to a useful resolution (_fit_image keeps RAM bounded).
  2. Grayscale conversion.
  3. Histogram equalisation — redistributes pixel intensities so that faint
     pencil strokes and shadow-darkened regions both land in the mid-range
     where Tesseract's LSTM model was trained, improving contrast uniformly
     without the halo artefacts that a fixed Unsharp Mask can introduce.
  4. Morphological line removal — horizontal notebook rules are structurally
     wider than any character stroke, so a wide horizontal kernel erodes them
     away while leaving letter shapes intact.
  5. Adaptive thresholding — applies a local threshold at each pixel based on
     its neighbourhood, making it robust to shadows and uneven phone lighting
     that defeats a global Otsu threshold on the full image.

Tesseract configuration:
  --psm 11  Sparse text mode.  Finds as much text as possible in no
            particular order.  Outperforms psm 4 on handwritten notes and
            material lists because it does not assume a single column layout.
  --oem 3   Hybrid legacy + LSTM engine — most accurate overall; oem 1
            (LSTM-only) was faster but missed numbers and short abbreviations.
  -l eng    Explicit language selection avoids Tesseract auto-detecting a
            non-English script and switching character sets mid-document.

Fallback:
  If the primary attempt yields fewer than 10 characters (indicating a layout
  misclassification), a second attempt uses --psm 6 (uniform text block) which
  captures dense handwritten pages that psm 11 sometimes treats as too sparse.
"""

import time
import logging
from pathlib import Path
from typing import Union

import numpy as np
from PIL import Image
import pytesseract

logger = logging.getLogger(__name__)

# OpenCV is optional at import time so the module can still be loaded in
# environments that lack the native libraries (e.g. unit-test runners that
# mock cv2).  Any code path that needs cv2 will raise ImportError at call time
# if it is absent.
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logger.warning("opencv-python-headless not installed — falling back to Pillow-only preprocessing.")

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
# PSM 6 assumes a single uniform block of text, which recovers dense pages
# that PSM 11's sparse-text heuristic sometimes skips entirely.
_TESS_CONFIG_FALLBACK = "--psm 6 --oem 3"

# If the primary OCR result is shorter than this (characters), retry with the
# fallback config.  10 chars is below any useful contractor line item.
_FALLBACK_THRESHOLD = 10

# Laplacian variance below this value indicates the image is too blurry for
# reliable OCR.  We still attempt OCR but log a warning so callers can
# surface a low-confidence flag to the user.
_BLUR_THRESHOLD = 30.0

# Short sleep between PDF pages to yield CPU on single-core hosts (seconds)
_PAGE_YIELD_SECS = 0.05


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


def _check_blur(gray_arr: np.ndarray) -> float:
    """
    Compute the variance of the Laplacian of a grayscale numpy array.
    High variance = sharp image; low variance = blurry image.
    Returns the blur score (higher is sharper).
    """
    if not CV2_AVAILABLE:
        return float("inf")
    return float(cv2.Laplacian(gray_arr, cv2.CV_64F).var())


def preprocess_image(img: Image.Image) -> Image.Image:
    """
    Prepare an image for Tesseract using a hybrid Pillow + OpenCV pipeline.

    Pipeline rationale:
      - Resize first so all subsequent operations work on a bounded array.
      - Histogram equalisation corrects uneven phone lighting before thresholding.
      - Morphological line removal strips notebook rules that OCR engines
        routinely misread as dashes or underscores, splitting item lines.
      - Adaptive threshold handles localised shadows that defeat a global
        Otsu threshold applied to the full image.

    Falls back to a Pillow-only path if OpenCV is not available.
    """
    img = _fit_image(img)
    img = img.convert("L")  # grayscale

    if not CV2_AVAILABLE:
        # Pillow-only fallback (less effective but keeps the service running)
        from PIL import ImageEnhance, ImageFilter
        img = ImageEnhance.Contrast(img).enhance(1.5)
        img = img.filter(ImageFilter.SHARPEN)
        return img

    # Convert PIL grayscale → numpy (no copy — array shares memory with PIL buffer)
    arr = np.array(img, dtype=np.uint8)

    # --- Histogram equalisation ------------------------------------------
    # Spreads intensity values across the full 0-255 range.  On phone photos
    # this lifts faint pencil strokes and suppresses washed-out backgrounds
    # without any per-region tuning.
    arr = cv2.equalizeHist(arr)

    # --- Remove horizontal notebook lines -----------------------------------
    # A horizontal structuring element whose width (1/20 of image width, min
    # 40 px) is much longer than any letter stroke but matches ruled lines.
    # morphologyEx OPEN = erode then dilate → removes features narrower than
    # the kernel (letter strokes stay; long horizontal rules disappear).
    h, w = arr.shape
    line_kernel_width = max(40, w // 20)
    horiz_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (line_kernel_width, 1)
    )
    lines_mask = cv2.morphologyEx(arr, cv2.MORPH_OPEN, horiz_kernel)
    # Subtract detected lines from the image so text pixels are unaffected
    arr = cv2.subtract(arr, lines_mask)

    # --- Adaptive thresholding -------------------------------------------
    # blockSize of 31 px covers roughly one character-height at 150 DPI,
    # giving each neighbourhood enough context to set a local threshold.
    # C=10 offsets the mean to push faint strokes above the cut-off.
    arr = cv2.adaptiveThreshold(
        arr,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=31,
        C=10,
    )

    # Convert back to PIL — fromarray does not copy the buffer
    return Image.fromarray(arr)


def _ocr_with_fallback(img: Image.Image) -> tuple[str, bool]:
    """
    Run Tesseract with the primary config.  If the result is suspiciously short
    (< _FALLBACK_THRESHOLD chars), retry with the fallback PSM config.

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
        # Use whichever attempt produced more output
        if len(fallback_text) > len(text):
            return fallback_text, True
    return text, False


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
        if CV2_AVAILABLE:
            gray_arr = np.array(processed, dtype=np.uint8)
            blur_score = _check_blur(gray_arr)
            if blur_score < _BLUR_THRESHOLD:
                logger.warning(
                    "Image %s appears very blurry (Laplacian variance=%.1f < %.1f); "
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

    # Convert pages one at a time using a generator to avoid loading all pages into RAM
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
                logger.warning("PDF has >%d pages; truncating OCR at page %d", _MAX_PDF_PAGES, _MAX_PDF_PAGES)
                page_img.close()
                break
            try:
                processed = preprocess_image(page_img)
                text, used_fallback = _ocr_with_fallback(processed)
                texts.append(text)
                if used_fallback:
                    logger.debug("Fallback OCR config used for page %d of %s", page_num, pdf_path)
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
