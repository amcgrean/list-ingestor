"""Export a Stage A ground-truth corpus for on-device OCR benchmarking.

Usage (run from repo root with the venv active, or inside Docker):
    python scripts/export_vision_benchmark.py --out data/vision_benchmark
    python scripts/export_vision_benchmark.py --out data/vision_benchmark --limit 30
    python scripts/export_vision_benchmark.py --out data/vision_benchmark --session-ids 25,31,44
    python scripts/export_vision_benchmark.py --out data/vision_benchmark --require-images

What it does:
    For each completed image/PDF session it writes
    ``<out>/session_<id>/expected.json`` describing what the cloud Stage A pass
    produced, and copies the archived source image alongside it when one exists.

Ground truth is taken from the best available source, in order:
    1. ``data/parse_debug/session_<id>/stage_a_raw_extract.json`` — the actual
       Stage A output, written when PARSE_DEBUG_SAVE_JSON=true.  Preferred:
       it includes header lines and section_type.
    2. The ``extracted_items`` rows for the session.  A lossy proxy — Stage A
       header lines are not persisted as items, so header_line_recall cannot be
       measured from this source.  ``expected.json`` records which was used.

Source images come from ``PARSE_ARCHIVE_DIR`` (see PARSE_ARCHIVE_UPLOADS).
Sessions processed before archiving was enabled will export their labels with
no image; use --require-images to skip those.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the project root is on the path so app/ is importable
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import create_app  # noqa: E402
from app.models import ExtractedItem, ProcessingSession  # noqa: E402

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".pdf", ".heic", ".heif"}


def _stage_a_debug_path(session_id: int) -> Path:
    return Path(_ROOT) / "data" / "parse_debug" / f"session_{session_id}" / "stage_a_raw_extract.json"


def _lines_from_stage_a_debug(session_id: int) -> list[dict] | None:
    path = _stage_a_debug_path(session_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, list):
        return None
    return [
        {
            "line_id": row.get("line_id", ""),
            "raw_text": row.get("raw_text", ""),
            "section_header": row.get("section_header", ""),
            "section_type": row.get("section_type", "unknown"),
            "quantity": row.get("quantity"),
        }
        for row in payload
        if isinstance(row, dict)
    ]


def _lines_from_extracted_items(session: ProcessingSession) -> list[dict]:
    items = (
        ExtractedItem.query.filter_by(session_id=session.id)
        .order_by(ExtractedItem.id.asc())
        .all()
    )
    lines = []
    for index, item in enumerate(items, start=1):
        lines.append(
            {
                "line_id": item.parse_line_id or f"L{index}",
                "raw_text": item.raw_description or "",
                "section_header": item.section_header or "",
                # extracted_items only ever holds material rows; Stage A header
                # lines are consumed upstream and never persisted here.
                "section_type": "item",
                "quantity": item.quantity,
            }
        )
    return lines


def _copy_archived_images(session_id: int, archive_dir: Path, destination: Path) -> list[str]:
    source_dir = archive_dir / f"session_{session_id}"
    if not source_dir.is_dir():
        return []
    copied = []
    for entry in sorted(source_dir.iterdir()):
        if entry.is_file() and entry.suffix.lower() in _IMAGE_SUFFIXES:
            shutil.copy2(entry, destination / entry.name)
            copied.append(entry.name)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="data/vision_benchmark", help="Output corpus directory")
    parser.add_argument("--limit", type=int, default=None, help="Export at most N sessions (newest first)")
    parser.add_argument("--session-ids", default="", help="Comma-separated session IDs to export")
    parser.add_argument(
        "--require-images",
        action="store_true",
        help="Skip sessions with no archived source image (they cannot be re-run on-device)",
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        archive_dir = Path(app.config.get("PARSE_ARCHIVE_DIR", ""))
        out_root = Path(args.out)
        out_root.mkdir(parents=True, exist_ok=True)

        query = ProcessingSession.query.filter(ProcessingSession.status == "completed")
        if args.session_ids.strip():
            wanted = [int(value) for value in args.session_ids.split(",") if value.strip()]
            query = query.filter(ProcessingSession.id.in_(wanted))
        else:
            # CSV uploads bypass the multimodal pass entirely — nothing to compare.
            query = query.filter(ProcessingSession.file_type != "csv")

        query = query.order_by(ProcessingSession.created_at.desc())
        if args.limit:
            query = query.limit(args.limit)

        exported = 0
        skipped_no_lines = 0
        skipped_no_images = 0
        with_images = 0
        from_debug = 0

        for session in query.all():
            debug_lines = _lines_from_stage_a_debug(session.id)
            if debug_lines is not None:
                lines, source = debug_lines, "stage_a_debug"
            else:
                lines, source = _lines_from_extracted_items(session), "extracted_items"

            if not lines:
                skipped_no_lines += 1
                continue

            session_dir = out_root / f"session_{session.id}"
            session_dir.mkdir(parents=True, exist_ok=True)
            images = _copy_archived_images(session.id, archive_dir, session_dir)

            if args.require_images and not images:
                shutil.rmtree(session_dir, ignore_errors=True)
                skipped_no_images += 1
                continue

            payload = {
                "session_id": session.id,
                "source": source,
                "filename": session.filename,
                "file_type": session.file_type,
                "branch_code": session.branch.code if session.branch else None,
                "upload_context": session.upload_context or "",
                "created_at": session.created_at.isoformat() if session.created_at else None,
                "images": images,
                "document_context": _safe_json(session.extracted_context_json),
                "lines": lines,
            }
            (session_dir / "expected.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            exported += 1
            if images:
                with_images += 1
            if source == "stage_a_debug":
                from_debug += 1

        print(f"Exported {exported} session(s) to {out_root}")
        print(f"  with source images : {with_images}")
        print(f"  from Stage A debug : {from_debug}  (rest reconstructed from extracted_items)")
        if skipped_no_lines:
            print(f"  skipped, no lines  : {skipped_no_lines}")
        if skipped_no_images:
            print(f"  skipped, no images : {skipped_no_images}")

        if exported and not with_images:
            print(
                "\nWARNING: no session had an archived source image, so nothing here can be\n"
                "re-run through an on-device extractor. Set PARSE_ARCHIVE_UPLOADS=true and\n"
                "collect fresh uploads before benchmarking."
            )
        if exported and from_debug < exported:
            print(
                "\nNOTE: sessions reconstructed from extracted_items cannot measure\n"
                "header_line_recall (Stage A header rows are not persisted as items).\n"
                "Set PARSE_DEBUG_SAVE_JSON=true for full-fidelity ground truth."
            )
    return 0


def _safe_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
