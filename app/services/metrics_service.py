"""
Metrics Service
---------------
Query helpers that aggregate ingester performance and accuracy data.
Called by /api/metrics and /metrics routes so the query logic stays
out of routes.py.

All functions return plain Python dicts/lists — no ORM objects leak
out of this module — so results are safe to JSON-serialize directly.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, case

from app import db
from app.models import IngesterMetrics, MatchFeedbackEvent, ProcessingSession

logger = logging.getLogger(__name__)


def save_session_metrics(
    *,
    session_id: int,
    ai_provider: str,
    ocr_ms: int | None,
    ai_parse_ms: int | None,
    match_ms: int | None,
    total_ms: int | None,
    items_extracted: int,
    items_matched: int,
    items_below_threshold: int,
    avg_confidence: float | None,
    avg_fuzzy_score: float | None,
    avg_vector_score: float | None,
    ocr_error: bool = False,
    ai_parse_error: bool = False,
    match_error: bool = False,
) -> None:
    """Upsert an IngesterMetrics row for the given session."""
    existing = IngesterMetrics.query.filter_by(session_id=session_id).first()
    if existing:
        row = existing
    else:
        row = IngesterMetrics(session_id=session_id)
        db.session.add(row)

    row.ai_provider = ai_provider
    row.ocr_duration_ms = ocr_ms
    row.ai_parse_duration_ms = ai_parse_ms
    row.match_duration_ms = match_ms
    row.total_duration_ms = total_ms
    row.items_extracted = items_extracted
    row.items_matched = items_matched
    row.items_below_threshold = items_below_threshold
    row.avg_confidence = avg_confidence
    row.avg_fuzzy_score = avg_fuzzy_score
    row.avg_vector_score = avg_vector_score
    row.ocr_error = ocr_error
    row.ai_parse_error = ai_parse_error
    row.match_error = match_error

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to save IngesterMetrics for session %d", session_id)


def get_summary(days: int = 30) -> dict:
    """Aggregate stats for the last *days* days.

    Returns a dict suitable for /api/metrics and the dashboard template.
    Accuracy (correction/skip rates) comes from MatchFeedbackEvent because
    those are only known after user review completes.
    """
    since = datetime.utcnow() - timedelta(days=days)

    # --- Pipeline timing / volume from IngesterMetrics ---
    m = db.session.query(
        func.count(IngesterMetrics.id).label("total_sessions"),
        func.sum(case((IngesterMetrics.ocr_error.is_(True), 1), else_=0)).label("ocr_errors"),
        func.sum(case((IngesterMetrics.ai_parse_error.is_(True), 1), else_=0)).label("ai_parse_errors"),
        func.sum(case((IngesterMetrics.match_error.is_(True), 1), else_=0)).label("match_errors"),
        func.avg(IngesterMetrics.ocr_duration_ms).label("avg_ocr_ms"),
        func.avg(IngesterMetrics.ai_parse_duration_ms).label("avg_ai_ms"),
        func.avg(IngesterMetrics.match_duration_ms).label("avg_match_ms"),
        func.avg(IngesterMetrics.total_duration_ms).label("avg_total_ms"),
        func.sum(IngesterMetrics.items_extracted).label("total_items"),
        func.avg(IngesterMetrics.items_extracted).label("avg_items_per_session"),
        func.avg(IngesterMetrics.avg_confidence).label("avg_confidence"),
        func.avg(IngesterMetrics.avg_fuzzy_score).label("avg_fuzzy"),
        func.avg(IngesterMetrics.avg_vector_score).label("avg_vector"),
        func.sum(IngesterMetrics.items_below_threshold).label("total_below_threshold"),
        func.sum(IngesterMetrics.items_matched).label("total_matched"),
    ).filter(IngesterMetrics.created_at >= since).one()

    # --- Accuracy from MatchFeedbackEvent ---
    fb = db.session.query(
        func.count(MatchFeedbackEvent.id).label("total_feedback"),
        func.sum(case((MatchFeedbackEvent.was_corrected.is_(True), 1), else_=0)).label("corrected"),
        func.sum(case((MatchFeedbackEvent.was_skipped.is_(True), 1), else_=0)).label("skipped"),
    ).filter(MatchFeedbackEvent.created_at >= since).one()

    total_feedback = int(fb.total_feedback or 0)
    corrected = int(fb.corrected or 0)
    skipped = int(fb.skipped or 0)

    correction_rate = round(corrected / total_feedback, 4) if total_feedback else None
    skip_rate = round(skipped / total_feedback, 4) if total_feedback else None
    accuracy_rate = round(1.0 - correction_rate, 4) if correction_rate is not None else None

    total_matched = int(m.total_matched or 0)
    total_below = int(m.total_below_threshold or 0)
    below_threshold_pct = round(total_below / total_matched * 100, 1) if total_matched else None

    return {
        "period_days": days,
        "total_sessions": int(m.total_sessions or 0),
        "total_items_processed": int(m.total_items or 0),
        "avg_items_per_session": round(float(m.avg_items_per_session), 1) if m.avg_items_per_session else None,
        "pipeline_errors": {
            "ocr": int(m.ocr_errors or 0),
            "ai_parse": int(m.ai_parse_errors or 0),
            "match": int(m.match_errors or 0),
        },
        "latency_ms": {
            "ocr_avg": _ms(m.avg_ocr_ms),
            "ai_parse_avg": _ms(m.avg_ai_ms),
            "match_avg": _ms(m.avg_match_ms),
            "total_avg": _ms(m.avg_total_ms),
        },
        "accuracy": {
            "total_reviewed_items": total_feedback,
            "corrected": corrected,
            "skipped": skipped,
            "correction_rate": correction_rate,
            "skip_rate": skip_rate,
            "accuracy_rate": accuracy_rate,
        },
        "confidence": {
            "avg": round(float(m.avg_confidence), 4) if m.avg_confidence else None,
            "avg_fuzzy": round(float(m.avg_fuzzy), 4) if m.avg_fuzzy else None,
            "avg_vector": round(float(m.avg_vector), 4) if m.avg_vector else None,
            "below_threshold_pct": below_threshold_pct,
        },
    }


def get_recent_sessions(limit: int = 25) -> list[dict]:
    """Return the most recent sessions with their metrics and post-review accuracy."""
    rows = (
        db.session.query(IngesterMetrics, ProcessingSession)
        .join(ProcessingSession, IngesterMetrics.session_id == ProcessingSession.id)
        .order_by(IngesterMetrics.created_at.desc())
        .limit(limit)
        .all()
    )

    # Fetch per-session feedback counts in one query
    session_ids = [m.session_id for m, _ in rows]
    feedback_map: dict[int, dict] = {}
    if session_ids:
        fb_rows = (
            db.session.query(
                MatchFeedbackEvent.session_id,
                func.count(MatchFeedbackEvent.id).label("total"),
                func.sum(case((MatchFeedbackEvent.was_corrected.is_(True), 1), else_=0)).label("corrected"),
                func.sum(case((MatchFeedbackEvent.was_skipped.is_(True), 1), else_=0)).label("skipped"),
            )
            .filter(MatchFeedbackEvent.session_id.in_(session_ids))
            .group_by(MatchFeedbackEvent.session_id)
            .all()
        )
        for fb in fb_rows:
            total = int(fb.total or 0)
            corr = int(fb.corrected or 0)
            feedback_map[fb.session_id] = {
                "total": total,
                "corrected": corr,
                "skipped": int(fb.skipped or 0),
                "correction_rate": round(corr / total, 3) if total else None,
            }

    result = []
    for metrics, session in rows:
        fb = feedback_map.get(metrics.session_id, {})
        result.append({
            "session_id": metrics.session_id,
            "filename": session.filename,
            "status": session.status,
            "ai_provider": metrics.ai_provider,
            "ocr_duration_ms": metrics.ocr_duration_ms,
            "ai_parse_duration_ms": metrics.ai_parse_duration_ms,
            "match_duration_ms": metrics.match_duration_ms,
            "total_duration_ms": metrics.total_duration_ms,
            "items_extracted": metrics.items_extracted,
            "items_matched": metrics.items_matched,
            "items_below_threshold": metrics.items_below_threshold,
            "avg_confidence": round(metrics.avg_confidence, 3) if metrics.avg_confidence is not None else None,
            "ocr_error": metrics.ocr_error,
            "ai_parse_error": metrics.ai_parse_error,
            "match_error": metrics.match_error,
            "created_at": metrics.created_at.isoformat(),
            "feedback": fb,
        })
    return result


def get_confidence_distribution(days: int = 30) -> dict:
    """Return counts of MatchFeedbackEvent rows bucketed by confidence score.

    Single aggregation query — no Python-side loop of COUNT queries.
    """
    since = datetime.utcnow() - timedelta(days=days)
    cs = MatchFeedbackEvent.confidence_score
    row = db.session.query(
        func.sum(case((cs < 0.3, 1), else_=0)).label("b0"),
        func.sum(case((db.and_(cs >= 0.3, cs < 0.5), 1), else_=0)).label("b1"),
        func.sum(case((db.and_(cs >= 0.5, cs < 0.7), 1), else_=0)).label("b2"),
        func.sum(case((db.and_(cs >= 0.7, cs < 0.9), 1), else_=0)).label("b3"),
        func.sum(case((cs >= 0.9, 1), else_=0)).label("b4"),
    ).filter(MatchFeedbackEvent.created_at >= since).one()
    return {
        "0.0–0.3": int(row.b0 or 0),
        "0.3–0.5": int(row.b1 or 0),
        "0.5–0.7": int(row.b2 or 0),
        "0.7–0.9": int(row.b3 or 0),
        "0.9–1.0": int(row.b4 or 0),
    }


def get_provider_stats(days: int = 30) -> dict:
    """Per-AI-provider breakdown of latency, volume, and accuracy.

    Single query using an outer join — no N+1 per-provider queries and no
    intermediate Python-side list of all session IDs.
    """
    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.session.query(
            IngesterMetrics.ai_provider,
            func.count(IngesterMetrics.id).label("sessions"),
            func.avg(IngesterMetrics.ai_parse_duration_ms).label("avg_ai_ms"),
            func.avg(IngesterMetrics.total_duration_ms).label("avg_total_ms"),
            func.avg(IngesterMetrics.avg_confidence).label("avg_confidence"),
            func.sum(IngesterMetrics.items_extracted).label("total_items"),
            func.count(MatchFeedbackEvent.id).label("fb_total"),
            func.sum(case((MatchFeedbackEvent.was_corrected.is_(True), 1), else_=0)).label("fb_corrected"),
        )
        .outerjoin(
            MatchFeedbackEvent,
            db.and_(
                MatchFeedbackEvent.session_id == IngesterMetrics.session_id,
                MatchFeedbackEvent.created_at >= since,
            ),
        )
        .filter(IngesterMetrics.created_at >= since)
        .group_by(IngesterMetrics.ai_provider)
        .all()
    )
    result = {}
    for row in rows:
        prov = row.ai_provider or "unknown"
        fb_total = int(row.fb_total or 0)
        fb_corr = int(row.fb_corrected or 0)
        result[prov] = {
            "sessions": int(row.sessions),
            "total_items": int(row.total_items or 0),
            "avg_ai_parse_ms": _ms(row.avg_ai_ms),
            "avg_total_ms": _ms(row.avg_total_ms),
            "avg_confidence": round(float(row.avg_confidence), 3) if row.avg_confidence else None,
            "correction_rate": round(fb_corr / fb_total, 3) if fb_total else None,
        }
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ms(val) -> int | None:
    return int(round(float(val))) if val is not None else None
