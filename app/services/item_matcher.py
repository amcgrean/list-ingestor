"""Hybrid SKU item matching service."""

from __future__ import annotations

import re
import threading
import time
from typing import Iterable

from sqlalchemy import func

from app.models import ERPItem, ItemAlias, MatchFeedbackEvent
from app.services.fuzzy_matcher import fuzzy_match
from app.services.size_parser import parse_size_and_length
from app.services.vector_index import VectorIndex


_vector_index: VectorIndex | None = None
_index_size = 0
_index_lock = threading.Lock()  # guards index build so only one thread rebuilds at a time


def normalise_description(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"(\d+x\d+)x(\d{1,2})", r"\1 \2ft", text)
    text = re.sub(r"\bpt\b", "pressure treated", text)
    return re.sub(r"\s+", " ", text)


def _ensure_vector_index(erp_items: Iterable[ERPItem], model_name: str) -> VectorIndex:
    global _vector_index, _index_size
    items = list(erp_items)
    # Fast path: check without lock first
    if _vector_index is not None and _index_size == len(items) and _vector_index.model_name == model_name:
        return _vector_index
    # Slow path: only one thread builds the index at a time
    with _index_lock:
        # Re-check inside the lock in case another thread just built it
        if _vector_index is None or _index_size != len(items) or _vector_index.model_name != model_name:
            idx = VectorIndex(model_name=model_name)
            idx.build_index(items)
            _vector_index = idx
            _index_size = len(items)
    return _vector_index


def build_index(catalog, model_name: str):
    idx = _ensure_vector_index(catalog, model_name)
    return idx


def _alias_lookup(description: str):
    normalized = normalise_description(description)
    return ItemAlias.query.filter_by(alias=normalized).first()


def _alias_lookup_batch(descriptions: list[str]) -> dict[str, str]:
    """Return {normalized_description: sku} for all descriptions in one query."""
    normalized = [normalise_description(d) for d in descriptions]
    unique = list(set(normalized))
    if not unique:
        return {}
    try:
        rows = ItemAlias.query.filter(ItemAlias.alias.in_(unique)).all()
    except RuntimeError:
        return {}
    return {row.alias: row.sku for row in rows}


def _catalog_by_sku(erp_items):
    return {item.sku: item for item in erp_items}


def _feedback_counts(normalized_description: str) -> dict[str, int]:
    if not normalized_description:
        return {}

    try:
        rows = (
            MatchFeedbackEvent.query.with_entities(
                MatchFeedbackEvent.final_sku,
                func.count(MatchFeedbackEvent.id),
            )
            .filter(
                MatchFeedbackEvent.normalized_description == normalized_description,
                MatchFeedbackEvent.final_sku.isnot(None),
                MatchFeedbackEvent.was_skipped.is_(False),
            )
            .group_by(MatchFeedbackEvent.final_sku)
            .all()
        )
    except RuntimeError:
        return {}

    return {sku: int(count) for sku, count in rows if sku}


def _feedback_counts_batch(normalized_descriptions: list[str]) -> dict[str, dict[str, int]]:
    """Return {normalized_description: {sku: count}} for all descriptions in one query."""
    unique = list(set(d for d in normalized_descriptions if d))
    if not unique:
        return {}
    try:
        rows = (
            MatchFeedbackEvent.query.with_entities(
                MatchFeedbackEvent.normalized_description,
                MatchFeedbackEvent.final_sku,
                func.count(MatchFeedbackEvent.id),
            )
            .filter(
                MatchFeedbackEvent.normalized_description.in_(unique),
                MatchFeedbackEvent.final_sku.isnot(None),
                MatchFeedbackEvent.was_skipped.is_(False),
            )
            .group_by(MatchFeedbackEvent.normalized_description, MatchFeedbackEvent.final_sku)
            .all()
        )
    except RuntimeError:
        return {}

    result: dict[str, dict[str, int]] = {}
    for norm_desc, sku, count in rows:
        if norm_desc and sku:
            result.setdefault(norm_desc, {})[sku] = int(count)
    return result


def _apply_feedback_rerank(candidates: list[dict], feedback_counts: dict[str, int]) -> list[dict]:
    if not candidates or not feedback_counts:
        return candidates

    total = sum(feedback_counts.values())
    if total <= 0:
        return candidates

    reranked = []
    for candidate in candidates:
        sku = candidate["sku"]
        count = feedback_counts.get(sku, 0)
        ratio = count / total
        boost = min(0.2, (ratio * 0.15) + (min(count, 5) * 0.01))
        updated = dict(candidate)
        updated["feedback_boost"] = round(boost, 4)
        updated["feedback_count"] = count
        updated["confidence_score"] = round(min(updated["confidence_score"] + boost, 1.0), 4)
        reranked.append(updated)

    reranked.sort(key=lambda x: x["confidence_score"], reverse=True)
    return reranked


