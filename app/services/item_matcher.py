"""
Item Matcher Service
--------------------
Matches extracted descriptions to ERP catalog items using a hybrid approach:

  1. Fuzzy text matching via RapidFuzz (token_set_ratio)
  2. TF-IDF cosine similarity via scikit-learn

  final_score = (fuzzy_score * FUZZY_WEIGHT) + (tfidf_score * VECTOR_WEIGHT)

Also normalises common lumber/construction formats before matching.

Memory note
-----------
The previous implementation used sentence-transformers (PyTorch) which
required ~300 MB RAM, exceeding the 512 MB limit on Render's starter plan.
scikit-learn TF-IDF achieves comparable accuracy for short item descriptions
at a fraction of the cost (~5 MB).  The vectorizer is kept in a module-level
cache so it is fitted once per catalog and reused across requests.
"""

import logging
import re
from typing import Optional

import numpy as np
from rapidfuzz import fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity as sk_cosine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory TF-IDF state
# Rebuilt on catalog upload; lazily rebuilt after a server restart.
# ---------------------------------------------------------------------------

_vectorizer: Optional[TfidfVectorizer] = None
_catalog_matrix = None          # scipy sparse (n_items, vocab)
_catalog_item_ids: list = []    # ERPItem.id values aligned with matrix rows


def _build_vectorizer(erp_items: list) -> None:
    """Fit TF-IDF on the current catalog and cache the result globally."""
    global _vectorizer, _catalog_matrix, _catalog_item_ids
    texts = [normalise_description(item.searchable_text) for item in erp_items]
    vec = TfidfVectorizer(
        analyzer="word",
        ngram_range=(1, 2),
        min_df=1,
        sublinear_tf=True,
    )
    _catalog_matrix = vec.fit_transform(texts)
    _vectorizer = vec
    _catalog_item_ids = [item.id for item in erp_items]
    logger.info("Built TF-IDF vectorizer for %d catalog items.", len(erp_items))


def _ensure_vectorizer(erp_items: list) -> bool:
    """Return True (and rebuild if needed) when the vectorizer is ready."""
    current_ids = [item.id for item in erp_items]
    if _vectorizer is None or _catalog_item_ids != current_ids:
        if not erp_items:
            return False
        _build_vectorizer(erp_items)
    return True


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
    Convert common construction shorthand to expanded form so fuzzy / TF-IDF
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
# Catalog pre-computation (called after catalog upload)
# ---------------------------------------------------------------------------

def compute_catalog_embeddings(erp_items: list, model_name: str = "") -> None:
    """
    Build and cache the TF-IDF vectorizer for the current catalog.

    The ``model_name`` parameter is accepted for API compatibility but ignored;
    the vectorizer is always TF-IDF.
    """
    if erp_items:
        _build_vectorizer(erp_items)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

MatchResult = dict  # typed for readability


def match_item(
    description: str,
    erp_items: list,
    model_name: str = "",
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
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
    fuzzy_scores = [
        fuzz.token_set_ratio(norm_desc, normalise_description(item.searchable_text)) / 100.0
        for item in erp_items
    ]

    # --- TF-IDF vector scoring ---
    vector_scores = _tfidf_scores(norm_desc, erp_items)

    combined = [
        (f * fuzzy_weight) + (v * vector_weight)
        for f, v in zip(fuzzy_scores, vector_scores)
    ]

    best_idx = int(np.argmax(combined))
    best_item = erp_items[best_idx]

    return {
        "matched_item_code": best_item.item_code,
        "matched_description": best_item.description,
        "confidence_score": round(combined[best_idx], 4),
        "fuzzy_score": round(fuzzy_scores[best_idx], 4),
        "vector_score": round(vector_scores[best_idx], 4),
    }


def match_items_batch(
    descriptions: list[str],
    erp_items: list,
    model_name: str = "",
    fuzzy_weight: float = 0.4,
    vector_weight: float = 0.6,
) -> list[MatchResult]:
    """Batch version — vectorises all queries in one shot for efficiency."""
    if not erp_items:
        return [_no_match() for _ in descriptions]

    norm_descs = [normalise_description(d) for d in descriptions]

    # TF-IDF scores for all queries at once
    if _ensure_vectorizer(erp_items):
        query_matrix = _vectorizer.transform(norm_descs)
        # (n_queries, n_items)
        sim_matrix = sk_cosine(query_matrix, _catalog_matrix)
        all_vector_scores = np.clip(sim_matrix, 0, 1).tolist()
    else:
        all_vector_scores = [[0.0] * len(erp_items)] * len(norm_descs)

    results = []
    for i, norm_desc in enumerate(norm_descs):
        fuzzy_scores = [
            fuzz.token_set_ratio(norm_desc, normalise_description(item.searchable_text)) / 100.0
            for item in erp_items
        ]
        vector_scores = all_vector_scores[i]

        combined = [
            (f * fuzzy_weight) + (v * vector_weight)
            for f, v in zip(fuzzy_scores, vector_scores)
        ]

        best_idx = int(np.argmax(combined))
        best_item = erp_items[best_idx]

        results.append({
            "matched_item_code": best_item.item_code,
            "matched_description": best_item.description,
            "confidence_score": round(combined[best_idx], 4),
            "fuzzy_score": round(fuzzy_scores[best_idx], 4),
            "vector_score": round(vector_scores[best_idx], 4),
        })

    return results


def _tfidf_scores(norm_query: str, erp_items: list) -> list[float]:
    """Return per-item TF-IDF cosine similarity scores for a single query."""
    if not _ensure_vectorizer(erp_items):
        return [0.0] * len(erp_items)
    q_vec = _vectorizer.transform([norm_query])
    sims = sk_cosine(q_vec, _catalog_matrix)[0]
    return np.clip(sims, 0, 1).tolist()


def _no_match() -> MatchResult:
    return {
        "matched_item_code": None,
        "matched_description": None,
        "confidence_score": 0.0,
        "fuzzy_score": 0.0,
        "vector_score": 0.0,
    }
