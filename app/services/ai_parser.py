"""
AI Parser Service
-----------------
Sends raw OCR text to Claude and returns a list of structured line items.
Claude normalises spelling errors, construction shorthand, and units.
"""

import json
import logging
import re
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert at reading material lists for the building materials and lumber industry.

Your job is to parse raw, messy OCR text from contractor-submitted material lists and \
return a clean, structured JSON array of line items.

Rules:
- Each line item must have "quantity" (number, default to 1 if missing) and "description" (string).
- Normalize spelling errors (e.g. "deckin" → "decking").
- Expand construction shorthand (e.g. "2x10x16" → "2x10 16ft", "lf" → "linear feet").
- Keep measurements in the description (size, length, grade, species, finish, color).
- Do NOT infer or add items that are not present in the source text.
- If a line has no clear quantity, use 1.
- Merge duplicate items if they are clearly the same material listed twice.
- Return ONLY valid JSON — no markdown fences, no commentary.

Grouped / hierarchical lists:
- Lists often have a group header that provides shared context (brand, color, product line, \
finish, etc.) followed by indented or bulleted sub-items that each represent a distinct SKU.
- When you encounter this pattern, build the full description for each sub-item by combining \
the group context with the sub-item detail. Do NOT emit the group header as its own line item.
- Example: if the group header is "black textured Westbury C-10 railings" and sub-items are \
"8' sections", "7' sections", "6' sections", produce descriptions like \
"Westbury C-10 railing black textured 8ft section", "Westbury C-10 railing black textured 7ft section", etc.
- Similarly, if a product line / brand is stated at the top (e.g. "Moisture Shield Vantage") \
and sub-items list sizes and colors, incorporate that brand context into each sub-item description.

Tally marks:
- Quantities are sometimes recorded as tally marks using vertical strokes (e.g. "IIII II", "III", "II").
- Count each stroke: I=1, II=2, III=3, IIII=4 (or ||||), IIII I=6, IIII II=7, etc.
- Use the counted total as the numeric quantity for that line item.

Output format (JSON array):
[
  {"quantity": 25, "description": "2x10 SPF joist 16ft"},
  {"quantity": 2,  "description": "6x6 pressure treated post"},
  {"quantity": 1,  "description": "LUS210 joist hanger 1lb box"},
  {"quantity": 100,"description": "Trex decking 16ft sand"}
]
"""

USER_PROMPT_TEMPLATE = """\
Parse the following material list text into structured JSON line items.

--- MATERIAL LIST START ---
{ocr_text}
--- MATERIAL LIST END ---

Return ONLY the JSON array.
"""


def parse_material_list(
    ocr_text: str,
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> list[dict[str, Any]]:
    """
    Send OCR text to Claude and return a list of dicts with keys:
      - quantity (float)
      - description (str)

    Raises ValueError on parse failure, anthropic.APIError on API errors.
    """
    if not ocr_text or not ocr_text.strip():
        raise ValueError("OCR text is empty — nothing to parse.")

    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(ocr_text=ocr_text.strip()),
            }
        ],
    )

    # Find the first text block — content may include thinking blocks or other types
    text_block = next(
        (block for block in message.content if block.type == "text"),
        None,
    )
    if text_block is None:
        logger.error(
            "Claude response contained no text block. stop_reason=%s content=%r",
            message.stop_reason,
            message.content,
        )
        raise ValueError(
            "Claude returned no text content "
            f"(stop_reason={message.stop_reason!r})."
        )

    raw_content = text_block.text.strip()
    logger.debug("Claude raw response: %s", raw_content)

    if not raw_content:
        logger.error(
            "Claude returned an empty text block. stop_reason=%s",
            message.stop_reason,
        )
        raise ValueError(
            "Claude returned an empty response "
            f"(stop_reason={message.stop_reason!r})."
        )

    # Strip accidental markdown fences if Claude adds them
    raw_content = re.sub(r"^```(?:json)?\s*", "", raw_content)
    raw_content = re.sub(r"\s*```$", "", raw_content).strip()

    if not raw_content:
        raise ValueError("Claude response was only a markdown code fence with no content.")

    try:
        items = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse Claude response as JSON: %s", raw_content)
        raise ValueError(f"Claude returned invalid JSON: {exc}") from exc

    if not isinstance(items, list):
        raise ValueError("Claude response is not a JSON array.")

    validated = []
    for item in items:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description", "")).strip()
        if not description:
            continue
        try:
            quantity = float(item.get("quantity", 1))
        except (TypeError, ValueError):
            quantity = 1.0
        validated.append({"quantity": quantity, "description": description})

    return validated
