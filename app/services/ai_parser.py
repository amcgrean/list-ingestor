"""
AI Parser Service
-----------------
Sends raw OCR text to Claude and returns a list of structured line items
with rich metadata for building-material / lumber-yard workflows.
"""

import json
import logging
import re
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert at reading material lists for Beisser Lumber, a building \
materials and lumber yard.  Contractors submit handwritten or typed lists \
that have been OCR-transcribed (sometimes with errors).

Your job: parse the OCR text into a structured JSON array of line items.

# DOMAIN KNOWLEDGE — BUILDING MATERIALS

Abbreviations you MUST recognise and expand in the description:
  PT = pressure treated, RH = right hand, LH = left hand,
  LVL = laminated veneer lumber, OSB = oriented strand board,
  OSBM = obscure glass (window type), SF = square feet, LF = linear feet,
  GRK = GRK fastener brand, T25 = Torx 25 drive, H2.5 = Simpson H2.5 clip,
  LUS = Simpson LUS joist hanger, TJI = Trus Joist I-joist,
  SPF = spruce-pine-fir, SYP = southern yellow pine, DF = Douglas fir,
  KD = kiln dried, S4S = surfaced 4 sides, #2 = number 2 grade,
  WRC = western red cedar, MCA = micronised copper azole (PT treatment),
  XO/OX = door handing (sliding door configuration — keep exactly as written),
  thru-lok / ledger lock = structural bolt brands

