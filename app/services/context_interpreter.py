from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from typing import Any

from openai import OpenAI

from app.services.parse_types import ContextualizedLine, RawExtractedLine

logger = logging.getLogger(__name__)

_STAGE_B_PROMPT = (
    "You are resolving hierarchical context in a contractor material list. "
    "Input is full-document JSON from a prior extraction pass. "
    "Apply section headers to following rows until a new header appears. "
    "Infer brand/color/product-family/product-type only when strongly supported. "
    "Mark ambiguity_flags when unclear instead of guessing. "
    "Build normalized_description suitable for ERP SKU matching. "
    "Return strict JSON only with key contextualized_lines."
)


class ContextInterpreter:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def interpret(self, stage_a_lines: list[RawExtractedLine]) -> list[ContextualizedLine]:
        if not stage_a_lines:
            return []

        if not self.api_key:
            return self._heuristic_interpret(stage_a_lines)

        payload = [asdict(line) for line in stage_a_lines]
        client = OpenAI(api_key=self.api_key)
        try:
            response = client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": _STAGE_B_PROMPT},
                            {"type": "input_text", "text": json.dumps({"lines": payload})},
                        ],
                    }
                ],
            )
            parsed = _extract_json(response.output_text)
            lines = parsed.get("contextualized_lines", parsed if isinstance(parsed, list) else [])
            return [self._normalize_context_line(line, raw_lookup={r.line_id: r for r in stage_a_lines}) for line in lines]
        except Exception:
            logger.exception("Stage B interpretation failed; using heuristic interpretation")
            return self._heuristic_interpret(stage_a_lines)

    def _heuristic_interpret(self, stage_a_lines: list[RawExtractedLine]) -> list[ContextualizedLine]:
        current_header = ""
        contextualized: list[ContextualizedLine] = []
        for line in stage_a_lines:
            if line.section_type == "header" or (line.section_header and not line.raw_text):
                current_header = line.section_header or line.raw_text
                continue

            inherited = line.section_header or current_header
            text = line.raw_text
            normalized = " ".join(part for part in [inherited, text] if part).strip()
            ambiguity: list[str] = []
            if re.search(r"\b(?:misc|unknown|tbd|assorted)\b", text, flags=re.IGNORECASE):
                ambiguity.append("ambiguous_shorthand")

            contextualized.append(
                ContextualizedLine(
                    line_id=line.line_id,
                    raw_text=text,
                    inherited_section_header=inherited,
                    product_family=inherited,
                    dimensions=line.dimensions_raw,
                    quantity=line.quantity,
                    normalized_description=normalized,
                    ambiguity_flags=ambiguity,
                    review_reason="; ".join(ambiguity),
                    confidence=0.65,
                )
            )
        return contextualized

    def _normalize_context_line(self, line: dict[str, Any], raw_lookup: dict[str, RawExtractedLine]) -> ContextualizedLine:
        line_id = str(line.get("line_id", ""))
        raw = raw_lookup.get(line_id)
        return ContextualizedLine(
            line_id=line_id,
            raw_text=str(line.get("raw_text") or (raw.raw_text if raw else "")),
            inherited_section_header=str(line.get("inherited_section_header", "")),
            brand=str(line.get("brand", "")),
            color=str(line.get("color", "")),
            product_family=str(line.get("product_family", "")),
            product_type=str(line.get("product_type", "")),
            profile=str(line.get("profile", "")),
            material=str(line.get("material", "")),
            finish=str(line.get("finish", "")),
            dimensions=str(line.get("dimensions", "")),
            quantity=float(line.get("quantity", raw.quantity if raw else 1) or 1),
            inferred_use=str(line.get("inferred_use", "")),
            accessory_for_line_id=str(line.get("accessory_for_line_id", "")),
            normalized_description=str(line.get("normalized_description", "")).strip(),
            ambiguity_flags=list(line.get("ambiguity_flags", []) or []),
            review_reason=str(line.get("review_reason", "")),
            confidence=float(line.get("confidence", 0.0) or 0.0),
        )


def _extract_json(text: str) -> Any:
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", cleaned)
        if not match:
            raise
        return json.loads(match.group(1))
