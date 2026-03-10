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


def preprocess_image(img: Image.Image) -> Image.Image:
    """
    Apply grayscale, contrast enhancement, and adaptive thresholding
    to maximise OCR accuracy on photos of printed material lists.
    """
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
        return text.strip()
    except Exception as exc:
        logger.exception("OCR failed for %s", image_path)
        raise RuntimeError(f"OCR failed: {exc}") from exc


def extract_text_from_pdf(pdf_path: Union[str, Path], dpi: int = 300) -> str:
    """
    Convert each PDF page to an image then OCR them, returning concatenated text.
    Raises RuntimeError on failure.
    """
    if not PDF_SUPPORT:
        raise RuntimeError(
            "pdf2image is not installed. Install it and poppler to enable PDF support."
        )
    try:
        pages = convert_from_path(str(pdf_path), dpi=dpi)
    except Exception as exc:
        logger.exception("PDF conversion failed for %s", pdf_path)
        raise RuntimeError(f"Could not convert PDF to images: {exc}") from exc

    texts = []
    for i, page_img in enumerate(pages, start=1):
        try:
            page_img = preprocess_image(page_img)
            text = pytesseract.image_to_string(page_img, config="--psm 6 --oem 3")
            texts.append(text.strip())
        except Exception as exc:
            logger.warning("OCR failed for page %d: %s", i, exc)
            texts.append(f"[OCR failed for page {i}]")

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
