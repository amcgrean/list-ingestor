"""Hybrid SKU item matching service."""

from __future__ import annotations

import re
import time
from typing import Iterable

from sqlalchemy import func

from app.models import ERPItem, ItemAlias, MatchFeedbackEvent
from app.services.fuzzy_matcher import fuzzy_match
from app.services.size_parser import parse_size_and_length
from app.services.vector_index import VectorIndex


_vector_index: VectorIndex | None = None
_index_size = 0


def normalise_description(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"(\d+x\d+)x(\d{1,2})", r"\1 \2ft", text)
    text = re.sub(r"\bpt\b", "pressure treated", text)
    return re.sub(r"\s+", " ", text)


def _ensure_vector_index(erp_items: Iterable[ERPItem], model_name: str) -> VectorIndex:
    global _vector_index, _index_size
    items = list(erp_items)
    if _vector_index is None or _index_size != len(items) or _vector_index.model_name != model_name:
        _vector_index = VectorIndex(model_name=model_name)
        _vector_index.build_index(items)
        _index_size = len(items)
    return _vector_index


def build_index(catalog, model_name: str):
    idx = _ensure_vector_index(catalog, model_name)
    return idx


def _alias_lookup(description: str):
    normalized = normalise_description(description)
    return ItemAlias.query.filter_by(alias=normalized).first()


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
    """
    if not descriptions:
        return []

    by_sku = _catalog_by_sku(erp_items)

    # Separate alias-resolved items from those that need vector search
    alias_results: dict[int, dict] = {}
    needs_vector: list[tuple[int, str, str, tuple, dict]] = []  # (idx, description, norm_desc, size_length, feedback)

    for i, description in enumerate(descriptions):
        alias = _alias_lookup(description)
        if alias and alias.sku in by_sku:
            item = by_sku[alias.sku]
            alias_results[i] = {
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
            norm_desc = normalise_description(description)
            size_length = parse_size_and_length(norm_desc)
            feedback = _feedback_counts(norm_desc)
            needs_vector.append((i, description, norm_desc, size_length, feedback))

    # Batch-encode all remaining queries in one transformer call
    if needs_vector and erp_items:
        idx = _ensure_vector_index(erp_items, model_name)
        k = 5
        norm_queries = [norm_desc for _, _, norm_desc, _, _ in needs_vector]
        batch_hits = idx.search_batch(norm_queries, k=max(k * 2, 10))

        for (orig_idx, description, norm_desc, (size, length), feedback_counts), vector_hits in zip(
            needs_vector, batch_hits
        ):
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
                alias_results[orig_idx] = {
                    "matched_item_code": top["sku"],
                    "matched_description": top["description"],
                    "confidence_score": top["confidence_score"],
                    "fuzzy_score": top["fuzzy_score"],
                    "vector_score": top["vector_score"],
                    "candidates": candidates,
                }
            else:
                alias_results[orig_idx] = _no_match()

            # Yield CPU briefly between items so the web worker stays responsive
            time.sleep(0)
    else:
        for orig_idx, *_ in needs_vector:
            alias_results[orig_idx] = _no_match()

    return [alias_results[i] for i in range(len(descriptions))]


def _no_match():
    return {
        "matched_item_code": None,
        "matched_description": None,
        "confidence_score": 0.0,
        "fuzzy_score": 0.0,
        "vector_score": 0.0,
        "candidates": [],
    }
