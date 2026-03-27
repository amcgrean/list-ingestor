"""Hybrid SKU item matching service with brand/color-aware scoring."""

from __future__ import annotations

import re
from typing import Iterable

from app.models import ERPItem, ItemAlias
from app.services.fuzzy_matcher import fuzzy_match
from app.services.size_parser import parse_size_and_length
from app.services.vector_index import VectorIndex


_vector_index: VectorIndex | None = None
_index_size = 0

# ── Lumber-yard abbreviation dictionary ──────────────────────────────────

_ABBREVIATIONS = {
    r"\bpt\b": "pressure treated",
    r"\brh\b": "right hand",
    r"\blh\b": "left hand",
    r"\blvl\b": "laminated veneer lumber",
    r"\bosb\b": "oriented strand board",
    r"\bspf\b": "spruce pine fir",
    r"\bsyp\b": "southern yellow pine",
    r"\bdf\b": "douglas fir",
    r"\bkd\b": "kiln dried",
    r"\bs4s\b": "surfaced four sides",
    r"\bwrc\b": "western red cedar",
    r"\bmca\b": "micronized copper azole",
    r"\blf\b": "linear feet",
    r"\bsf\b": "square feet",
    r"\bbf\b": "board feet",
    r"\btji\b": "trus joist i-joist",
    r"\bgrk\b": "GRK fastener",
    r"\blus\b": "Simpson LUS joist hanger",
    r"\bpvc\b": "PVC",
    r"\bhdg\b": "hot dip galvanized",
    r"\bss\b": "stainless steel",
    r"\bea\b": "each",
}

# Known building-material brands (lowercase) for matching boost
_KNOWN_BRANDS = {
    "timbertech", "trex", "deckorators", "moisture shield", "moistureshield",
    "westbury", "azek", "lp", "lp smartside", "gerkin", "simpson",
    "grk", "fastenmaster", "spax", "fiberon", "wolf", "versatex",
    "kleer", "boral", "royal", "certainteed", "gaf", "owens corning",
    "james hardie", "hardie", "andersen", "pella", "marvin", "milgard",
}


def normalise_description(text: str) -> str:
    """Normalize a description for matching: lowercase, expand abbreviations, clean whitespace."""
    text = (text or "").lower().strip()
    # Normalize dimension formats: 2x10x16, 2-10-16, 2"x10"x16' → 2x10 16ft
    text = re.sub(r'["\']', '', text)  # strip inch/foot marks
    text = re.sub(r"(\d+)\s*[-xX]\s*(\d+)\s*[-xX]\s*(\d{1,2})", r"\1x\2 \3ft", text)
    text = re.sub(r"(\d+/\d+)\s*[-xX]\s*(\d+)\s*[-xX]\s*(\d{1,2})", r"\1x\2 \3ft", text)
    # Expand abbreviations
    for pattern, replacement in _ABBREVIATIONS.items():
        text = re.sub(pattern, replacement, text)
    return re.sub(r"\s+", " ", text).strip()


def _ensure_vector_index(erp_items: Iterable[ERPItem], model_name: str) -> VectorIndex:
    global _vector_index, _index_size
    items = list(erp_items)
    if _vector_index is None or _index_size != len(items) or _vector_index.model_name != model_name:
        _vector_index = VectorIndex(model_name=model_name)
        _vector_index.build_index(items)
        _index_size = len(items)
    return _vector_index


def build_index(catalog, model_name: str):
    return _ensure_vector_index(catalog, model_name)


def _alias_lookup(description: str):
    normalized = normalise_description(description)
    return ItemAlias.query.filter_by(alias=normalized).first()


def _catalog_by_sku(erp_items):
    return {item.sku: item for item in erp_items}


def _brand_match_bonus(parsed_brand: str | None, catalog_item: ERPItem) -> float:
    """Return a confidence bonus if the parsed brand matches the catalog item's brand."""
    if not parsed_brand:
        return 0.0
    pb = parsed_brand.lower().strip()
    cb = (catalog_item.brand or "").lower().strip()
    if not cb:
        # Check if brand appears in description or keywords
        searchable = (catalog_item.description + " " + (catalog_item.keywords or "")).lower()
        if pb in searchable:
            return 0.06
        return 0.0
    if pb == cb or pb in cb or cb in pb:
        return 0.10
    return -0.05  # penalty for brand mismatch