Decking / railing brands and their color lines:
  TimberTech: Antique Leather, Mocha, Pecan, Dark Roast, Coastline, Driftwood, etc.
  Trex: Tiki Torch, Toasted Sand, Clam Shell, Lava Rock, Foggy Wharf, etc.
  Deckorators: Cinnamon Cove, Cape Cod Gray, etc.
  Moisture Shield: Sedona, Tuscany, etc.
  Westbury: railing system (posts have height-specific SKUs: 37", 43", 47")
  Azek / TimberTech trim: PVC trim boards
  LP SmartSide: engineered wood siding

Dimension formats (all mean the same thing):
  2x10x16  |  2-10-16  |  2"x10"x16'  →  normalise to "2x10 16ft"
  5/4x6x16 → "5/4x6 16ft"

# PARSING RULES

1. Each line item MUST include:
   - "quantity": number (default 1 if missing)
   - "description": cleaned-up product description

2. Each line item SHOULD include when present:
   - "brand": brand name if stated or clearly implied
   - "color": color name if stated (keep brand-specific color names exactly)
   - "size": lumber dimension (e.g. "2x10", "5/4x6", "6x6", "4x8")
   - "length": in feet as a number (e.g. 16, 20, 12)
   - "grade": lumber grade if stated ("select", "premium", "#2", "nice", "clear")
   - "category": product category (framing, decking, railing, hardware, trim,
     doors, windows, siding, roofing, fasteners, concrete, misc)

3. FLAGS — set these boolean fields when applicable:
   - "is_crossed_out": true if the OCR text shows [CROSSED OUT] or strikethrough
   - "is_tbd": true if the item says "TBD", "color TBD", "pricing only", or "quote only"
   - "is_approximate": true if quantity is preceded by ~ or "approx" or "about"
   - "color_tbd": true if the item explicitly says the color is not yet chosen

4. TALLY MARKS: OCR may show tally marks as | characters.
   Convert: |||| = 5, |||| | = 6, |||| || = 7, |||| ||| = 8, |||| |||| = 10, etc.
   Groups of 5 (||||) with additional singles after.

5. "Nice" or "select" when applied to framing lumber = premium grade.
   Set grade to "select" and keep it in the description.

6. XO / OX for doors = sliding door handing configuration.
   Keep exactly as written.  This is a critical order detail, NOT a typo.

7. Door/window specs: preserve handing (RH/LH), glass type (OSBM, low-e, clear),
   configuration (XO, OX), and exact sizes.

8. Items marked "TBD" or "pricing only": include in the output with is_tbd=true.
   Do NOT skip them.

9. Crossed-out items: include in the output with is_crossed_out=true.
   Do NOT skip them — we log them for audit.

10. Customer name/address headers and job location notes: skip these entirely.
    They are metadata, not material items.

11. Do NOT infer or add items not in the source text.
12. Merge exact duplicates only if quantity and description are clearly the same.
13. Normalize spelling errors (e.g. "deckin" → "decking").
14. Return ONLY valid JSON — no markdown fences, no commentary.

# OUTPUT FORMAT

[
  {
    "quantity": 25,
    "description": "2x10 SPF #2 joist 16ft pressure treated",
    "brand": null,
    "color": null,
    "size": "2x10",
    "length": 16,
    "grade": "#2",
    "category": "framing",
    "is_crossed_out": false,
    "is_tbd": false,
    "is_approximate": false,
    "color_tbd": false
  },
  {
    "quantity": 100,
    "description": "TimberTech grooved decking 16ft Antique Leather",
    "brand": "TimberTech",
    "color": "Antique Leather",
    "size": "5/4x6",
    "length": 16,
    "grade": null,
    "category": "decking",
    "is_crossed_out": false,
    "is_tbd": false,
    "is_approximate": false,
    "color_tbd": false
  },
  {
    "quantity": 4,
    "description": "Westbury railing post 43 inch black",
    "brand": "Westbury",
    "color": "black",
    "size": null,
    "length": null,
    "grade": null,
    "category": "railing",
    "is_crossed_out": false,
    "is_tbd": false,
    "is_approximate": false,
    "color_tbd": true
  }
]
"""

USER_PROMPT_TEMPLATE = """\
Parse the following material list text into structured JSON line items.
Apply all domain knowledge for building materials, lumber, decking, and hardware.

--- MATERIAL LIST START ---
{ocr_text}
--- MATERIAL LIST END ---

Return ONLY the JSON array.
"""


def _clean_json_response(raw: str) -> str:
    """Strip markdown fences and stray text around a JSON array."""
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    # If there's text before the first [, strip it
    bracket = raw.find("[")
    if bracket > 0:
        raw = raw[bracket:]
    # If there's text after the last ], strip it
    last_bracket = raw.rfind("]")
    if last_bracket >= 0 and last_bracket < len(raw) - 1:
        raw = raw[:last_bracket + 1]
    return raw.strip()


def _validate_item(item: dict) -> dict | None:
    """Validate and normalize a single parsed item dict."""
    if not isinstance(item, dict):
        return None

    description = str(item.get("description", "")).strip()
    if not description:
        return None

    try:
        quantity = float(item.get("quantity", 1))
    except (TypeError, ValueError):
        quantity = 1.0

    # Length as number if possible
    length = item.get("length")
    if length is not None:
        try:
            length = int(length)
        except (TypeError, ValueError):
            try:
                length = float(length)
            except (TypeError, ValueError):
                length = None

    return {
        "quantity": quantity,
        "description": description,
        "brand": str(item.get("brand") or "").strip() or None,
        "color": str(item.get("color") or "").strip() or None,
        "size": str(item.get("size") or "").strip() or None,
        "length": length,
        "grade": str(item.get("grade") or "").strip() or None,
        "category": str(item.get("category") or "").strip() or None,
        "is_crossed_out": bool(item.get("is_crossed_out", False)),
        "is_tbd": bool(item.get("is_tbd", False)),
        "is_approximate": bool(item.get("is_approximate", False)),
        "color_tbd": bool(item.get("color_tbd", False)),
    }


def parse_material_list(
    ocr_text: str,
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> list[dict[str, Any]]:
    """
    Send OCR text to Claude and return a list of structured line-item dicts.
    Raises ValueError on parse failure, anthropic.APIError on API errors.
    """
    if not ocr_text or not ocr_text.strip():
        raise ValueError("OCR text is empty — nothing to parse.")

    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(ocr_text=ocr_text.strip()),
            }
        ],
    )

    text_block = next(
        (block for block in message.content if block.type == "text"),
        None,
    )
    if text_block is None:
        logger.error(
            "Claude response contained no text block. stop_reason=%s content=%r",
            message.stop_reason, message.content,
        )
        raise ValueError(
            f"Claude returned no text content (stop_reason={message.stop_reason!r})."
        )

    raw_content = text_block.text.strip()
    logger.debug("Claude raw response: %s", raw_content[:500])

    if not raw_content:
        raise ValueError(
            f"Claude returned an empty response (stop_reason={message.stop_reason!r})."
        )

    raw_content = _clean_json_response(raw_content)

    if not raw_content:
        raise ValueError("Claude response contained no JSON content.")

    try:
        items = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse Claude response as JSON: %s", raw_content[:500])
        raise ValueError(f"Claude returned invalid JSON: {exc}") from exc

    if not isinstance(items, list):
        raise ValueError("Claude response is not a JSON array.")

    validated = []
    for item in items:
        v = _validate_item(item)
        if v is not None:
            validated.append(v)

    return validated
