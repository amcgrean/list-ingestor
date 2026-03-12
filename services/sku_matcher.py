"""
SKU Matcher Service
-------------------
Matches material list descriptions against the Neon Postgres SKU database.

Matching priority:
  1. Exact alias lookup  — returns the previously learned SKU immediately.
  2. Keyword / normalized_name ILIKE search — token-based scoring.
  3. Optional fuzzy scoring via rapidfuzz when it is installed.

User corrections are persisted via ``record_alias()`` so the system improves
automatically over time.

Environment variable required:
  DATABASE_URL — psycopg-compatible Postgres connection string.

Database tables consumed (created by ``database/init.sql``):
  skus          — canonical SKU catalogue.
  aliases       — learned alias → SKU mappings.
  match_history — audit trail of every match decision.
"""

import logging
import os
import re
from typing import Optional

import psycopg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    # Normalise legacy postgres:// scheme used by some hosting providers
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def _connect() -> psycopg.Connection:
    url = _get_db_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(url)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip non-alphanumeric punctuation."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s/\-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Public matching API
# ---------------------------------------------------------------------------

def match_description(description: str, top_k: int = 5) -> list[dict]:
    """Match a free-text description to SKUs in the database.

    Args:
        description: Raw material description from the extracted list.
        top_k: Maximum number of candidate SKUs to return.

    Returns:
        List of dicts ordered by descending confidence::

            [{"sku": "0210tre16", "description": "2x10 Treated 16ft", "confidence": 0.92}]

        Returns an empty list if no match is found or the database is
        unreachable.
    """
    norm = normalise(description)

    try:
        with _connect() as conn:
            # --- Priority 1: alias table ---
            result = _alias_lookup(conn, norm)
            if result:
                return result

            # --- Priority 2: keyword / normalized_name search ---
            return _keyword_search(conn, norm, top_k)

    except RuntimeError:
        raise
    except Exception:
        logger.exception("SKU match failed for description: %.100s", description)
        return []


def record_alias(alias: str, sku: str) -> None:
    """Persist an alias → SKU mapping and increment its usage counter.

    If the alias already exists with a different SKU it is updated so the
    most-recently confirmed correction always wins.

    Args:
        alias: The raw or normalised input text used as the alias.
        sku:   The confirmed SKU code.
    """
    norm = normalise(alias)
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO aliases (alias, sku, usage_count, created_at)
                    VALUES (%s, %s, 1, NOW())
                    ON CONFLICT (alias) DO UPDATE
                        SET sku         = EXCLUDED.sku,
                            usage_count = aliases.usage_count + 1
                    """,
                    (norm, sku),
                )
            conn.commit()
    except Exception:
        logger.exception("Failed to record alias '%s' → '%s'", norm, sku)


def record_match_history(
    input_text: str,
    predicted_sku: Optional[str],
    final_sku: Optional[str],
    corrected: bool,
) -> None:
    """Append one row to the match_history audit table.

    Args:
        input_text:    The raw description that was matched.
        predicted_sku: The SKU the system predicted automatically.
        final_sku:     The SKU the user accepted or chose.
        corrected:     True when the user overrode the predicted SKU.
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO match_history
                        (input_text, predicted_sku, final_sku, corrected, timestamp)
                    VALUES (%s, %s, %s, %s, NOW())
                    """,
                    (input_text, predicted_sku, final_sku, corrected),
                )
            conn.commit()
    except Exception:
        logger.exception("Failed to record match history for: %.100s", input_text)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _alias_lookup(conn: psycopg.Connection, norm: str) -> list[dict]:
    """Return a single-item list if an alias match exists, else []."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sku FROM aliases WHERE alias = %s LIMIT 1",
            (norm,),
        )
        row = cur.fetchone()
        if not row:
            return []
        sku = row[0]

        # Fetch full SKU record so we can return a description
        cur.execute(
            "SELECT sku, description FROM skus WHERE sku = %s",
            (sku,),
        )
        sku_row = cur.fetchone()
        if sku_row:
            return [{"sku": sku_row[0], "description": sku_row[1], "confidence": 1.0}]
        # Alias points to a SKU that was deleted — return partial info
        return [{"sku": sku, "description": "", "confidence": 1.0}]


def _keyword_search(conn: psycopg.Connection, norm: str, top_k: int) -> list[dict]:
    """Token-based ILIKE search against skus table, scored by coverage."""
    tokens = [t for t in norm.split() if len(t) > 1]
    if not tokens:
        return []

    # Build: (keywords ILIKE %s OR normalized_name ILIKE %s OR description ILIKE %s)
    # repeated for each token, joined with OR.
    per_token = "(keywords ILIKE %s OR normalized_name ILIKE %s OR description ILIKE %s)"
    where = " OR ".join([per_token] * len(tokens))
    params: list = []
    for token in tokens:
        like = f"%{token}%"
        params.extend([like, like, like])
    params.append(top_k * 3)  # fetch extra rows so scoring can re-rank

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT sku, description, keywords, normalized_name "
            f"FROM skus WHERE {where} LIMIT %s",
            params,
        )
        rows = cur.fetchall()

    if not rows:
        return []

    results: list[dict] = []
    for sku, desc, keywords, normalized_name in rows:
        haystack = " ".join(filter(None, [desc, keywords, normalized_name])).lower()
        matched = sum(1 for t in tokens if t in haystack)
        confidence = round(min(matched / len(tokens), 0.99), 4)
        results.append({"sku": sku, "description": desc or "", "confidence": confidence})

    # Optional: boost with rapidfuzz if available
    try:
        from rapidfuzz import fuzz as _fuzz

        for item in results:
            haystack = item["description"].lower()
            ratio = _fuzz.token_set_ratio(norm, haystack) / 100.0
            # Blend token coverage (70%) with fuzzy ratio (30%)
            item["confidence"] = round(item["confidence"] * 0.7 + ratio * 0.3, 4)
    except ImportError:
        pass

    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results[:top_k]
