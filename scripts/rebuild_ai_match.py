"""Rebuild ai_match_text and color for all ERPItems and refresh vector indexes.

Usage (run from repo root inside Docker or with venv active):
    python scripts/rebuild_ai_match.py [--branch BRANCH_CODE] [--dry-run]

What it does:
    1. Re-derives ai_match_text for every ERPItem using the same dedup-token
       logic as the catalog importer (no API call required).
    2. Extracts color names from descriptions using the known-colors list.
    3. Clears any cached vector indexes so the next match request re-encodes
       the freshened text.
    4. Optionally limits work to a single branch (--branch).

Run this after:
    - Adding new synonym expansions to sku_pipeline._KNOWN_COLORS
    - Bulk-updating catalog descriptions outside the normal import flow
    - Any schema migration that changes searchable_text composition
"""

from __future__ import annotations

import argparse
import sys
import os

# ---------------------------------------------------------------------------
# Ensure the project root is on the path so app/ is importable
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import create_app, db
from app.models import ERPItem, Branch, BranchCatalogItem
from app.services import item_matcher
from app.services.sku_pipeline import _dedupe_tokens, _extract_color
from config import Config


def _rebuild_item(item: ERPItem) -> bool:
    """Regenerate ai_match_text and color for a single ERPItem.

    Returns True if any field changed.
    """
    new_color = _extract_color(
        f"{item.description or ''} {item.ext_description or ''} {item.minor_description or ''}"
    )
    new_ai = _dedupe_tokens([
        item.item_code or "",
        item.description or "",
        item.ext_description or "",
        item.major_description or "",
        item.minor_description or "",
        item.size or "",
        item.length or "",
        f"{item.length}ft" if item.length else "",
        f"{item.length} foot" if item.length else "",
        new_color,
        item.keywords or "",
    ])
    changed = False
    if item.color != new_color:
        item.color = new_color
        changed = True
    if item.ai_match_text != new_ai:
        item.ai_match_text = new_ai
        changed = True
    if changed:
        item.embedding = None  # force re-encode on next vector build
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", help="Only rebuild items for this branch code")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing to DB")
    args = parser.parse_args()

    app = create_app(Config)
    with app.app_context():
        if args.branch:
            branch = Branch.query.filter_by(code=args.branch).first()
            if not branch:
                print(f"ERROR: branch '{args.branch}' not found", file=sys.stderr)
                sys.exit(1)
            items = (
                ERPItem.query
                .join(BranchCatalogItem, BranchCatalogItem.erp_item_id == ERPItem.id)
                .filter(BranchCatalogItem.branch_id == branch.id)
                .all()
            )
            print(f"Rebuilding {len(items)} items for branch {args.branch}…")
        else:
            items = ERPItem.query.all()
            print(f"Rebuilding {len(items)} items across all branches…")

        changed = 0
        for i, item in enumerate(items, 1):
            if _rebuild_item(item):
                changed += 1
            if i % 500 == 0:
                print(f"  processed {i}/{len(items)} ({changed} changed)…")
                if not args.dry_run:
                    db.session.flush()

        print(f"Done — {changed}/{len(items)} items updated.")

        if args.dry_run:
            db.session.rollback()
            print("Dry-run: no changes written.")
        else:
            db.session.commit()
            # Clear all cached vector indexes so next request re-encodes fresh text
            item_matcher.clear_index()
            print("Vector index cache cleared — indexes will rebuild on next match request.")


if __name__ == "__main__":
    main()
