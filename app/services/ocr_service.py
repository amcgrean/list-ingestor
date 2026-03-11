"""
OCR Service
-----------
Extracts text from uploaded images and PDFs using pytesseract.
Applies preprocessing (grayscale, contrast boost, thresholding) to improve accuracy.
"""

import os
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

# Maximum dimension (px) before downscaling; keeps RAM under ~25 MB per image
_MAX_IMAGE_DIM = 2000
# PDF render DPI — 150 is plenty for printed text and uses 4× less RAM than 300
_PDF_DPI = 150
# Cap pages to avoid unbounded memory on large PDFs
_MAX_PDF_PAGES = 10


def _downscale(img: Image.Image, max_dim: int = _MAX_IMAGE_DIM) -> Image.Image:
    """Scale down an image so its longest side is at most max_dim pixels."""
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    scale = max_dim / max(w, h)
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def preprocess_image(img: Image.Image) -> Image.Image:
    """
    Apply grayscale, contrast enhancement, and adaptive thresholding
    to maximise OCR accuracy on photos of printed material lists.
    """
    # Downscale first to reduce RAM usage before expensive operations
    img = _downscale(img)

    # Convert to grayscale
    img = img.convert("L")

    # Enhance contrast
    img = ImageEnhance.Contrast(img).enhance(2.0)

    # Sharpen slightly to help with out-of-focus photos
    img = img.filter(ImageFilter.SHARPEN)

    # Binarise with Pillow's built-in point threshold (fast & effective)
    img = img.point(lambda p: 255 if p > 140 else 0)

    return img


def extract_text_from_image(image_path: Union[str, Path]) -> str:
    """
    Run OCR on a single image file and return raw text.
    Raises RuntimeError on failure.
    """
    try:
        img = Image.open(image_path)
        img = preprocess_image(img)
        text = pytesseract.image_to_string(
            img,
            config="--psm 6 --oem 3",  # psm 6 = assume uniform block of text
        )
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
                text = pytesseract.image_to_string(processed, config="--psm 6 --oem 3")
                texts.append(text.strip())
            except Exception as exc:
                logger.warning("OCR failed for page %d: %s", page_num, exc)
                texts.append(f"[OCR failed for page {page_num}]")
            finally:
                page_img.close()
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
