"""Fuzzy matcher service for ERP SKU lookup."""

from rapidfuzz import fuzz


def fuzzy_match(query: str, catalog) -> dict:
    """Return best fuzzy match as {sku, score} where score is [0, 1]."""
    best_sku = None
    best_score = 0.0

    for item in catalog:
        score = fuzz.token_set_ratio(query, item.searchable_text) / 100.0
        if score > best_score:
            best_score = score
            best_sku = item.sku

    return {"sku": best_sku, "score": round(best_score, 4)}
