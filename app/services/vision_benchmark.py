"""Scoring core for on-device Vision OCR vs. cloud Stage A benchmarking.

This module is deliberately free of Flask and OpenAI imports so it can be
exercised directly by unit tests and by the CLI scripts in ``scripts/``.

The benchmark answers one question: **can Apple Vision (or Android ML Kit)
running on-device reproduce what the cloud multimodal Stage A pass produces
today?**  Both sides are reduced to the same line shape, aligned in document
order, and scored on the dimensions that actually matter downstream:

  * line recall / precision  — did we find the same rows at all?
  * text similarity          — did we read the characters correctly?
  * quantity accuracy        — did we read the counts correctly?
  * section header accuracy  — did we attribute rows to the right heading?

Section header attribution is scored separately because it is the part of
Stage A that depends on *seeing* the page layout rather than merely reading
glyphs, and is therefore the capability most at risk when moving on-device.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from rapidfuzz import fuzz

# A pair whose text similarity falls below this is treated as "not the same
# line" — the aligner prefers leaving both sides unmatched over pairing them.
DEFAULT_ALIGN_THRESHOLD = 0.60

# Quantities are floats; compare with a tolerance rather than ==.
_QUANTITY_EPSILON = 1e-6

_PUNCT_RE = re.compile(r"[^\w\s/\-.']+")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Line model
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkLine:
    """One extracted row, from either the expected or the actual side."""

    line_id: str = ""
    raw_text: str = ""
    section_header: str = ""
    section_type: str = "unknown"
    quantity: float | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BenchmarkLine":
        return cls(
            line_id=str(payload.get("line_id") or ""),
            raw_text=str(payload.get("raw_text") or ""),
            section_header=str(payload.get("section_header") or ""),
            section_type=str(payload.get("section_type") or "unknown"),
            quantity=_coerce_optional_float(payload.get("quantity")),
        )

    @property
    def is_header(self) -> bool:
        return self.section_type.strip().lower() == "header"


def _coerce_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """Case/'punctuation/whitespace-insensitive form used for comparisons.

    Dimension-bearing characters (``/``, ``-``, ``.``) are preserved because
    ``3-1/8`` and ``31/8`` are genuinely different items in this domain, and
    collapsing them would hide exactly the OCR failures we are hunting for.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text)
    folded = folded.replace("’", "'").replace("‘", "'")
    folded = folded.replace("“", '"').replace("”", '"')
    folded = _PUNCT_RE.sub(" ", folded)
    folded = _WS_RE.sub(" ", folded)
    return folded.strip().lower()


