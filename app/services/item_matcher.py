"""
Item Matcher Service
--------------------
Matches extracted descriptions to ERP catalog items using a hybrid approach:

  1. Fuzzy text matching via RapidFuzz (token_set_ratio)
  2. Vector cosine similarity via sentence-transformers

  final_score = (fuzzy_score * FUZZY_WEIGHT) + (vector_score * VECTOR_WEIGHT)

Also normalises common lumber/construction formats before matching.
"""

import logging
import re
from typing import Optional

import numpy as np
from rapidfuzz import fuzz
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# Lazy-loaded singleton so the model is downloaded only once
_model: Optional[SentenceTransformer] = None


def _get_model(model_name: str) -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info("Loading sentence-transformers model: %s", model_name)
        _model = SentenceTransformer(model_name)
    return _model


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_LENGTH_WORDS = {
    "eight": "8", "ten": "10", "twelve": "12", "fourteen": "14",
    "sixteen": "16", "eighteen": "18", "twenty": "20",
    "twentyfour": "24", "twenty four": "24",
}

_UNIT_NORMALISATION = {
    r"\blf\b": "linear feet",
    r"\blin\b": "linear",
    r"\bft\b": "ft",
    r"\bpcs?\b": "piece",
    r"\bpkgs?\b": "package",
    r"\blbs?\b": "lb",
    r"\bpd\b": "lb",
    r"\bsf\b": "square feet",
    r"\bbf\b": "board feet",
    r"\bea\b": "each",
}


def normalise_description(text: str) -> str:
    """
    Convert common construction shorthand to expanded form so fuzzy / vector
    matching has more surface area to work with.

    Examples:
        "2x10x16"      → "2x10 16ft"
        "2x10 sixteen" → "2x10 16ft"
        "3/4 cdx ply"  → "3/4 cdx plywood"
    """
    text = text.lower().strip()

    # Expand written-out lengths ("sixteen" → "16")
    for word, digit in _LENGTH_WORDS.items():
        text = re.sub(r"\b" + word + r"\b", digit, text)

    # Expand unit abbreviations
    for pattern, replacement in _UNIT_NORMALISATION.items():
        text = re.sub(pattern, replacement, text)

    # "2x10x16" → "2x10 16ft"
    text = re.sub(r"(\d+x\d+)x(\d+)", r"\1 \2ft", text)

    # "2x10 16" (number after dimension) → "2x10 16ft"
    text = re.sub(r"(\d+x\d+)\s+(\d+)(?!\s*ft|\s*inch|\s*in\b)", r"\1 \2ft", text)

    # Expand "ply" → "plywood"
    text = re.sub(r"\bply\b", "plywood", text)

    # Expand "pt" → "pressure treated" (common in lumber)
    text = re.sub(r"\bpt\b", "pressure treated", text)

    return text.strip()


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

def _embed(texts: list[str], model_name: str) -> np.ndarray:
    model = _get_model(model_name)
    return model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two normalised vectors (dot product)."""
    return float(np.dot(a, b))


# ---------------------------------------------------------------------------
# Catalog embedding pre-computation
# ---------------------------------------------------------------------------

def compute_catalog_embeddings(erp_items: list, model_name: str) -> None:
    """
    Compute and store embeddings for all ERPItem objects in place.
    Call this after loading a new catalog or on first run.
    """
    if not erp_items:
        return

    texts = [normalise_description(item.searchable_text) for item in erp_items]
    embeddings = _embed(texts, model_name)

    for item, emb in zip(erp_items, embeddings):
        item.embedding = emb.tolist()

    logger.info("Computed embeddings for %d ERP items.", len(erp_items))


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

MatchResult = dict  # typed for readability


def match_item(
    description: str,
    erp_items: list,
    model_name: str,
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
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
    }


def match_items_batch(
    descriptions: list[str],
    erp_items: list,
    model_name: str,
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
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


def _no_match() -> MatchResult:
    return {
        "matched_item_code": None,
        "matched_description": None,
        "confidence_score": 0.0,
        "fuzzy_score": 0.0,
        "vector_score": 0.0,
    }
