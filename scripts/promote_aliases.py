"""
Auto-promote frequently-corrected descriptions to permanent ItemAlias entries.

HOW IT WORKS
------------
When a user corrects a match on the review page, a MatchFeedbackEvent is recorded
with the normalized description and the SKU they chose.  When the same
(normalized_description, final_sku) pair has been confirmed PROMOTE_THRESHOLD
or more times, this script creates a permanent ItemAlias — so the next time
that exact description appears it gets a 1.0 confidence score without needing
any vector/fuzzy matching.

This is the primary "self-learning" mechanism: the more corrections sales reps
submit, the smarter the system gets, without any AI retraining.

RUN MANUALLY
------------
    cat scripts/promote_aliases.py | docker compose exec -T web python -

RUN VIA FLASK CLI (after deploy)
---------------------------------
    docker compose exec web flask promote-aliases

CRON (daily at 2am via deploy/cron/setup_cron.sh)
---------------------------------------------------
    0 2 * * * cd /home/amcgrean/services/list-ingestor && \
      cat scripts/promote_aliases.py | docker compose exec -T web python - >> logs/promote_aliases.log 2>&1
"""

from __future__ import annotations

PROMOTE_THRESHOLD = 3  # confirmations needed before alias is created

from app import create_app, db
from app.models import ERPItem, ItemAlias, MatchFeedbackEvent
from sqlalchemy import func
from datetime import datetime

app = create_app()
with app.app_context():
    # Find (normalized_description, final_sku) pairs that meet the threshold
    candidates = (
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

    created = 0
    skipped_exists = 0
    skipped_no_sku = 0

    for norm_desc, sku, cnt in candidates:
        if not norm_desc or not sku:
            continue

        # Skip if alias already exists for this description
        existing = ItemAlias.query.filter_by(alias=norm_desc).first()
        if existing:
            # Update usage count if the SKU matches
            if existing.sku == sku:
                existing.usage_count = cnt
            skipped_exists += 1
            continue

        # Verify SKU still exists in catalog
        item = ERPItem.query.filter_by(item_code=sku).first()
        if not item:
            print("SKIP (no catalog item): %r -> %s" % (norm_desc, sku))
            skipped_no_sku += 1
            continue

        db.session.add(ItemAlias(alias=norm_desc, sku=sku, usage_count=cnt))
        print("[%s] NEW ALIAS: %r -> %s | %s [%d confirmations]" % (
            datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            norm_desc,
            sku,
            item.description[:50],
            cnt,
        ))
        created += 1

    db.session.commit()
    print("\n--- promote_aliases complete ---")
    print("New aliases created : %d" % created)
    print("Already existed     : %d" % skipped_exists)
    print("SKU not in catalog  : %d" % skipped_no_sku)
    print("Threshold           : %d confirmations" % PROMOTE_THRESHOLD)
