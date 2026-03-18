from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawExtractedLine:
    line_id: str
    raw_text: str
    section_header: str = ""
    section_type: str = "unknown"
    quantity_raw: str = ""
    quantity: float = 1.0
    dimensions_raw: str = ""
    length: str = ""
    width: str = ""
    height: str = ""
    unit: str = ""
    indentation_level: int = 0
    bullet_style: str = ""
    source_page: int = 1
    source_order: int = 0
    confidence: float = 0.0
    unresolved_tokens: list[str] = field(default_factory=list)


@dataclass
class ContextualizedLine:
    line_id: str
    raw_text: str
    inherited_section_header: str = ""
    brand: str = ""
    color: str = ""
    product_family: str = ""
    product_type: str = ""
    profile: str = ""
    material: str = ""
    finish: str = ""
    dimensions: str = ""
    quantity: float = 1.0
    inferred_use: str = ""
    accessory_for_line_id: str = ""
    normalized_description: str = ""
    ambiguity_flags: list[str] = field(default_factory=list)
    review_reason: str = ""
    confidence: float = 0.0


@dataclass
class MatchReadyLine:
    line_id: str
    quantity: float
    raw_text: str
    normalized_description: str
    match_text: str
    brand: str = ""
    color: str = ""
    product_family: str = ""
    product_type: str = ""
    size: str = ""
    length: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    ambiguity_flags: list[str] = field(default_factory=list)
    needs_review: bool = False
    review_reason: str = ""
    section_header: str = ""
