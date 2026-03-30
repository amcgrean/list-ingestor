"""Helpers for combining manual and inferred upload context."""

from __future__ import annotations

import json
import re
from typing import Any


# ---------------------------------------------------------------------------
# Project-type detection and context-aware keyword injection
# ---------------------------------------------------------------------------
# When the upload_context or document summary indicates a project type, inject
# species/treatment keywords so the matcher biases toward the right lumber.
# Example: "deck" → inject "treated exterior" so "joists" finds treated SYP
# instead of untreated SPF.  "framing" → inject "spf fir" to avoid treated.
#
# Keys are regex patterns matched against the combined context text (case-insensitive).
# Values are keyword strings appended to every item's enriched match text.
_PROJECT_TYPE_SIGNALS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(deck|decking|porch|pergola)\b', re.IGNORECASE), "treated exterior pressure treated"),
    (re.compile(r'\b(framing|interior|wall|floor)\b', re.IGNORECASE), "spf fir spruce pine fir"),
    (re.compile(r'\b(roof|roofing)\b', re.IGNORECASE), "lumber rafter treated"),
    (re.compile(r'\b(fence|fencing)\b', re.IGNORECASE), "cedar pressure treated post"),
    (re.compile(r'\b(foundation|sill|mudsill|crawl)\b', re.IGNORECASE), "pressure treated ground contact"),
]


def _detect_project_type_keywords(upload_context: str, document_context: dict | None) -> str:
    """Return extra keywords to inject based on detected project type, or empty string."""
    summary = (document_context or {}).get("summary", "") or ""
    combined = f"{upload_context} {summary}".strip()
    if not combined:
        return ""
    for pattern, keywords in _PROJECT_TYPE_SIGNALS:
        if pattern.search(combined):
            return keywords
    return ""


def compact_text(value: Any) -> str:
    text = str(value or "").strip()
    return " ".join(text.split())


def _as_clean_list(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]

    cleaned: list[str] = []
    for value in values:
        text = compact_text(value)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def normalize_document_context(context: dict[str, Any] | None) -> dict[str, Any]:
    context = context or {}
    return {
        "summary": compact_text(context.get("summary")),
        "customer_name": compact_text(context.get("customer_name")),
        "project_name": compact_text(context.get("project_name")),
        "global_material_context": _as_clean_list(context.get("global_material_context")),
        "job_notes": _as_clean_list(context.get("job_notes")),
        "warnings": _as_clean_list(context.get("warnings")),
    }


def merge_document_contexts(contexts: list[dict[str, Any]]) -> dict[str, Any]:
    merged = normalize_document_context({})
    for context in contexts:
        current = normalize_document_context(context)
        if current["summary"] and not merged["summary"]:
            merged["summary"] = current["summary"]
        if current["customer_name"] and not merged["customer_name"]:
            merged["customer_name"] = current["customer_name"]
        if current["project_name"] and not merged["project_name"]:
            merged["project_name"] = current["project_name"]
        for key in ("global_material_context", "job_notes", "warnings"):
            for value in current[key]:
                if value not in merged[key]:
                    merged[key].append(value)
    return merged


def context_to_json(context: dict[str, Any] | None) -> str | None:
    normalized = normalize_document_context(context)
    if not any(
        normalized[key]
        for key in ("summary", "customer_name", "project_name", "global_material_context", "job_notes", "warnings")
    ):
        return None
    return json.dumps(normalized, sort_keys=True)


def enrich_description_for_matching(
    description: str,
    upload_context: str = "",
    document_context: dict[str, Any] | None = None,
) -> str:
    base_description = compact_text(description)
    normalized = normalize_document_context(document_context)

    match_clauses: list[str] = []
    if normalized["global_material_context"]:
        match_clauses.append(", ".join(normalized["global_material_context"]))
    if normalized["job_notes"]:
        match_clauses.append(", ".join(normalized["job_notes"]))
    if upload_context:
        match_clauses.append(compact_text(upload_context))

    # Inject implicit species/treatment keywords when the project type is detectable.
    # E.g. "deck" context → "treated exterior pressure treated" so "joists" finds
    # treated SYP rather than SPF studs.
    project_keywords = _detect_project_type_keywords(upload_context, document_context)
    if project_keywords:
        match_clauses.append(project_keywords)

    if not match_clauses:
        return base_description

    return f"{base_description} {' '.join(match_clauses)}".strip()
