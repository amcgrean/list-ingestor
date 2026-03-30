"""
Daily operations digest for the material list ingestor.

Prints a summary covering the last N days:
  - Sessions processed and their match quality
  - Low-confidence items (catalog gaps)
  - Corrections made (feedback events)
  - Aliases ready to promote
  - Cumulative alias count

Run:
    cat scripts/daily_digest.py | docker compose exec -T web python -

Or via Flask CLI:
    docker compose exec web flask daily-digest

For automated email/slack digest, pipe the output to your notification script.
"""

from __future__ import annotations

DAYS = 7  # look-back window for the digest

from app import create_app, db
from app.models import (
    ERPItem, ExtractedItem, ItemAlias, MatchFeedbackEvent, ProcessingSession
)
from sqlalchemy import func
from datetime import datetime, timedelta, timezone

app = create_app()
with app.app_context():
    now = datetime.now(tz=timezone.utc)
    since = now - timedelta(days=DAYS)

    print("=" * 60)
    print("MATERIAL LIST INGESTOR — DAILY DIGEST")
    print("Generated : %s UTC" % now.strftime("%Y-%m-%d %H:%M"))
    print("Window    : last %d days" % DAYS)
    print("=" * 60)

    # ── Sessions ──────────────────────────────────────────────────────
    sessions = ProcessingSession.query.filter(
        ProcessingSession.created_at >= since
    ).all()
    statuses = {}
    for s in sessions:
        statuses[s.status] = statuses.get(s.status, 0) + 1

    print("\n[SESSIONS] last %d days: %d total" % (DAYS, len(sessions)))
    for status, count in sorted(statuses.items()):
        print("  %-20s %d" % (status, count))

    # ── Match quality ─────────────────────────────────────────────────
    recent_items = (
        ExtractedItem.query
        .join(ProcessingSession)
        .filter(ProcessingSession.created_at >= since)
        .all()
    )
    if recent_items:
        total_items = len(recent_items)
        high_conf   = sum(1 for i in recent_items if i.confidence_score >= 0.80)
        mid_conf    = sum(1 for i in recent_items if 0.60 <= i.confidence_score < 0.80)
        low_conf    = sum(1 for i in recent_items if i.confidence_score < 0.60 and not i.is_skipped)
        skipped     = sum(1 for i in recent_items if i.is_skipped)
        avg_conf    = sum(i.confidence_score for i in recent_items) / total_items

        print("\n[MATCH QUALITY] %d items across %d sessions" % (total_items, len(sessions)))
        print("  High conf (>=0.80): %d  (%.0f%%)" % (high_conf, 100 * high_conf / total_items))
        print("  Mid  conf (0.60-0.80): %d  (%.0f%%)" % (mid_conf, 100 * mid_conf / total_items))
        print("  Low  conf (<0.60):  %d  (%.0f%%)  <-- needs attention" % (low_conf, 100 * low_conf / total_items))
        print("  Skipped:            %d" % skipped)
        print("  Avg confidence:     %.3f" % avg_conf)
    else:
        print("\n[MATCH QUALITY] No items in window.")

    # ── Feedback / corrections ────────────────────────────────────────
    feedback = MatchFeedbackEvent.query.filter(
        MatchFeedbackEvent.created_at >= since
    ).all()
    corrections   = sum(1 for f in feedback if f.was_corrected)
    skipped_fb    = sum(1 for f in feedback if f.was_skipped)
    confirmed_ok  = len(feedback) - corrections - skipped_fb

    print("\n[FEEDBACK] last %d days: %d events" % (DAYS, len(feedback)))
    print("  Confirmed correct  : %d" % confirmed_ok)
    print("  Corrected          : %d" % corrections)
    print("  Skipped (no match) : %d" % skipped_fb)

    # ── Catalog gaps — descriptions with consistent low confidence ───
    # Find normalized descriptions that appear 3+ times and always < 0.70
    gap_candidates = (
        MatchFeedbackEvent.query.with_entities(
            MatchFeedbackEvent.normalized_description,
            func.count(MatchFeedbackEvent.id).label("cnt"),
            func.avg(MatchFeedbackEvent.confidence_score).label("avg_conf"),
        )
        .filter(MatchFeedbackEvent.was_skipped.is_(False))
        .group_by(MatchFeedbackEvent.normalized_description)
        .having(
            func.count(MatchFeedbackEvent.id) >= 3,
            func.avg(MatchFeedbackEvent.confidence_score) < 0.65,
        )
        .order_by(func.avg(MatchFeedbackEvent.confidence_score))
        .limit(10)
        .all()
    )
    print("\n[CATALOG GAPS] descriptions seen 3+ times, avg confidence < 0.65:")
    if gap_candidates:
        for norm_desc, cnt, avg_conf in gap_candidates:
            # Check if an alias already covers this
            has_alias = ItemAlias.query.filter_by(alias=norm_desc).first() is not None
            flag = " [has alias]" if has_alias else " <-- ADD TO CATALOG?"
            print("  %.3f  (%dx) %r%s" % (avg_conf, cnt, (norm_desc or '')[:60], flag))
    else:
        print("  None found — catalog coverage looks good!")

    # ── Aliases ready to promote ──────────────────────────────────────
    PROMOTE_THRESHOLD = 3
    promotable = (
        MatchFeedbackEvent.query.with_entities(
            MatchFeedbackEvent.normalized_description,
            MatchFeedbackEvent.final_sku,
            func.count(MatchFeedbackEvent.id).label("cnt"),
        )
        .filter(
            MatchFeedbackEvent.final_sku.isnot(None),
            MatchFeedbackEvent.was_skipped.is_(False),
        )
        .group_by(
            MatchFeedbackEvent.normalized_description,
            MatchFeedbackEvent.final_sku,
        )
        .having(func.count(MatchFeedbackEvent.id) >= PROMOTE_THRESHOLD)
        .all()
    )
    new_promotable = [
        (d, s, c) for d, s, c in promotable
        if not ItemAlias.query.filter_by(alias=d).first()
    ]
    print("\n[ALIAS QUEUE] descriptions ready to promote (run promote_aliases.py):")
    print("  Total promotable  : %d" % len(promotable))
    print("  Not yet aliased   : %d" % len(new_promotable))
    if new_promotable:
        for norm_desc, sku, cnt in new_promotable[:5]:
            print("    [%dx] %r -> %s" % (cnt, (norm_desc or '')[:50], sku))
        if len(new_promotable) > 5:
            print("    ... and %d more" % (len(new_promotable) - 5))

    # ── Cumulative stats ──────────────────────────────────────────────
    total_aliases  = ItemAlias.query.count()
    total_sessions = ProcessingSession.query.count()
    total_feedback = MatchFeedbackEvent.query.count()
    total_catalog  = ERPItem.query.count()

    print("\n[CUMULATIVE TOTALS]")
    print("  Catalog items     : %d" % total_catalog)
    print("  Total aliases     : %d" % total_aliases)
    print("  Total sessions    : %d" % total_sessions)
    print("  Total feedback    : %d" % total_feedback)

    print("\n" + "=" * 60)
    print("To promote aliases:  cat scripts/promote_aliases.py | docker compose exec -T web python -")
    print("=" * 60)
