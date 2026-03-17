"""
Claude Vision Service
---------------------
Sends an uploaded image directly to the Anthropic Messages API and extracts
a structured material list in one shot — no separate OCR step needed.

Claude's vision model handles handwritten lists, tally marks, grouped items,
and construction shorthand better than generic OCR + parse pipelines.

Usage::

    from services.claude_vision import extract_items_from_image

    items = extract_items_from_image("/path/to/photo.jpg", api_key="sk-ant-...")
    # [{"quantity": 25, "description": "2x10 SPF joist 16ft"}, ...]

Accepts a local file path, a public image URL, or raw bytes.
Images are base64-encoded and sent inline — they are never stored.
"""

import base64
import json
import logging
import re
from pathlib import Path
from typing import Union

import anthropic

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an expert at reading material lists for the building materials and lumber industry.

Your job is to look at a photo or scan of a contractor-submitted material list and return a
clean, structured JSON array of line items.

Rules:
- Each line item must have "quantity" (number, default to 1 if missing) and "description" (string).
- Normalize spelling errors (e.g. "deckin" → "decking").
- Expand construction shorthand (e.g. "2x10x16" → "2x10 16ft", "lf" → "linear feet").
- Keep measurements in the description (size, length, grade, species, finish, color).
- Do NOT infer or add items that are not present in the source image.
- If a line has no clear quantity, use 1.
- Merge duplicate items if they are clearly the same material listed twice.
- Return ONLY valid JSON — no markdown fences, no commentary.

Grouped / hierarchical lists:
- Lists often have a group header that provides shared context (brand, color, product line,
  finish, etc.) followed by indented or bulleted sub-items that each represent a distinct SKU.
- When you encounter this pattern, build the full description for each sub-item by combining
  the group context with the sub-item detail. Do NOT emit the group header as its own line item.
- Example: if the group header is "black textured Westbury C-10 railings" and sub-items are
  "8' sections", "7' sections", "6' sections", produce descriptions like
  "Westbury C-10 railing black textured 8ft section", etc.

Tally marks:
- Quantities are sometimes recorded as tally marks using vertical strokes (e.g. "IIII II", "III").
- Count each stroke: I=1, II=2, III=3, IIII=4, IIII I=6, IIII II=7, etc.
- Use the counted total as the numeric quantity for that line item.

Output format (JSON array only, no other text):
[
  {"quantity": 25, "description": "2x10 SPF joist 16ft"},
  {"quantity": 2,  "description": "6x6 pressure treated post"},
  {"quantity": 1,  "description": "LUS210 joist hanger 1lb box"},
  {"quantity": 100,"description": "Trex decking 16ft sand"}
]
"""

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
    model: str = "claude-opus-4-6",
) -> list[dict]:
    """Send an image to the Anthropic Messages API and return extracted items.

    Args:
        image_source: Local file path, public URL string, or raw image bytes.
        api_key: Anthropic API key.
        model: Vision-capable Claude model (default ``claude-opus-4-6``).

    Returns:
        List of dicts, each with ``quantity`` (float) and ``description`` (str).

    Raises:
        ValueError: If ``api_key`` is empty.
        RuntimeError: If the API call or JSON extraction fails.
    """
    if not api_key:
        raise ValueError("api_key must not be empty")

    client = anthropic.Anthropic(api_key=api_key)
    image_content = _build_image_content(image_source)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        image_content,
                        {
                            "type": "text",
                            "text": "Extract all items from this material list as a JSON array.",
                        },
                    ],
                }
            ],
        )
        text_block = next(
            (block for block in response.content if block.type == "text"),
            None,
        )
        if text_block is None:
            raise RuntimeError(
                f"Claude returned no text block (stop_reason={response.stop_reason!r})"
            )
        raw_text = text_block.text.strip()
    except anthropic.APIError as exc:
        logger.exception("Anthropic API call failed")
        raise RuntimeError(f"Claude Vision API error: {exc}") from exc

    items = _parse_json_response(raw_text)
    logger.info("claude_vision_extracted items=%d", len(items))
    return items


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_image_content(image_source: Union[str, Path, bytes]) -> dict:
    """Return an Anthropic Messages API image content block."""
    if isinstance(image_source, bytes):
        b64 = base64.b64encode(image_source).decode("utf-8")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        }

    source_str = str(image_source)

    if source_str.startswith("http://") or source_str.startswith("https://"):
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": source_str,
            },
        }

    path = Path(image_source)
    mime = _EXT_TO_MIME.get(path.suffix.lower(), "image/jpeg")
    with open(path, "rb") as fh:
        raw = fh.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": b64,
        },
    }


def _parse_json_response(text: str) -> list[dict]:
    """Extract and validate a JSON array from the model's response."""
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
                logger.error("Could not parse JSON from Claude vision response: %.200s", text)
                return []
        else:
            logger.error("No JSON array found in Claude vision response: %.200s", text)
            return []

    if not isinstance(data, list):
        logger.error("Expected JSON array from Claude vision, got %s", type(data).__name__)
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
