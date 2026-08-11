"""Score on-device Vision OCR output against the cloud Stage A ground truth.

Usage (run from repo root):
    python scripts/score_vision_benchmark.py --corpus data/vision_benchmark
    python scripts/score_vision_benchmark.py --corpus data/vision_benchmark --detail
    python scripts/score_vision_benchmark.py --corpus data/vision_benchmark --json report.json
    python scripts/score_vision_benchmark.py --corpus data/vision_benchmark --threshold 0.7

Expects a corpus laid out by ``export_vision_benchmark.py``::

    data/vision_benchmark/
      session_25/
        expected.json     <- written by the exporter (cloud Stage A)
        vision.json       <- written by you (on-device extractor output)
        01_list.jpg

Sessions with no ``vision.json`` are reported as pending and excluded from the
totals, so you can score a partially-processed corpus while the rest runs.

See docs/VISION_BENCHMARK.md for the vision.json schema and a Swift reference
implementation that produces it.

Requires no API key and makes no network calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the project root is on the path so app/ is importable
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services.vision_benchmark import (  # noqa: E402
    DEFAULT_ALIGN_THRESHOLD,
    aggregate,
    lines_from_payload,
    score_session,
)


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ! could not read {path.name}: {exc}")
        return None


def _print_detail(score) -> None:
    if score.missed_lines:
        print(f"    missed ({len(score.missed_lines)}):")
        for text in score.missed_lines[:10]:
            print(f"      - {text}")
        if len(score.missed_lines) > 10:
            print(f"      ... and {len(score.missed_lines) - 10} more")

    if score.spurious_lines:
        print(f"    spurious ({len(score.spurious_lines)}):")
        for text in score.spurious_lines[:10]:
            print(f"      + {text}")
        if len(score.spurious_lines) > 10:
            print(f"      ... and {len(score.spurious_lines) - 10} more")

    if score.text_errors:
        print(f"    text mismatches ({len(score.text_errors)}):")
        for error in score.text_errors[:10]:
            print(f"      exp: {error['expected']}")
            print(f"      got: {error['actual']}   (sim {error['similarity']})")
        if len(score.text_errors) > 10:
            print(f"      ... and {len(score.text_errors) - 10} more")

    if score.quantity_errors:
        print(f"    quantity errors ({len(score.quantity_errors)}):")
        for error in score.quantity_errors[:10]:
            print(f"      {error['raw_text']!r}: expected {error['expected']}, got {error['actual']}")
        if len(score.quantity_errors) > 10:
            print(f"      ... and {len(score.quantity_errors) - 10} more")

    if score.header_errors:
        print(f"    header attribution errors ({len(score.header_errors)}):")
        for error in score.header_errors[:10]:
            print(f"      {error['raw_text']!r}: expected {error['expected']!r}, got {error['actual']!r}")
        if len(score.header_errors) > 10:
            print(f"      ... and {len(score.header_errors) - 10} more")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", default="data/vision_benchmark", help="Corpus directory")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_ALIGN_THRESHOLD,
        help=f"Text similarity below which two lines are not considered the same row (default {DEFAULT_ALIGN_THRESHOLD})",
    )
    parser.add_argument("--detail", action="store_true", help="Print per-session error listings")
    parser.add_argument("--json", dest="json_out", default="", help="Write the full report to this path")
    args = parser.parse_args()

    corpus_root = Path(args.corpus)
    if not corpus_root.is_dir():
        print(f"Corpus directory not found: {corpus_root}")
        print("Run scripts/export_vision_benchmark.py first.")
        return 1

    session_dirs = sorted(
        (path for path in corpus_root.iterdir() if path.is_dir() and (path / "expected.json").exists()),
        key=lambda path: path.name,
    )
    if not session_dirs:
        print(f"No sessions with expected.json found under {corpus_root}")
        return 1

    scores = []
    pending = []
    degraded = []

    for session_dir in session_dirs:
        expected_payload = _load(session_dir / "expected.json")
        if expected_payload is None:
            continue

        vision_path = session_dir / "vision.json"
        if not vision_path.exists():
            pending.append(session_dir.name)
            continue

        actual_payload = _load(vision_path)
        if actual_payload is None:
            continue

        expected_lines = lines_from_payload(expected_payload)
        actual_lines = lines_from_payload(actual_payload)
        score = score_session(
            expected_lines,
            actual_lines,
            session_id=session_dir.name,
            threshold=args.threshold,
        )
        scores.append(score)

        if expected_payload.get("source") == "extracted_items":
            degraded.append(session_dir.name)

        print(
            f"{session_dir.name}: "
            f"recall {score.line_recall:.2f}  "
            f"precision {score.line_precision:.2f}  "
            f"text {score.mean_text_similarity:.3f}  "
            f"qty {score.quantity_accuracy:.2f}  "
            f"header {score.header_attr_accuracy:.2f}  "
            f"({score.expected_lines} expected / {score.actual_lines} actual)"
        )
        if args.detail:
            _print_detail(score)

    if not scores:
        print("\nNo vision.json files found — nothing scored.")
        print(f"Pending sessions: {len(pending)}")
        print("See docs/VISION_BENCHMARK.md for how to generate vision.json.")
        return 1

    corpus = aggregate(scores)

    print("\n" + "=" * 62)
    print("CORPUS TOTALS (micro-averaged across all lines)")
    print("=" * 62)
    print(f"  sessions scored          : {corpus.sessions}")
    print(f"  expected lines           : {corpus.expected_lines}")
    print(f"  actual lines             : {corpus.actual_lines}")
    print(f"  line recall              : {corpus.line_recall:.4f}")
    print(f"  line precision           : {corpus.line_precision:.4f}")
    print(f"  line F1                  : {corpus.line_f1:.4f}")
    print(f"  mean text similarity     : {corpus.mean_text_similarity:.4f}")
    print(f"  exact text match rate    : {corpus.exact_text_rate:.4f}")
    print(f"  quantity accuracy        : {corpus.quantity_accuracy:.4f}  (n={corpus.quantity_comparable})")
    print(f"  header attribution acc.  : {corpus.header_attr_accuracy:.4f}  (n={corpus.header_attr_comparable})")
    if corpus.header_lines_expected:
        print(f"  header line recall       : {corpus.header_line_recall:.4f}  (n={corpus.header_lines_expected})")
    else:
        print("  header line recall       : n/a (no header lines in ground truth)")

    if pending:
        print(f"\n  pending (no vision.json) : {len(pending)}")
    if degraded:
        print(
            f"\n  NOTE: {len(degraded)} session(s) used extracted_items as ground truth.\n"
            "  Header line recall is not measurable for those — see the exporter notes."
        )

    print(
        "\nInterpretation: text similarity and quantity accuracy tell you whether\n"
        "on-device OCR can read the page. Header attribution accuracy tells you\n"
        "whether it can understand the page — that is the metric that decides\n"
        "whether Stage A can move on-device, since it is what the cloud model's\n"
        "visual layout understanding currently provides."
    )

    if args.json_out:
        report = {
            "threshold": args.threshold,
            "corpus": corpus.to_dict(),
            "pending": pending,
            "degraded_ground_truth": degraded,
            "sessions": [score.to_dict() for score in scores],
        }
        Path(args.json_out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull report written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