def _color_match_bonus(parsed_color: str | None, catalog_item: ERPItem) -> float:
    """Return a confidence bonus if the parsed color matches the catalog item."""
    if not parsed_color:
        return 0.0
    pc = parsed_color.lower().strip()
    cc = (getattr(catalog_item, 'color', '') or "").lower().strip()
    searchable = (catalog_item.description + " " + (catalog_item.keywords or "")).lower()
    if cc and (pc == cc or pc in cc or cc in pc):
        return 0.10
    if pc in searchable:
        return 0.06
    return -0.03  # mild penalty for color mismatch


def match_item(
    description: str,
    erp_items: list[ERPItem],
    model_name: str,
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
    parsed_brand: str | None = None,
    parsed_color: str | None = None,
    parsed_size: str | None = None,
    parsed_length: float | None = None,
):
    results = match_item_candidates(
        description,
        erp_items,
        model_name=model_name,
        fuzzy_weight=fuzzy_weight,
        vector_weight=vector_weight,
        parsed_brand=parsed_brand,
        parsed_color=parsed_color,
        parsed_size=parsed_size,
        parsed_length=parsed_length,
        k=5,
    )
    if not results:
        return _no_match()

    top = results[0]
    return {
        "matched_item_code": top["sku"],
        "matched_description": top["description"],
        "confidence_score": top["confidence_score"],
        "fuzzy_score": top["fuzzy_score"],
        "vector_score": top["vector_score"],
        "candidates": results,
    }


def match_item_candidates(
    description: str,
    erp_items: list[ERPItem],
    model_name: str,
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
    parsed_brand: str | None = None,
    parsed_color: str | None = None,
    parsed_size: str | None = None,
    parsed_length: float | None = None,
    k: int = 5,
):
    if not erp_items:
        return []

    alias = _alias_lookup(description)
    by_sku = _catalog_by_sku(erp_items)
    if alias and alias.sku in by_sku:
        item = by_sku[alias.sku]
        return [{
            "sku": item.sku,
            "description": item.description,
            "confidence_score": 1.0,
            "fuzzy_score": 1.0,
            "vector_score": 1.0,
            "size": item.size,
            "length": item.length,
        }]

    norm_desc = normalise_description(description)

    # Use AI-parsed size/length if available, fall back to regex extraction
    if parsed_size:
        size = parsed_size
    else:
        size, _ = parse_size_and_length(norm_desc)
    if parsed_length:
        length = str(int(parsed_length))
    else:
        _, length = parse_size_and_length(norm_desc)

    idx = _ensure_vector_index(erp_items, model_name)
    vector_hits = idx.search(norm_desc, k=max(k * 2, 10))
    vector_scores = {hit.sku: hit.score for hit in vector_hits}

    candidates = []
    for sku, v_score in vector_scores.items():
        item = by_sku.get(sku)
        if not item:
            continue
        f_score = fuzzy_match(norm_desc, [item])["score"]
        final_score = (v_score * vector_weight) + (f_score * fuzzy_weight)

        # Size/length bonus
        if size and item.size and item.size.lower().replace(" ", "") == size.lower().replace(" ", ""):
            final_score += 0.08
        if length and item.length and str(item.length) == str(length):
            final_score += 0.08

        # Brand/color bonus (uses AI-parsed structured fields)
        final_score += _brand_match_bonus(parsed_brand, item)
        final_score += _color_match_bonus(parsed_color, item)

        candidates.append({
            "sku": item.sku,
            "description": item.description,
            "confidence_score": round(max(min(final_score, 1.0), 0.0), 4),
            "fuzzy_score": round(f_score, 4),
            "vector_score": round(v_score, 4),
            "size": item.size,
            "length": item.length,
        })

    candidates.sort(key=lambda x: x["confidence_score"], reverse=True)
    return candidates[:k]


def match_items_batch(
    parsed_items: list[dict],
    erp_items: list[ERPItem],
    model_name: str,
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
):
    """Match a batch of parsed items. Accepts the full parsed item dicts (not just descriptions)."""
    matches = []
    for item in parsed_items:
        # Support both old format (plain string) and new format (dict with metadata)
        if isinstance(item, str):
            desc = item
            brand = color = size = None
            length = None
        else:
            desc = item.get("description", "")
            brand = item.get("brand")
            color = item.get("color")
            size = item.get("size")
            length = item.get("length")

        result = match_item(
            desc,
            erp_items,
            model_name=model_name,
            fuzzy_weight=fuzzy_weight,
            vector_weight=vector_weight,
            parsed_brand=brand,
            parsed_color=color,
            parsed_size=size,
            parsed_length=length,
        )
        matches.append(result)
    return matches


def _no_match():
    return {
        "matched_item_code": None,
        "matched_description": None,
        "confidence_score": 0.0,
        "fuzzy_score": 0.0,
        "vector_score": 0.0,
        "candidates": [],
    }
