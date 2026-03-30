"""
Catalog enrichment: inject structural member usage keywords into dimensional lumber items.

Attacks the vocabulary gap from the catalog side — contractors say "joists", "posts",
"studs", but catalog descriptions say "2x10 #1 SYP Treated" with no member terms.

Run on the Pi:
    cat scripts/enrich_lumber_keywords.py | docker compose exec -T web python -

Idempotent: skips items that already have the keyword so re-runs are safe.
Prints a summary of changes made.
"""

from __future__ import annotations

from app import create_app, db
from app.models import ERPItem


def _add_keywords(item: ERPItem, additions: list[str]) -> bool:
    """Append missing keywords to item.keywords.  Returns True if anything changed."""
    existing = (item.keywords or "").lower()
    to_add = [kw for kw in additions if kw not in existing]
    if not to_add:
        return False
    item.keywords = ((item.keywords or "") + " " + " ".join(to_add)).strip()
    return True


app = create_app()
with app.app_context():
    changed = 0

    # ── 2x8 / 2x10 / 2x12 treated → joist rafter header (beam for 2x12) ────
    wide_treated = ERPItem.query.filter(
        ERPItem.description.ilike("%treated%"),
        db.or_(
            ERPItem.description.ilike("%2x8%"),
            ERPItem.description.ilike("%2x10%"),
            ERPItem.description.ilike("%2x12%"),
        ),
    ).all()
    for item in wide_treated:
        additions = ["joist", "rafter", "header"]
        if "2x12" in item.description.lower():
            additions.append("beam")
        if _add_keywords(item, additions):
            changed += 1

    # ── 4x4 / 6x6 treated → post column ────────────────────────────────────
    post_treated = ERPItem.query.filter(
        ERPItem.description.ilike("%treated%"),
        db.or_(
            ERPItem.description.ilike("%4x4%"),
            ERPItem.description.ilike("%6x6%"),
        ),
    ).all()
    for item in post_treated:
        if _add_keywords(item, ["post", "column"]):
            changed += 1

    # ── 2x4 / 2x6 fir / SPF → stud framing ─────────────────────────────────
    stud_lumber = ERPItem.query.filter(
        db.or_(
            ERPItem.description.ilike("%spf%"),
            ERPItem.description.ilike("% fir%"),
            ERPItem.description.ilike("%spruce%"),
            ERPItem.description.ilike("%hem-fir%"),
            ERPItem.description.ilike("%hemfir%"),
        ),
        db.or_(
            ERPItem.description.ilike("%2x4%"),
            ERPItem.description.ilike("%2x6%"),
        ),
    ).all()
    for item in stud_lumber:
        if _add_keywords(item, ["stud", "framing"]):
            changed += 1

    # ── 2x8 / 2x10 / 2x12 fir / SPF → joist rafter header (untreated) ─────
    wide_fir = ERPItem.query.filter(
        db.or_(
            ERPItem.description.ilike("%spf%"),
            ERPItem.description.ilike("% fir%"),
            ERPItem.description.ilike("%spruce%"),
        ),
        db.or_(
            ERPItem.description.ilike("%2x8%"),
            ERPItem.description.ilike("%2x10%"),
            ERPItem.description.ilike("%2x12%"),
        ),
    ).all()
    for item in wide_fir:
        additions = ["joist", "rafter", "header"]
        if "2x12" in item.description.lower():
            additions.append("beam")
        if _add_keywords(item, additions):
            changed += 1

    # ── 4x4 / 4x6 / 6x6 fir/SPF → post column (untreated) ─────────────────
    post_fir = ERPItem.query.filter(
        db.or_(
            ERPItem.description.ilike("%spf%"),
            ERPItem.description.ilike("% fir%"),
            ERPItem.description.ilike("%spruce%"),
        ),
        db.or_(
            ERPItem.description.ilike("%4x4%"),
            ERPItem.description.ilike("%4x6%"),
            ERPItem.description.ilike("%6x6%"),
        ),
    ).all()
    for item in post_fir:
        if _add_keywords(item, ["post", "column"]):
            changed += 1

    db.session.commit()
    print(f"Updated {changed} catalog items with structural usage keywords.")
    print("Restart the web service to rebuild the vector index with the new keywords:")
    print("  docker compose restart web")
