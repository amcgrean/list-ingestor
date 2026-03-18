"""Customer/job context sync and lookup helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, text

from app import db
from app.models import CustomerJobContext
from app.services.upload_context import compact_text


@dataclass
class MatchedCustomerJobContext:
    context: CustomerJobContext
    score: int
    matched_terms: list[str]

    def to_session_payload(self) -> dict[str, Any]:
        return {
            "source_system": self.context.source_system,
            "external_id": self.context.external_id,
            "branch_code": self.context.branch_code,
            "customer_name": self.context.customer_name,
            "project_name": self.context.project_name,
            "material_context": compact_text(self.context.material_context),
            "job_notes": compact_text(self.context.job_notes),
            "aliases": self.context.aliases(),
            "metadata": self.context.metadata_dict(),
            "score": self.score,
            "matched_terms": self.matched_terms,
        }


def sync_contexts_from_cloud(
    database_url: str,
    query: str,
    source_system: str = "cloud",
) -> dict[str, int]:
    """Sync external customer/job context rows into the local app DB."""
    engine = create_engine(database_url)
    stats = {"inserted": 0, "updated": 0, "deactivated": 0, "seen": 0}
    seen_keys: set[tuple[str, str]] = set()

    with engine.connect() as conn:
        rows = conn.execute(text(query)).mappings().all()

    for row in rows:
        payload = _normalize_sync_row(row, source_system=source_system)
        if not payload:
            continue

        stats["seen"] += 1
        key = (payload["source_system"], payload["external_id"])
        seen_keys.add(key)

        existing = CustomerJobContext.query.filter_by(
            source_system=payload["source_system"],
            external_id=payload["external_id"],
        ).first()

        if existing is None:
            db.session.add(CustomerJobContext(**payload))
            stats["inserted"] += 1
            continue

        changed = False
        for field, value in payload.items():
            if getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True
        if changed:
            stats["updated"] += 1

    existing_contexts = CustomerJobContext.query.filter_by(source_system=source_system).all()
    for existing in existing_contexts:
        if (existing.source_system, existing.external_id) in seen_keys:
            continue
        if existing.is_active:
            existing.is_active = False
            stats["deactivated"] += 1

    db.session.commit()
    return stats


def match_customer_job_context(
    customer_name: str = "",
    project_name: str = "",
    upload_context: str = "",
    branch_code: str = "",
) -> MatchedCustomerJobContext | None:
    """Find the best synced customer/job context for the current upload."""
    search_terms = _collect_search_terms(customer_name, project_name, upload_context)
    if not search_terms:
        return None

    query = CustomerJobContext.query.filter_by(is_active=True)
    if branch_code:
        query = query.filter(
            db.or_(
                CustomerJobContext.branch_code.is_(None),
                CustomerJobContext.branch_code == "",
                CustomerJobContext.branch_code == branch_code,
            )
        )

    best_match: MatchedCustomerJobContext | None = None
    for context in query.all():
        score, matched_terms = _score_context_match(context, search_terms)
        if score <= 0:
            continue
        candidate = MatchedCustomerJobContext(context=context, score=score, matched_terms=matched_terms)
        if best_match is None or candidate.score > best_match.score:
            best_match = candidate
    return best_match


def _normalize_sync_row(row: Any, source_system: str) -> dict[str, Any] | None:
    external_id = compact_text(row.get("external_id") or row.get("id"))
    if not external_id:
        return None

    aliases = _to_list(row.get("aliases"))
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {"raw": metadata}

    return {
        "source_system": compact_text(row.get("source_system") or source_system) or source_system,
        "external_id": external_id,
        "branch_code": compact_text(row.get("branch_code")),
        "customer_name": compact_text(row.get("customer_name")),
        "project_name": compact_text(row.get("project_name")),
        "aliases_json": json.dumps(aliases) if aliases else None,
        "material_context": compact_text(row.get("material_context")),
        "job_notes": compact_text(row.get("job_notes")),
        "metadata_json": json.dumps(metadata, sort_keys=True) if metadata else None,
        "is_active": _to_bool(row.get("is_active"), default=True),
    }


def _collect_search_terms(customer_name: str, project_name: str, upload_context: str) -> list[str]:
    candidates = [
        customer_name,
        project_name,
        upload_context,
    ]
    search_terms: list[str] = []
    for candidate in candidates:
        text_value = compact_text(candidate).lower()
        if not text_value:
            continue
        if text_value not in search_terms:
            search_terms.append(text_value)
    return search_terms


def _score_context_match(context: CustomerJobContext, search_terms: list[str]) -> tuple[int, list[str]]:
    terms_to_check = [
        compact_text(context.customer_name).lower(),
        compact_text(context.project_name).lower(),
        compact_text(context.material_context).lower(),
        compact_text(context.job_notes).lower(),
    ]
    terms_to_check.extend(alias.lower() for alias in context.aliases())
    terms_to_check = [term for term in terms_to_check if term]

    score = 0
    matched_terms: list[str] = []
    for search_term in search_terms:
        for candidate in terms_to_check:
            if search_term in candidate or candidate in search_term:
                score += max(1, min(len(candidate), len(search_term)))
                matched_terms.append(candidate)
    deduped = list(dict.fromkeys(matched_terms))
    return score, deduped


def _to_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                return [compact_text(item) for item in parsed if compact_text(item)]
        return [compact_text(item) for item in stripped.split("|") if compact_text(item)]
    if isinstance(value, list):
        return [compact_text(item) for item in value if compact_text(item)]
    return [compact_text(value)] if compact_text(value) else []


def _to_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
