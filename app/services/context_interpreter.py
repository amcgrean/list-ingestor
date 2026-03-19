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
    "You are resolving hierarchical context in a customer or competitor material list. "
    "The source may be handwritten, typed, or mixed, and sparse rows often inherit detail from broader notes or headers. "
    "Input is full-document JSON from a prior extraction pass across one or more uploaded files. "
    "Apply section headers or general notes to following rows until a new header or note overrides them. "
    "Carry shared context across adjacent pages/files when the document structure clearly supports it. "
    "Expand common construction shorthand and obvious OCR misspellings when confidence is high, "
    "such as eng->engineered, pt->pressure treated, w/->with, lvl->LVL, ply->plywood, swng->swinging, surfce->surface. "
    "If a handwritten row is still unclear after using the surrounding context, keep the best normalized form you can but add ambiguity_flags and a short review_reason. "
    "Infer brand, color, product-family, and product-type only when strongly supported. "
    "Mark ambiguity_flags when unclear instead of guessing. "
    "Build normalized_description suitable for ERP SKU matching. "
    "Return strict JSON only with key contextualized_lines."
)


class ContextInterpreter:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def interpret(self, stage_a_lines: list[RawExtractedLine], upload_context: str = "") -> list[ContextualizedLine]:
        if not stage_a_lines:
            return []

        if not self.api_key:
            return self._heuristic_interpret(stage_a_lines)

        payload = [asdict(line) for line in stage_a_lines]
        raw_lookup = {line.line_id: line for line in stage_a_lines}
        client = OpenAI(api_key=self.api_key)
        try:
            prompt = _STAGE_B_PROMPT
            upload_context = " ".join(upload_context.split())
            if upload_context:
                prompt += f" User-provided upload context: {upload_context}."
            response = client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_text", "text": json.dumps({"lines": payload})},
                        ],
                    }
                ],
            )
            parsed = _extract_json(response.output_text)
            lines = parsed.get("contextualized_lines", parsed if isinstance(parsed, list) else [])
            if not self._looks_like_valid_model_output(lines, raw_lookup):
                raise ValueError("Stage B returned invalid or incomplete line identifiers")
            return [self._normalize_context_line(line, raw_lookup=raw_lookup) for line in lines]
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
            normalized = _normalize_handwritten_text(" ".join(part for part in [inherited, text] if part).strip())
            ambiguity = _derive_ambiguity_flags(text, normalized)

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
        raw_text = str(line.get("raw_text") or (raw.raw_text if raw else ""))
        normalized_description = _normalize_handwritten_text(str(line.get("normalized_description", "")).strip())
        combined_ambiguity = _merge_ambiguity_flags(
            list(line.get("ambiguity_flags", []) or []),
            _derive_ambiguity_flags(raw_text, normalized_description),
        )
        review_reason = str(line.get("review_reason", "")).strip() or "; ".join(combined_ambiguity)
        return ContextualizedLine(
            line_id=line_id,
            raw_text=raw_text,
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
            normalized_description=normalized_description,
            ambiguity_flags=combined_ambiguity,
            review_reason=review_reason,
            confidence=float(line.get("confidence", 0.0) or 0.0),
        )

    def _looks_like_valid_model_output(
        self,
        lines: list[Any],
        raw_lookup: dict[str, RawExtractedLine],
    ) -> bool:
        if not isinstance(lines, list):
            return False
        seen_line_ids: set[str] = set()
        for line in lines:
            if not isinstance(line, dict):
                return False
            line_id = str(line.get("line_id", "")).strip()
            if not line_id or line_id not in raw_lookup or line_id in seen_line_ids:
                return False
            seen_line_ids.add(line_id)
        return True


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


_SHORTHAND_REPLACEMENTS = (
    (r"\bw/\b", "with"),
    (r"\bw/o\b", "without"),
    (r"\beng\b", "engineered"),
    (r"\bpt\b", "pressure treated"),
    (r"\btrtd\b", "treated"),
    (r"\blvl\b", "LVL"),
    (r"\bply\b", "plywood"),
    (r"\bswng\b", "swinging"),
    (r"\bsurfce\b", "surface"),
    (r"\bhanle\b", "handle"),
    (r"\bclr\b", "color"),
)


def _normalize_handwritten_text(text: str) -> str:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return ""
    for pattern, replacement in _SHORTHAND_REPLACEMENTS:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized


def _derive_ambiguity_flags(raw_text: str, normalized_description: str) -> list[str]:
    source = f"{raw_text} {normalized_description}".strip()
    if not source:
        return []

    flags: list[str] = []
    patterns = (
        (r"\b(?:misc|unknown|tbd|assorted)\b", "ambiguous_shorthand"),
        (r"\b\d+/[A-Za-z]+\b|\b[A-Za-z]+/\d+\b", "handwritten_fraction_or_ocr_noise"),
        (r"\b(?:lye|surfce|hanle|swng)\b", "ocr_spelling_uncertain"),
        (r"\b\d+x\d+x\d+/\d+\b|\b\d+-\d+\b", "dimension_parse_uncertain"),
        (r"\([A-Za-z]\)", "single_letter_annotation"),
    )
    for pattern, flag in patterns:
        if re.search(pattern, source, flags=re.IGNORECASE):
            flags.append(flag)
    return flags


def _merge_ambiguity_flags(*flag_lists: list[str]) -> list[str]:
    merged: list[str] = []
    for flags in flag_lists:
        for flag in flags:
            if flag and flag not in merged:
                merged.append(flag)
    return merged