def match_item(
    description: str,
    erp_items: list[ERPItem],
    model_name: str,
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
):
    results = match_item_candidates(
        description,
        erp_items,
        model_name=model_name,
        fuzzy_weight=fuzzy_weight,
        vector_weight=vector_weight,
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
    size, length = parse_size_and_length(norm_desc)
    feedback_counts = _feedback_counts(norm_desc)

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

        if size and item.size and item.size.lower().replace(" ", "") == size.lower().replace(" ", ""):
            final_score += 0.08
        if length and item.length and str(item.length) == str(length):
            final_score += 0.08

        candidates.append({
            "sku": item.sku,
            "description": item.description,
            "confidence_score": round(min(final_score, 1.0), 4),
            "fuzzy_score": round(f_score, 4),
            "vector_score": round(v_score, 4),
            "size": item.size,
            "length": item.length,
        })

    candidates.sort(key=lambda x: x["confidence_score"], reverse=True)
    candidates = _apply_feedback_rerank(candidates, feedback_counts)
    return candidates[:k]


def match_items_batch(
    descriptions: list[str],
    erp_items: list[ERPItem],
    model_name: str,
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
):
    """Match a batch of descriptions against the catalog.

    Encodes all query descriptions in a single transformer forward pass
    (via search_batch) instead of N separate encode calls, which
    significantly reduces CPU load on single-core hosts like Render.

    Uses two bulk DB queries (one for aliases, one for feedback history)
    regardless of batch size, replacing the previous O(n) per-item queries.
    """
    if not descriptions:
        return []

    by_sku = _catalog_by_sku(erp_items)

    # --- Single bulk alias lookup for all descriptions ---
    alias_map = _alias_lookup_batch(descriptions)  # {norm_desc: sku}

    # Separate alias-resolved items from those that need vector search
    results: dict[int, dict] = {}
    needs_vector: list[tuple[int, str, str, tuple]] = []  # (idx, description, norm_desc, size_length)

    for i, description in enumerate(descriptions):
        norm_desc = normalise_description(description)
        resolved_sku = alias_map.get(norm_desc)
        if resolved_sku and resolved_sku in by_sku:
            item = by_sku[resolved_sku]
            results[i] = {
                "matched_item_code": item.sku,
                "matched_description": item.description,
                "confidence_score": 1.0,
                "fuzzy_score": 1.0,
                "vector_score": 1.0,
                "candidates": [{
                    "sku": item.sku,
                    "description": item.description,
                    "confidence_score": 1.0,
                    "fuzzy_score": 1.0,
                    "vector_score": 1.0,
                    "size": item.size,
                    "length": item.length,
                }],
            }
        else:
            size_length = parse_size_and_length(norm_desc)
            needs_vector.append((i, description, norm_desc, size_length))

    # --- Single bulk feedback query for all remaining descriptions ---
    norm_descs_needing_feedback = [norm_desc for _, _, norm_desc, _ in needs_vector]
    all_feedback = _feedback_counts_batch(norm_descs_needing_feedback)

    # Batch-encode all remaining queries in one transformer call
    if needs_vector and erp_items:
        idx = _ensure_vector_index(erp_items, model_name)
        k = 5
        norm_queries = [norm_desc for _, _, norm_desc, _ in needs_vector]
        batch_hits = idx.search_batch(norm_queries, k=max(k * 2, 10))

        for (orig_idx, description, norm_desc, (size, length)), vector_hits in zip(
            needs_vector, batch_hits
        ):
            feedback_counts = all_feedback.get(norm_desc, {})
            vector_scores = {hit.sku: hit.score for hit in vector_hits}

            candidates = []
            for sku, v_score in vector_scores.items():
                item = by_sku.get(sku)
                if not item:
                    continue
                f_score = fuzzy_match(norm_desc, [item])["score"]
                final_score = (v_score * vector_weight) + (f_score * fuzzy_weight)

                if size and item.size and item.size.lower().replace(" ", "") == size.lower().replace(" ", ""):
                    final_score += 0.08
                if length and item.length and str(item.length) == str(length):
                    final_score += 0.08

                candidates.append({
                    "sku": item.sku,
                    "description": item.description,
                    "confidence_score": round(min(final_score, 1.0), 4),
                    "fuzzy_score": round(f_score, 4),
                    "vector_score": round(v_score, 4),
                    "size": item.size,
                    "length": item.length,
                })

            candidates.sort(key=lambda x: x["confidence_score"], reverse=True)
            candidates = _apply_feedback_rerank(candidates, feedback_counts)
            candidates = candidates[:k]

            if candidates:
                top = candidates[0]
                results[orig_idx] = {
                    "matched_item_code": top["sku"],
                    "matched_description": top["description"],
                    "confidence_score": top["confidence_score"],
                    "fuzzy_score": top["fuzzy_score"],
                    "vector_score": top["vector_score"],
                    "candidates": candidates,
                }
            else:
                results[orig_idx] = _no_match()

            # Yield CPU briefly between items so the web worker stays responsive
            time.sleep(0)
    else:
        for orig_idx, *_ in needs_vector:
            results[orig_idx] = _no_match()

    return [results[i] for i in range(len(descriptions))]


def _no_match():
    return {
        "matched_item_code": None,
        "matched_description": None,
        "confidence_score": 0.0,
        "fuzzy_score": 0.0,
        "vector_score": 0.0,
        "candidates": [],
    }
