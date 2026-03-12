"""
OpenAI Vision Service
---------------------
Sends an uploaded image to the OpenAI Responses API and extracts a structured
material list.

Usage::

    from services.openai_vision import extract_items_from_image

    items = extract_items_from_image("/path/to/photo.jpg", api_key="sk-...")
    # [{"quantity": 25, "description": "2x10 joists 16ft"}, ...]

Accepts a local file path, a public image URL, or raw bytes.
Images are base64-encoded and sent inline — they are never stored.
"""

import base64
import json
import logging
import re
from pathlib import Path
from typing import Union

from openai import OpenAI

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are reading a contractor material list from a photo. "
    "Extract each line item with quantity and description. "
    "Return ONLY a JSON array with no surrounding markdown. "
    "Each element must have:\n"
    '  "quantity": a number (use 1 if not specified)\n'
    '  "description": the material description as a string\n'
    "Normalise spelling and expand common construction shorthand "
    "(e.g. '2x10' → '2x10', 'LUS210' stays as-is). "
    "Preserve measurements, sizes, grades, and lengths exactly. "
    "Do not include any explanation — only the raw JSON array."
)

# Supported MIME types for base64 inline images
_EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def extract_items_from_image(
    image_source: Union[str, Path, bytes],
    api_key: str,
    model: str = "gpt-4o",
) -> list[dict]:
    """Send an image to the OpenAI Responses API and return extracted items.

    Args:
        image_source: Local file path, public URL string, or raw image bytes.
        api_key: OpenAI API key.
        model: Vision-capable model to use (default ``gpt-4o``).

    Returns:
        List of dicts, each with ``quantity`` (float) and ``description`` (str).

    Raises:
        ValueError: If ``api_key`` is empty.
        RuntimeError: If the API call or JSON extraction fails.
    """
    if not api_key:
        raise ValueError("api_key must not be empty")

    client = OpenAI(api_key=api_key)
    image_content = _build_image_content(image_source)

    try:
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": _SYSTEM_PROMPT},
                        image_content,
                    ],
                }
            ],
        )
        raw_text = response.output_text.strip()
    except Exception as exc:
        logger.exception("OpenAI Responses API call failed")
        raise RuntimeError(f"Vision API error: {exc}") from exc

    items = _parse_json_response(raw_text)
    logger.info("vision_extracted items=%d", len(items))
    return items


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_image_content(image_source: Union[str, Path, bytes]) -> dict:
    """Return a Responses API ``input_image`` content block."""
    if isinstance(image_source, bytes):
        # Raw bytes — assume JPEG if we cannot detect
        b64 = base64.b64encode(image_source).decode("utf-8")
        return {
            "type": "input_image",
            "image_url": f"data:image/jpeg;base64,{b64}",
        }

    source_str = str(image_source)

    if source_str.startswith("http://") or source_str.startswith("https://"):
        # Public URL — pass directly
        return {
            "type": "input_image",
            "image_url": source_str,
        }

    # Local file path
    path = Path(image_source)
    mime = _EXT_TO_MIME.get(path.suffix.lower(), "image/jpeg")
    with open(path, "rb") as fh:
        raw = fh.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    return {
        "type": "input_image",
        "image_url": f"data:{mime};base64,{b64}",
    }


def _parse_json_response(text: str) -> list[dict]:
    """Extract and validate a JSON array from the model's response."""
    # Strip markdown code fences if the model wrapped the output
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                logger.error("Could not parse JSON from vision response: %.200s", text)
                return []
        else:
            logger.error("No JSON array found in vision response: %.200s", text)
            return []

    if not isinstance(data, list):
        logger.error("Expected JSON array from vision, got %s", type(data).__name__)
        return []

    validated: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description", "")).strip()
        if not desc:
            continue
        try:
            qty = float(item.get("quantity", 1))
        except (TypeError, ValueError):
            qty = 1.0
        validated.append({"quantity": qty, "description": desc})

    return validated
