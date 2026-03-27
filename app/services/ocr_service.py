"""
OCR Service
-----------
Extracts text from uploaded images and PDFs.

Two modes:
  - "vision" (default): sends images directly to Claude or GPT-4o vision for
    far superior handwriting recognition.  Falls back to Tesseract if no API
    key is available.
  - "tesseract": classic local OCR with preprocessing.  Good for typed/printed
    text but weak on handwriting.
"""

import base64
import logging
import os
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
# For vision API we can go larger for legibility
_MAX_VISION_DIM = 3000
# PDF render DPI — 150 is plenty for printed text and uses 4× less RAM than 300
_PDF_DPI = 150
# Vision PDF DPI — higher for handwriting legibility
_VISION_PDF_DPI = 200
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
    img = _downscale(img)
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.filter(ImageFilter.SHARPEN)
    img = img.point(lambda p: 255 if p > 140 else 0)
    return img


def _image_to_base64(img: Image.Image, max_dim: int = _MAX_VISION_DIM) -> str:
    """Convert a PIL image to a base64-encoded JPEG for vision APIs."""
    import io
    img = _downscale(img, max_dim)
    if img.mode == "RGBA":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _file_to_base64(file_path: Path, max_dim: int = _MAX_VISION_DIM) -> str:
    """Load an image file and return base64-encoded JPEG."""
    img = Image.open(file_path)
    b64 = _image_to_base64(img, max_dim)
    img.close()
    return b64


# ---------------------------------------------------------------------------
# Vision-based OCR (Claude / GPT-4o)
# ---------------------------------------------------------------------------

_VISION_SYSTEM = """\
You are an expert OCR system specialised in reading handwritten and printed \
material lists from the building materials / lumber industry.

Your job is to transcribe the image as faithfully as possible into plain text. \
Preserve the original structure (line breaks, groupings, indentation). \
Do NOT interpret, restructure, or add anything — just transcribe what you see.

Special rules:
- Tally marks (||||) → convert to the number they represent (e.g. |||| = 5, |||| || = 7)
- Crossed-out items → prefix the line with [CROSSED OUT]
- Illegible words → use [???] as a placeholder
- Arrows or continuation marks → note with [→ continued]
- Page numbers or headers (e.g. "Page 2 of 3") → include as-is
- Preserve original abbreviations, brand names, and shorthand exactly as written
"""


def extract_text_vision_claude(
    images_b64: list[str],
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> str:
    """Send one or more images to Claude vision for transcription."""
    import anthropic
    content = []
    for i, b64 in enumerate(images_b64):
        if len(images_b64) > 1:
            content.append({"type": "text", "text": f"--- Page {i+1} ---"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
    content.append({
        "type": "text",
        "text": "Transcribe this material list exactly as written. Preserve layout and line breaks.",
    })

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_VISION_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text_block = next((b for b in message.content if b.type == "text"), None)
    return text_block.text.strip() if text_block else ""


def extract_text_vision_openai(
    images_b64: list[str],
    api_key: str,
    model: str = "gpt-4o",
) -> str:
    """Send one or more images to GPT-4o vision for transcription."""
    import openai
    content = []
    for i, b64 in enumerate(images_b64):
        if len(images_b64) > 1:
            content.append({"type": "text", "text": f"--- Page {i+1} ---"})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"},
        })
    content.append({
        "type": "text",
        "text": "Transcribe this material list exactly as written. Preserve layout and line breaks.",
    })

    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": _VISION_SYSTEM},
            {"role": "user", "content": content},
        ],
    )
    choice = response.choices[0] if response.choices else None
    return (choice.message.content or "").strip() if choice else ""


# ---------------------------------------------------------------------------
# Tesseract OCR (local fallback)
# ---------------------------------------------------------------------------

def extract_text_from_image(image_path: Union[str, Path]) -> str:
    """Run Tesseract OCR on a single image file and return raw text."""
    try:
        img = Image.open(image_path)
        img = preprocess_image(img)
        text = pytesseract.image_to_string(
            img,
            config="--psm 6 --oem 3",
        )
        img.close()
        return text.strip()
    except Exception as exc:
        logger.exception("OCR failed for %s", image_path)
        raise RuntimeError(f"OCR failed: {exc}") from exc


def extract_text_from_pdf(pdf_path: Union[str, Path], dpi: int = _PDF_DPI) -> str:
    """Convert each PDF page to an image then OCR them via Tesseract."""
    if not PDF_SUPPORT:
        raise RuntimeError(
            "pdf2image is not installed. Install it and poppler to enable PDF support."
        )

    texts = []
    page_num = 0
    try:
        for page_img in convert_from_path(
            str(pdf_path), dpi=dpi, fmt="jpeg", thread_count=1,
        ):
            page_num += 1
            if page_num > _MAX_PDF_PAGES:
                logger.warning("PDF has >%d pages; truncating at page %d", _MAX_PDF_PAGES, _MAX_PDF_PAGES)
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


def pdf_to_base64_pages(pdf_path: Union[str, Path], dpi: int = _VISION_PDF_DPI) -> list[str]:
    """Convert PDF pages to a list of base64-encoded JPEG images for vision APIs."""
    if not PDF_SUPPORT:
        raise RuntimeError("pdf2image is not installed.")

    pages_b64 = []
    page_num = 0
    for page_img in convert_from_path(str(pdf_path), dpi=dpi, fmt="jpeg", thread_count=1):
        page_num += 1
        if page_num > _MAX_PDF_PAGES:
            page_img.close()
            break
        try:
            pages_b64.append(_image_to_base64(page_img))
        finally:
            page_img.close()
    return pages_b64


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

def extract_text(
    file_path: Union[str, Path],
    vision_provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> str:
    """
    Extract text from an image or PDF.

    If vision_provider is set ("claude" or "openai") and an api_key is provided,
    uses the vision API for superior handwriting recognition.
    Otherwise falls back to local Tesseract OCR.
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    if ext not in {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp", ".pdf"}:
        raise ValueError(f"Unsupported file type: {ext}")

    # --- Vision mode ---
    if vision_provider and api_key:
        try:
            if ext == ".pdf":
                images_b64 = pdf_to_base64_pages(path)
            else:
                images_b64 = [_file_to_base64(path)]

            if vision_provider == "openai":
                text = extract_text_vision_openai(images_b64, api_key, model or "gpt-4o")
            else:
                text = extract_text_vision_claude(images_b64, api_key, model or "claude-sonnet-4-6")

            if text.strip():
                logger.info("Vision OCR succeeded (%d chars)", len(text))
                return text.strip()

            logger.warning("Vision OCR returned empty — falling back to Tesseract")
        except Exception as exc:
            logger.warning("Vision OCR failed (%s) — falling back to Tesseract: %s", vision_provider, exc)

    # --- Tesseract fallback ---
    if ext == ".pdf":
        return extract_text_from_pdf(path)
    return extract_text_from_image(path)