def text_similarity(left: str, right: str) -> float:
    """Character-level similarity in ``[0, 1]`` over normalised text.

    ``fuzz.ratio`` is used rather than a token-set variant on purpose: OCR
    errors are character errors, and a token-set ratio would happily call
    ``"2x10-16 joist"`` and ``"2x10-12 joist"`` a perfect match.
    """
    a, b = normalize_text(left), normalize_text(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return fuzz.ratio(a, b) / 100.0


# ---------------------------------------------------------------------------
# Order-preserving alignment
# ---------------------------------------------------------------------------


@dataclass
class AlignedPair:
    expected_index: int
    actual_index: int
    similarity: float
    expected: BenchmarkLine
    actual: BenchmarkLine


@dataclass
class Alignment:
    pairs: list[AlignedPair] = field(default_factory=list)
    unmatched_expected: list[int] = field(default_factory=list)
    unmatched_actual: list[int] = field(default_factory=list)


def align_lines(
    expected: Sequence[BenchmarkLine],
    actual: Sequence[BenchmarkLine],
    threshold: float = DEFAULT_ALIGN_THRESHOLD,
) -> Alignment:
    """Align two line sequences with Needleman-Wunsch, preserving order.

    Order matters: a greedy global matcher will happily pair expected line 3
    with actual line 47 when a list repeats similar rows (very common — a deck
    list has a dozen near-identical joist rows), which inflates recall while
    destroying the quantity and header comparisons that follow.

    A pair contributes ``similarity - threshold`` to the DP objective, so any
    pairing weaker than ``threshold`` scores worse than leaving both sides as
    gaps.  That makes the threshold a genuine "same line" decision boundary
    rather than a post-hoc filter.
    """
    n, m = len(expected), len(actual)
    if n == 0 or m == 0:
        return Alignment(
            pairs=[],
            unmatched_expected=list(range(n)),
            unmatched_actual=list(range(m)),
        )

    # score[i][j] = best objective for expected[:i] against actual[:j]
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    # 0 = gap in actual (skip expected), 1 = gap in expected, 2 = pair
    back = [[0] * (m + 1) for _ in range(n + 1)]

    for j in range(1, m + 1):
        back[0][j] = 1

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            pair_gain = text_similarity(expected[i - 1].raw_text, actual[j - 1].raw_text) - threshold
            diagonal = score[i - 1][j - 1] + pair_gain
            skip_expected = score[i - 1][j]
            skip_actual = score[i][j - 1]

            best = skip_expected
            move = 0
            if skip_actual > best:
                best, move = skip_actual, 1
            if diagonal > best:
                best, move = diagonal, 2

            score[i][j] = best
            back[i][j] = move

    pairs: list[AlignedPair] = []
    unmatched_expected: list[int] = []
    unmatched_actual: list[int] = []

    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and back[i][j] == 2:
            similarity = text_similarity(expected[i - 1].raw_text, actual[j - 1].raw_text)
            pairs.append(
                AlignedPair(
                    expected_index=i - 1,
                    actual_index=j - 1,
                    similarity=similarity,
                    expected=expected[i - 1],
                    actual=actual[j - 1],
                )
            )
            i, j = i - 1, j - 1
        elif j > 0 and (i == 0 or back[i][j] == 1):
            unmatched_actual.append(j - 1)
            j -= 1
        else:
            unmatched_expected.append(i - 1)
            i -= 1

    pairs.reverse()
    unmatched_expected.reverse()
    unmatched_actual.reverse()
    return Alignment(pairs=pairs, unmatched_expected=unmatched_expected, unmatched_actual=unmatched_actual)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class SessionScore:
    session_id: str = ""
    expected_lines: int = 0
    actual_lines: int = 0
    aligned_lines: int = 0

    line_recall: float = 0.0
    line_precision: float = 0.0
    line_f1: float = 0.0

    mean_text_similarity: float = 0.0
    exact_text_matches: int = 0

    quantity_comparable: int = 0
    quantity_correct: int = 0

    header_attr_comparable: int = 0
    header_attr_correct: int = 0

    header_lines_expected: int = 0
    header_lines_found: int = 0

    missed_lines: list[str] = field(default_factory=list)
    spurious_lines: list[str] = field(default_factory=list)
    text_errors: list[dict[str, Any]] = field(default_factory=list)
    quantity_errors: list[dict[str, Any]] = field(default_factory=list)
    header_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def quantity_accuracy(self) -> float:
        return _safe_ratio(self.quantity_correct, self.quantity_comparable)

    @property
    def header_attr_accuracy(self) -> float:
        return _safe_ratio(self.header_attr_correct, self.header_attr_comparable)

    @property
    def header_line_recall(self) -> float:
        return _safe_ratio(self.header_lines_found, self.header_lines_expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "expected_lines": self.expected_lines,
            "actual_lines": self.actual_lines,
            "aligned_lines": self.aligned_lines,
            "line_recall": round(self.line_recall, 4),
            "line_precision": round(self.line_precision, 4),
            "line_f1": round(self.line_f1, 4),
            "mean_text_similarity": round(self.mean_text_similarity, 4),
            "exact_text_matches": self.exact_text_matches,
            "quantity_accuracy": round(self.quantity_accuracy, 4),
            "quantity_comparable": self.quantity_comparable,
            "quantity_correct": self.quantity_correct,
            "header_attr_accuracy": round(self.header_attr_accuracy, 4),
            "header_attr_comparable": self.header_attr_comparable,
            "header_attr_correct": self.header_attr_correct,
            "header_line_recall": round(self.header_line_recall, 4),
            "header_lines_expected": self.header_lines_expected,
            "header_lines_found": self.header_lines_found,
            "missed_lines": self.missed_lines,
            "spurious_lines": self.spurious_lines,
            "text_errors": self.text_errors,
            "quantity_errors": self.quantity_errors,
            "header_errors": self.header_errors,
        }


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def score_session(
    expected: Sequence[BenchmarkLine],
    actual: Sequence[BenchmarkLine],
    session_id: str = "",
    threshold: float = DEFAULT_ALIGN_THRESHOLD,
    exact_text_similarity: float = 0.999,
) -> SessionScore:
    """Compare one session's Vision output against its Stage A ground truth."""
    alignment = align_lines(expected, actual, threshold=threshold)

    result = SessionScore(
        session_id=session_id,
        expected_lines=len(expected),
        actual_lines=len(actual),
        aligned_lines=len(alignment.pairs),
    )

    result.line_recall = _safe_ratio(len(alignment.pairs), len(expected))
    result.line_precision = _safe_ratio(len(alignment.pairs), len(actual))
    if result.line_recall + result.line_precision > 0:
        result.line_f1 = (
            2 * result.line_recall * result.line_precision / (result.line_recall + result.line_precision)
        )

    similarities: list[float] = []
    for pair in alignment.pairs:
        similarities.append(pair.similarity)
        if pair.similarity >= exact_text_similarity:
            result.exact_text_matches += 1
        else:
            result.text_errors.append(
                {
                    "expected": pair.expected.raw_text,
                    "actual": pair.actual.raw_text,
                    "similarity": round(pair.similarity, 4),
                }
            )

        # Quantity — only scored where the ground truth actually has one.
        expected_qty = pair.expected.quantity
        if expected_qty is not None:
            result.quantity_comparable += 1
            actual_qty = pair.actual.quantity
            if actual_qty is not None and abs(actual_qty - expected_qty) < _QUANTITY_EPSILON:
                result.quantity_correct += 1
            else:
                result.quantity_errors.append(
                    {
                        "raw_text": pair.expected.raw_text,
                        "expected": expected_qty,
                        "actual": actual_qty,
                    }
                )

        # Section header attribution — only scored where the ground truth
        # assigned a heading, since "no header" is the trivial majority case
        # and would swamp the metric.
        expected_header = normalize_text(pair.expected.section_header)
        if expected_header:
            result.header_attr_comparable += 1
            if normalize_text(pair.actual.section_header) == expected_header:
                result.header_attr_correct += 1
            else:
                result.header_errors.append(
                    {
                        "raw_text": pair.expected.raw_text,
                        "expected": pair.expected.section_header,
                        "actual": pair.actual.section_header,
                    }
                )

    result.mean_text_similarity = sum(similarities) / len(similarities) if similarities else 0.0

    aligned_expected = {pair.expected_index for pair in alignment.pairs}
    for index, line in enumerate(expected):
        if line.is_header:
            result.header_lines_expected += 1
            if index in aligned_expected:
                result.header_lines_found += 1

    result.missed_lines = [expected[i].raw_text for i in alignment.unmatched_expected]
    result.spurious_lines = [actual[i].raw_text for i in alignment.unmatched_actual]
    return result


# ---------------------------------------------------------------------------
# Corpus aggregation
# ---------------------------------------------------------------------------


@dataclass
class CorpusScore:
    sessions: int = 0
    expected_lines: int = 0
    actual_lines: int = 0
    aligned_lines: int = 0
    exact_text_matches: int = 0
    quantity_comparable: int = 0
    quantity_correct: int = 0
    header_attr_comparable: int = 0
    header_attr_correct: int = 0
    header_lines_expected: int = 0
    header_lines_found: int = 0
    weighted_similarity: float = 0.0

    @property
    def line_recall(self) -> float:
        return _safe_ratio(self.aligned_lines, self.expected_lines)

    @property
    def line_precision(self) -> float:
        return _safe_ratio(self.aligned_lines, self.actual_lines)

    @property
    def line_f1(self) -> float:
        recall, precision = self.line_recall, self.line_precision
        if recall + precision == 0:
            return 0.0
        return 2 * recall * precision / (recall + precision)

    @property
    def mean_text_similarity(self) -> float:
        return self.weighted_similarity / self.aligned_lines if self.aligned_lines else 0.0

    @property
    def exact_text_rate(self) -> float:
        return _safe_ratio(self.exact_text_matches, self.aligned_lines)

    @property
    def quantity_accuracy(self) -> float:
        return _safe_ratio(self.quantity_correct, self.quantity_comparable)

    @property
    def header_attr_accuracy(self) -> float:
        return _safe_ratio(self.header_attr_correct, self.header_attr_comparable)

    @property
    def header_line_recall(self) -> float:
        return _safe_ratio(self.header_lines_found, self.header_lines_expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "expected_lines": self.expected_lines,
            "actual_lines": self.actual_lines,
            "aligned_lines": self.aligned_lines,
            "line_recall": round(self.line_recall, 4),
            "line_precision": round(self.line_precision, 4),
            "line_f1": round(self.line_f1, 4),
            "mean_text_similarity": round(self.mean_text_similarity, 4),
            "exact_text_rate": round(self.exact_text_rate, 4),
            "quantity_accuracy": round(self.quantity_accuracy, 4),
            "quantity_comparable": self.quantity_comparable,
            "header_attr_accuracy": round(self.header_attr_accuracy, 4),
            "header_attr_comparable": self.header_attr_comparable,
            "header_line_recall": round(self.header_line_recall, 4),
            "header_lines_expected": self.header_lines_expected,
        }


def aggregate(scores: Iterable[SessionScore]) -> CorpusScore:
    """Micro-average session scores into a corpus-level verdict.

    Micro rather than macro: a 90-line list should carry more weight than a
    4-line list when deciding whether on-device extraction is viable.
    """
    corpus = CorpusScore()
    for score in scores:
        corpus.sessions += 1
        corpus.expected_lines += score.expected_lines
        corpus.actual_lines += score.actual_lines
        corpus.aligned_lines += score.aligned_lines
        corpus.exact_text_matches += score.exact_text_matches
        corpus.quantity_comparable += score.quantity_comparable
        corpus.quantity_correct += score.quantity_correct
        corpus.header_attr_comparable += score.header_attr_comparable
        corpus.header_attr_correct += score.header_attr_correct
        corpus.header_lines_expected += score.header_lines_expected
        corpus.header_lines_found += score.header_lines_found
        corpus.weighted_similarity += score.mean_text_similarity * score.aligned_lines
    return corpus


# ---------------------------------------------------------------------------
# Document (de)serialisation
# ---------------------------------------------------------------------------


def lines_from_payload(payload: Any) -> list[BenchmarkLine]:
    """Read a ``lines`` array from either a bare list or a wrapper object."""
    if isinstance(payload, dict):
        raw = payload.get("lines", [])
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    return [BenchmarkLine.from_dict(row) for row in raw if isinstance(row, dict)]


def load_lines(path: str) -> list[BenchmarkLine]:
    with open(path, "r", encoding="utf-8") as handle:
        return lines_from_payload(json.load(handle))
