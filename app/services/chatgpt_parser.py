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

from app.services.ai_parser import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, _clean_json_response, _validate_item

logger = logging.getLogger(__name__)


def parse_material_list(
    ocr_text: str,
    api_key: str,
    model: str = "gpt-4o",
) -> list[dict[str, Any]]:
    """
    Send OCR text to ChatGPT and return a list of structured line-item dicts.
    Raises ValueError on parse failure, openai.APIError on API errors.
    """
    if not ocr_text or not ocr_text.strip():
        raise ValueError("OCR text is empty — nothing to parse.")

    client = openai.OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        max_tokens=4096,
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
    logger.debug("ChatGPT raw response: %s", raw_content[:500])

    if not raw_content:
        raise ValueError(
            f"ChatGPT returned an empty response (finish_reason={choice.finish_reason!r})."
        )

    raw_content = _clean_json_response(raw_content)

    if not raw_content:
        raise ValueError("ChatGPT response contained no JSON content.")

    try:
        items = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse ChatGPT response as JSON: %s", raw_content[:500])
        raise ValueError(f"ChatGPT returned invalid JSON: {exc}") from exc

    if not isinstance(items, list):
        raise ValueError("ChatGPT response is not a JSON array.")

    validated = []
    for item in items:
        v = _validate_item(item)
        if v is not None:
            validated.append(v)

    return validated
