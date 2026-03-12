"""
List Parser Pipeline
--------------------
End-to-end pipeline:

  uploaded image
    → OpenAI Vision API  (extract structured line items)
    → SKU matcher        (map each description to a SKU)
    → structured result

Usage::

    from services.list_parser import parse_and_match

    results = parse_and_match("/path/to/photo.jpg")
    # [
    #   {
    #     "quantity": 25,
    #     "input_text": "2x10 joists 16ft",
    #     "matched_sku": "0210tre16",
    #     "confidence": 0.92,
    #   },
    #   ...
    # ]

Environment variables required:
  OPENAI_API_KEY — OpenAI key with access to a vision-capable model.
  DATABASE_URL   — Neon Postgres connection string (used by sku_matcher).
"""

import logging
import os
from pathlib import Path
from typing import Optional, Union

from services.openai_vision import extract_items_from_image
from services.sku_matcher import match_description

logger = logging.getLogger(__name__)


def parse_and_match(
    image_source: Union[str, Path, bytes],
    api_key: Optional[str] = None,
    model: str = "gpt-4o",
    top_k: int = 5,
) -> list[dict]:
    """Run the full vision → extraction → SKU-matching pipeline.

    Args:
        image_source: Local file path, public HTTPS URL, or raw image bytes.
        api_key: OpenAI API key.  Falls back to the ``OPENAI_API_KEY``
                 environment variable when ``None``.
        model: Vision-capable OpenAI model (default ``gpt-4o``).
        top_k: Number of SKU candidates to fetch per item (only the best
               match is included in the result; the rest are discarded).

    Returns:
        A list of result dicts, one per extracted line item::

            {
                "quantity":    float,        # parsed quantity (1.0 if unspecified)
                "input_text":  str,          # description as read from the image
                "matched_sku": str | None,   # best SKU code, or None
                "confidence":  float,        # 0.0 – 1.0 match confidence
            }

    Raises:
        ValueError: If no OpenAI API key is available.
        RuntimeError: If the Vision API call fails.
    """
    resolved_key = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not resolved_key:
        raise ValueError(
            "OpenAI API key required. Set OPENAI_API_KEY or pass api_key."
        )

    # --- Step 1: Vision extraction ---
    items = extract_items_from_image(image_source, api_key=resolved_key, model=model)
    logger.info("list_parser vision extracted %d items", len(items))

    # --- Step 2: SKU matching ---
    results: list[dict] = []
    for item in items:
        description = item["description"]
        candidates = match_description(description, top_k=top_k)
        best = candidates[0] if candidates else None
        results.append(
            {
                "quantity": item["quantity"],
                "input_text": description,
                "matched_sku": best["sku"] if best else None,
                "confidence": best["confidence"] if best else 0.0,
            }
        )

    logger.info(
        "list_parser matched %d/%d items",
        sum(1 for r in results if r["matched_sku"]),
        len(results),
    )
    return results
