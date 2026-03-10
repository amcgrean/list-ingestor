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

    raw_content = message.content[0].text.strip()
    logger.debug("Claude raw response: %s", raw_content)

    # Strip accidental markdown fences if Claude adds them
    raw_content = re.sub(r"^```(?:json)?\s*", "", raw_content)
    raw_content = re.sub(r"\s*```$", "", raw_content)

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
