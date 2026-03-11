"""
ChatGPT Parser Service
----------------------
Sends raw OCR text to OpenAI's ChatGPT and returns a list of structured line items.
Mirrors the interface of ai_parser.py so routes.py can swap providers transparently.
"""

import json
import logging
import re
from typing import Any

import openai

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
    model: str = "gpt-4o",
) -> list[dict[str, Any]]:
    """
    Send OCR text to ChatGPT and return a list of dicts with keys:
      - quantity (float)
      - description (str)

    Raises ValueError on parse failure, openai.APIError on API errors.
    """
    if not ocr_text or not ocr_text.strip():
        raise ValueError("OCR text is empty — nothing to parse.")

    client = openai.OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        max_tokens=2048,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(ocr_text=ocr_text.strip()),
            },
        ],
    )

    choice = response.choices[0] if response.choices else None
    if choice is None:
        raise ValueError("ChatGPT returned no choices in the response.")

    raw_content = (choice.message.content or "").strip()
    logger.debug("ChatGPT raw response: %s", raw_content)

    if not raw_content:
        raise ValueError(
            f"ChatGPT returned an empty response (finish_reason={choice.finish_reason!r})."
        )

    # Strip accidental markdown fences
    raw_content = re.sub(r"^```(?:json)?\s*", "", raw_content)
    raw_content = re.sub(r"\s*```$", "", raw_content).strip()

    if not raw_content:
        raise ValueError("ChatGPT response was only a markdown code fence with no content.")

    try:
        items = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse ChatGPT response as JSON: %s", raw_content)
        raise ValueError(f"ChatGPT returned invalid JSON: {exc}") from exc

    if not isinstance(items, list):
        raise ValueError("ChatGPT response is not a JSON array.")

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
