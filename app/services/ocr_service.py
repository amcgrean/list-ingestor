"""
OCR Service
-----------
Extracts text from uploaded images and PDFs using pytesseract.
Applies preprocessing (grayscale, contrast boost, sharpening) to improve accuracy.
Binarisation is intentionally left to Tesseract's internal engine, which uses
locally-adaptive thresholding far superior to any fixed global threshold.
"""

import time
import logging
from pathlib import Path
from typing import Union

from PIL import Image, ImageEnhance, ImageFilter
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
# Tesseract config:
#   --psm 4  = single column of text (suits material lists better than
#              psm 6's assumption of a single uniform text block)
#   --oem 3  = hybrid legacy + LSTM engine (most accurate; oem 1 LSTM-only
#              was faster but missed numbers and short abbreviations)
_TESS_CONFIG = "--psm 4 --oem 3"
# Short sleep between pages to yield CPU on single-core hosts (seconds)
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


def preprocess_image(img: Image.Image) -> Image.Image:
    """
    Prepare an image for Tesseract: fit to a useful size, convert to grayscale,
    lightly boost contrast, and sharpen edges.

    We deliberately omit a fixed binary threshold here.  A global point-threshold
    destroys faint text, shadows, and uneven lighting — all common in phone photos
    of material lists.  Tesseract's own locally-adaptive Otsu binarisation handles
    these cases far better when given a clean greyscale input.
    """
    img = _fit_image(img)
    img = img.convert("L")
    # Mild contrast lift — brings text forward without blowing out fine strokes
    img = ImageEnhance.Contrast(img).enhance(1.5)
    # Edge sharpening helps with slightly out-of-focus captures
    img = img.filter(ImageFilter.SHARPEN)
    return img


def extract_text_from_image(image_path: Union[str, Path]) -> str:
    """
    Run OCR on a single image file and return raw text.
    Raises RuntimeError on failure.
    """
    try:
        img = Image.open(image_path)
        img = preprocess_image(img)
        text = pytesseract.image_to_string(img, config=_TESS_CONFIG)
        img.close()
        return text.strip()
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
                text = pytesseract.image_to_string(processed, config=_TESS_CONFIG)
                texts.append(text.strip())
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
