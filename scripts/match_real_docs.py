"""Offline matching harness for real-world document fixtures.

Runs transcribed handwritten material lists (tests/fixtures/real_docs/*.json)
through the production matching path — enrich_description_for_matching() +
item_matcher.match_items_batch() — against a catalog CSV, with no Flask app,
DB, or vision/OCR stage required.

Each fixture is a Stage-A-equivalent transcription: document-level context
(summary, global_material_context, job_notes) plus per-line rows of
{quantity, description, section?, brand?, color?}. Sections are prepended to
the line description the same way Stage A propagates section headers into
product_family for reconstruction.

Usage:
    python scripts/match_real_docs.py                       # all fixtures
    python scripts/match_real_docs.py --only espelund_deck  # one fixture
    python scripts/match_real_docs.py --json-out results.json

When huggingface.co is unreachable (locked-down environments), pass
--onnx-dir pointing at an ONNX export of the same model (e.g. the
qdrant-fastembed all-MiniLM-L6-v2 archive). The harness then swaps a
numerically-equivalent ONNX encoder into VectorIndex — same weights, same
mean pooling + L2 normalization — without touching production code.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.models import ERPItem  # noqa: E402
from app.services import item_matcher  # noqa: E402
from app.services.size_parser import parse_size_and_length  # noqa: E402
from app.services.upload_context import enrich_description_for_matching  # noqa: E402

DEFAULT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "real_docs"
DEFAULT_CATALOG = REPO_ROOT / "example_catalog.csv"
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class OnnxMiniLMEncoder:
    """Drop-in stand-in for SentenceTransformer backed by an ONNX export.

    Reproduces the all-MiniLM-L6-v2 sentence pipeline: BERT encoder →
    attention-masked mean pooling → L2 normalization, max_seq_length 256.
    Only the encode() surface VectorIndex uses is implemented.
    """

    onnx_dir: str | None = None  # set before use via install()

    def __init__(self, model_name: str):
        import numpy as _np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = _np
        directory = Path(type(self).onnx_dir)
        self.session = ort.InferenceSession(
            str(directory / "model.onnx"), providers=["CPUExecutionProvider"]
        )
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding()

    def encode(self, texts, convert_to_numpy=True, normalize_embeddings=True):
        np = self._np
        encodings = self.tokenizer.encode_batch(list(texts))
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)
        (hidden,) = self.session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )
        mask = attention_mask[:, :, None].astype(hidden.dtype)
        summed = (hidden * mask).sum(axis=1)
        counts = np.clip(mask.sum(axis=1), 1e-9, None)
        embeddings = summed / counts
        if normalize_embeddings:
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / np.clip(norms, 1e-12, None)
        return embeddings.astype(np.float32)

    @classmethod
    def install(cls, onnx_dir: Path) -> None:
        from app.services import vector_index

        cls.onnx_dir = str(onnx_dir)
        vector_index.SentenceTransformer = cls


def load_catalog(csv_path: Path) -> list[ERPItem]:
    """Build detached ERPItem rows from a catalog CSV.

    size/length fall back to parse_size_and_length(description) when the CSV
    has no explicit columns, matching what a fully-populated ERP export carries.
    """
    items: list[ERPItem] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("item_code") or row.get("sku") or "").strip()
            desc = (row.get("description") or "").strip()
            if not code or not desc:
                continue
            size = (row.get("size") or "").strip()
            length = (row.get("length") or "").strip()
            if not size and not length:
                size, length = parse_size_and_length(desc.lower())
            items.append(
                ERPItem(
                    item_code=code,
                    description=desc,
                    keywords=(row.get("keywords") or "").strip(),
                    category=(row.get("category") or "").strip(),
                    size=size or "",
                    length=length or "",
                    brand=(row.get("brand") or "").strip(),
                )
            )
    return items


def reconstruct_description(line: dict) -> str:
    """Mirror the production reconstruction: section/family context + base text."""
    parts = [line.get("section") or "", line.get("description") or ""]
    return " ".join(p for p in parts if p).strip()


def run_fixture(fixture: dict, catalog: list[ERPItem], model_name: str) -> list[dict]:
    context = fixture.get("document_context") or {}
    lines = fixture.get("lines") or []

    descriptions = [
        enrich_description_for_matching(
            reconstruct_description(line),
            upload_context="",
            document_context=context,
        )
        for line in lines
    ]
    brands = [line.get("brand") for line in lines]
    colors = [line.get("color") for line in lines]

    matches = item_matcher.match_items_batch(
        descriptions,
        catalog,
        model_name=model_name,
        parsed_brands=brands,
        parsed_colors=colors,
    )

    rows = []
    for line, enriched, match in zip(lines, descriptions, matches):
        rows.append(
            {
                "id": line.get("id"),
                "quantity": line.get("quantity"),
                "raw": line.get("description"),
                "enriched_query": enriched,
                "matched_item_code": match["matched_item_code"],
                "matched_description": match["matched_description"],
                "confidence_score": match["confidence_score"],
                "fuzzy_score": match["fuzzy_score"],
                "vector_score": match["vector_score"],
                "runner_up": (
                    {
                        "sku": match["candidates"][1]["sku"],
                        "confidence_score": match["candidates"][1]["confidence_score"],
                    }
                    if len(match.get("candidates") or []) > 1
                    else None
                ),
            }
        )
    return rows


def print_report(name: str, rows: list[dict]) -> None:
    print(f"\n{'=' * 100}")
    print(f"DOCUMENT: {name}  ({len(rows)} lines)")
    print(f"{'=' * 100}")
    print(f"{'ID':<5}{'QTY':>5}  {'RAW':<42}{'MATCH':<20}{'CONF':>6}{'FUZ':>6}{'VEC':>6}")
    print("-" * 100)
    for r in rows:
        conf = r["confidence_score"]
        flag = " " if conf >= 0.75 else ("~" if conf >= 0.55 else "!")
        raw = (r["raw"] or "")[:40]
        sku = (r["matched_item_code"] or "NO MATCH")[:19]
        print(
            f"{r['id']:<5}{r['quantity']:>5}  {raw:<42}{sku:<20}"
            f"{conf:>6.2f}{r['fuzzy_score']:>6.2f}{r['vector_score']:>6.2f} {flag}"
        )
    strong = sum(1 for r in rows if r["confidence_score"] >= 0.75)
    mid = sum(1 for r in rows if 0.55 <= r["confidence_score"] < 0.75)
    weak = len(rows) - strong - mid
    print("-" * 100)
    print(f"strong(>=0.75): {strong}   review(0.55-0.75): {mid}   weak(<0.55): {weak}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--only", help="run a single fixture by stem name")
    parser.add_argument("--json-out", type=Path, help="write full results as JSON")
    parser.add_argument(
        "--onnx-dir",
        type=Path,
        help="local ONNX export of the embedding model (offline fallback)",
    )
    args = parser.parse_args()

    if args.onnx_dir:
        OnnxMiniLMEncoder.install(args.onnx_dir)
        print(f"Using ONNX encoder from {args.onnx_dir}")

    catalog = load_catalog(args.catalog)
    if not catalog:
        print(f"No catalog rows loaded from {args.catalog}", file=sys.stderr)
        return 1
    print(f"Catalog: {args.catalog} ({len(catalog)} items)   Model: {args.model}")

    fixture_paths = sorted(args.fixtures.glob("*.json"))
    if args.only:
        fixture_paths = [p for p in fixture_paths if p.stem == args.only]
    if not fixture_paths:
        print(f"No fixtures found in {args.fixtures}", file=sys.stderr)
        return 1

    all_results: dict[str, list[dict]] = {}
    for path in fixture_paths:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        rows = run_fixture(fixture, catalog, args.model)
        all_results[path.stem] = rows
        print_report(path.stem, rows)

    if args.json_out:
        args.json_out.write_text(
            json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nFull results written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
