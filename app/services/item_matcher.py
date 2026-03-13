"""Hybrid SKU item matching service."""

from __future__ import annotations

import re
from typing import Iterable

from app.models import ERPItem, ItemAlias
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


def match_item(
    description: str,
    erp_items: list[ERPItem],
    model_name: str,
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
<<<<<<< HEAD
    recency_weight: float = 0.15,
    branch_system_id: str = "",
) -> MatchResult:
    """
    Find the best-matching ERP catalog item for a given extracted description.

    Returns a dict with:
        matched_item_code   – str or None
        matched_description – str or None
        confidence_score    – float 0-1
        fuzzy_score         – float 0-1
        vector_score        – float 0-1
    """
    if not erp_items:
        return _no_match()

    norm_desc = normalise_description(description)

    # --- Fuzzy scoring ---
    fuzzy_scores = []
    for item in erp_items:
        norm_catalog = normalise_description(item.searchable_text)
        score = fuzz.token_set_ratio(norm_desc, norm_catalog) / 100.0
        fuzzy_scores.append(score)

    # --- Vector scoring ---
    query_emb = _embed([norm_desc], model_name)[0]

    vector_scores = []
    for item in erp_items:
        if item.embedding is not None:
            item_emb = np.array(item.embedding, dtype=np.float32)
            # Normalise in case it wasn't stored normalised
            norm = np.linalg.norm(item_emb)
            if norm > 0:
                item_emb = item_emb / norm
            sim = cosine_similarity(query_emb, item_emb)
            # Clamp to [0, 1] — cosine can be slightly negative for dissimilar texts
            vector_scores.append(max(0.0, sim))
        else:
            vector_scores.append(0.0)

    # --- Combined score ---
    # Try branch specific items first
    best_idx = -1
    best_score = -1.0
    
    for i, item in enumerate(erp_items):
        score = (fuzzy_scores[i] * fuzzy_weight) + (vector_scores[i] * vector_weight)
        
        # Add recency tiebreaker
        hw = getattr(item, "sold_weight", 0.0)
        score += hw * recency_weight
        
        # Add slight penalty for out-of-branch catalog items if a branch is selected
        if branch_system_id and getattr(item, 'branch_system_id', '') != branch_system_id:
            score -= 0.15
            
        if score > best_score:
            best_score = score
            best_idx = i

    best_item = erp_items[best_idx]

    return {
        "matched_item_code": best_item.item_code,
        "matched_description": best_item.description,
        "confidence_score": round(best_score, 4),
        "fuzzy_score": round(fuzzy_scores[best_idx], 4),
        "vector_score": round(vector_scores[best_idx], 4),
=======
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
>>>>>>> origin
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
    return candidates[:k]


def match_items_batch(
    descriptions: list[str],
    erp_items: list[ERPItem],
    model_name: str,
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
<<<<<<< HEAD
    recency_weight: float = 0.15,
    branch_system_id: str = "",
) -> list[MatchResult]:
    """Batch version — pre-embeds all queries in one shot for efficiency."""
    if not erp_items:
        return [_no_match() for _ in descriptions]

    norm_descs = [normalise_description(d) for d in descriptions]

    # Embed all queries at once
    query_embs = _embed(norm_descs, model_name)

    # Build catalog embedding matrix
    catalog_embs = []
    for item in erp_items:
        if item.embedding is not None:
            emb = np.array(item.embedding, dtype=np.float32)
            norm = np.linalg.norm(emb)
            catalog_embs.append(emb / norm if norm > 0 else emb)
        else:
            catalog_embs.append(np.zeros(query_embs.shape[1], dtype=np.float32))

    catalog_matrix = np.stack(catalog_embs)  # (n_items, dim)

    results = []
    for i, (norm_desc, q_emb) in enumerate(zip(norm_descs, query_embs)):
        fuzzy_scores = [
            fuzz.token_set_ratio(norm_desc, normalise_description(item.searchable_text)) / 100.0
            for item in erp_items
        ]
        vector_sims = np.clip(catalog_matrix @ q_emb, 0, 1).tolist()

        best_idx = -1
        best_score = -1.0
        
        for j, item in enumerate(erp_items):
            score = (fuzzy_scores[j] * fuzzy_weight) + (vector_sims[j] * vector_weight)
            
            # Recency tiebreaker
            hw = getattr(item, "sold_weight", 0.0)
            score += hw * recency_weight
            
            # Penalty for out-of-branch
            if branch_system_id and getattr(item, 'branch_system_id', '') != branch_system_id:
                score -= 0.15
                
            if score > best_score:
                best_score = score
                best_idx = j

        best_item = erp_items[best_idx]

        results.append({
            "matched_item_code": best_item.item_code,
            "matched_description": best_item.description,
            "confidence_score": round(best_score, 4),
            "fuzzy_score": round(fuzzy_scores[best_idx], 4),
            "vector_score": round(vector_sims[best_idx], 4),
        })

    return results
=======
):
    matches = []
    for description in descriptions:
        result = match_item(
            description,
            erp_items,
            model_name=model_name,
            fuzzy_weight=fuzzy_weight,
            vector_weight=vector_weight,
        )
        matches.append(result)
    return matches
>>>>>>> origin


def _no_match():
    return {
        "matched_item_code": None,
        "matched_description": None,
        "confidence_score": 0.0,
        "fuzzy_score": 0.0,
        "vector_score": 0.0,
        "candidates": [],
    }
