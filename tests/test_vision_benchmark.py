import unittest

from app.services.vision_benchmark import (
    BenchmarkLine,
    aggregate,
    align_lines,
    lines_from_payload,
    normalize_text,
    score_session,
    text_similarity,
)


def line(raw_text, quantity=None, section_header="", section_type="item", line_id=""):
    return BenchmarkLine(
        line_id=line_id,
        raw_text=raw_text,
        section_header=section_header,
        section_type=section_type,
        quantity=quantity,
    )


class NormalizeTests(unittest.TestCase):
    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(normalize_text("  2x10  SYP  Joist "), normalize_text("2X10 syp joist"))

    def test_preserves_dimension_characters(self):
        # 3-1/8 vs 31/8 must not collapse — they are different SKUs.
        self.assertNotEqual(normalize_text("3-1/8 screw"), normalize_text("31/8 screw"))
        self.assertIn("3-1/8", normalize_text("3-1/8 screw"))

    def test_strips_incidental_punctuation(self):
        self.assertEqual(normalize_text("Cinnamon Cove:"), "cinnamon cove")

    def test_empty(self):
        self.assertEqual(normalize_text(""), "")


class SimilarityTests(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertEqual(text_similarity("2x10 joist", "2x10 joist"), 1.0)

    def test_both_empty_is_one(self):
        self.assertEqual(text_similarity("", ""), 1.0)

    def test_one_empty_is_zero(self):
        self.assertEqual(text_similarity("2x10", ""), 0.0)

    def test_ocr_error_scores_high_but_not_perfect(self):
        score = text_similarity("LUS210 hangers", "1os210 hangers")
        self.assertGreater(score, 0.6)
        self.assertLess(score, 1.0)

    def test_different_dimensions_penalised(self):
        same_size = text_similarity("9x3-1/8 bronze star screw", "9x3-1/8 bronze star screw")
        wrong_size = text_similarity("9x3-1/8 bronze star screw", "9x1-1/2 bronze star screw")
        self.assertEqual(same_size, 1.0)
        self.assertLess(wrong_size, same_size)


class AlignmentTests(unittest.TestCase):
    def test_perfect_alignment(self):
        rows = [line("2x10 joist"), line("6x6 post"), line("LUS210 hanger")]
        alignment = align_lines(rows, list(rows))
        self.assertEqual(len(alignment.pairs), 3)
        self.assertEqual(alignment.unmatched_expected, [])
        self.assertEqual(alignment.unmatched_actual, [])

    def test_missing_line_is_unmatched_not_mispaired(self):
        expected = [line("2x10 joist"), line("6x6 post"), line("LUS210 hanger")]
        actual = [line("2x10 joist"), line("LUS210 hanger")]
        alignment = align_lines(expected, actual)
        self.assertEqual(len(alignment.pairs), 2)
        self.assertEqual(alignment.unmatched_expected, [1])
        self.assertEqual(alignment.unmatched_actual, [])

    def test_spurious_line_reported(self):
        expected = [line("2x10 joist")]
        actual = [line("2x10 joist"), line("page 1 of 2")]
        alignment = align_lines(expected, actual)
        self.assertEqual(len(alignment.pairs), 1)
        self.assertEqual(alignment.unmatched_actual, [1])

    def test_dissimilar_lines_are_not_paired(self):
        expected = [line("2x10 SYP treated joist")]
        actual = [line("Simpson strong tie hanger")]
        alignment = align_lines(expected, actual)
        self.assertEqual(alignment.pairs, [])
        self.assertEqual(alignment.unmatched_expected, [0])
        self.assertEqual(alignment.unmatched_actual, [0])

    def test_alignment_preserves_document_order(self):
        # Near-identical repeated rows must pair positionally, not arbitrarily.
        expected = [line("2x10x16 joist"), line("2x10x12 joist"), line("2x10x14 joist")]
        actual = [line("2x10x16 joist"), line("2x10x12 joist"), line("2x10x14 joist")]
        alignment = align_lines(expected, actual)
        for pair in alignment.pairs:
            self.assertEqual(pair.expected_index, pair.actual_index)

    def test_empty_inputs(self):
        self.assertEqual(align_lines([], []).pairs, [])
        alignment = align_lines([line("a")], [])
        self.assertEqual(alignment.unmatched_expected, [0])
        self.assertEqual(alignment.unmatched_actual, [])


class ScoringTests(unittest.TestCase):
    def test_identical_extraction_scores_perfect(self):
        rows = [
            line("2x10 joist", quantity=25, section_header="Deck Framing"),
            line("6x6 post", quantity=2, section_header="Deck Framing"),
        ]
        score = score_session(rows, list(rows))
        self.assertEqual(score.line_recall, 1.0)
        self.assertEqual(score.line_precision, 1.0)
        self.assertEqual(score.line_f1, 1.0)
        self.assertEqual(score.quantity_accuracy, 1.0)
        self.assertEqual(score.header_attr_accuracy, 1.0)
        self.assertEqual(score.exact_text_matches, 2)
        self.assertEqual(score.missed_lines, [])

    def test_quantity_error_detected(self):
        expected = [line("2x10 joist", quantity=25)]
        actual = [line("2x10 joist", quantity=23)]
        score = score_session(expected, actual)
        self.assertEqual(score.line_recall, 1.0)
        self.assertEqual(score.quantity_comparable, 1)
        self.assertEqual(score.quantity_correct, 0)
        self.assertEqual(score.quantity_accuracy, 0.0)
        self.assertEqual(score.quantity_errors[0]["expected"], 25)
        self.assertEqual(score.quantity_errors[0]["actual"], 23)

    def test_missing_quantity_counts_as_wrong_not_skipped(self):
        expected = [line("2x10 joist", quantity=25)]
        actual = [line("2x10 joist", quantity=None)]
        score = score_session(expected, actual)
        self.assertEqual(score.quantity_comparable, 1)
        self.assertEqual(score.quantity_correct, 0)

    def test_quantity_not_scored_when_ground_truth_lacks_one(self):
        expected = [line("misc note", quantity=None)]
        actual = [line("misc note", quantity=7)]
        score = score_session(expected, actual)
        self.assertEqual(score.quantity_comparable, 0)
        self.assertEqual(score.quantity_accuracy, 0.0)

    def test_header_attribution_failure_is_the_headline_metric(self):
        # Text read perfectly, quantities right, but the heading did not carry
        # down — precisely the on-device risk the benchmark exists to measure.
        expected = [
            line("56 Grooved x 12", quantity=56, section_header="Cinnamon Cove"),
            line("13 Round x 16", quantity=13, section_header="Cinnamon Cove"),
        ]
        actual = [
            line("56 Grooved x 12", quantity=56, section_header=""),
            line("13 Round x 16", quantity=13, section_header=""),
        ]
        score = score_session(expected, actual)
        self.assertEqual(score.line_recall, 1.0)
        self.assertEqual(score.quantity_accuracy, 1.0)
        self.assertEqual(score.header_attr_comparable, 2)
        self.assertEqual(score.header_attr_correct, 0)
        self.assertEqual(score.header_attr_accuracy, 0.0)
        self.assertEqual(len(score.header_errors), 2)

    def test_header_comparison_ignores_trailing_colon(self):
        expected = [line("56 Grooved x 12", section_header="Cinnamon Cove")]
        actual = [line("56 Grooved x 12", section_header="Cinnamon Cove:")]
        score = score_session(expected, actual)
        self.assertEqual(score.header_attr_correct, 1)

    def test_header_lines_tracked_separately(self):
        expected = [
            line("Cinnamon Cove", quantity=0, section_type="header"),
            line("56 Grooved x 12", quantity=56, section_header="Cinnamon Cove"),
        ]
        actual = [line("56 Grooved x 12", quantity=56, section_header="Cinnamon Cove")]
        score = score_session(expected, actual)
        self.assertEqual(score.header_lines_expected, 1)
        self.assertEqual(score.header_lines_found, 0)
        self.assertEqual(score.header_line_recall, 0.0)

    def test_missed_and_spurious_lines_listed(self):
        expected = [line("2x10 joist"), line("6x6 post")]
        actual = [line("2x10 joist"), line("Page 1 of 3")]
        score = score_session(expected, actual)
        self.assertEqual(score.missed_lines, ["6x6 post"])
        self.assertEqual(score.spurious_lines, ["Page 1 of 3"])
        self.assertEqual(score.line_recall, 0.5)
        self.assertEqual(score.line_precision, 0.5)

    def test_empty_actual_scores_zero_without_crashing(self):
        score = score_session([line("2x10 joist", quantity=4)], [])
        self.assertEqual(score.line_recall, 0.0)
        self.assertEqual(score.line_precision, 0.0)
        self.assertEqual(score.line_f1, 0.0)
        self.assertEqual(score.mean_text_similarity, 0.0)
        self.assertEqual(score.missed_lines, ["2x10 joist"])


class AggregateTests(unittest.TestCase):
    def test_micro_average_weights_longer_lists_more(self):
        long_rows = [line(f"2x10x{n} joist", quantity=n) for n in range(10, 30)]
        long_score = score_session(long_rows, list(long_rows), session_id="long")

        short_expected = [line("6x6 post", quantity=2)]
        short_actual = [line("6x6 post", quantity=99)]
        short_score = score_session(short_expected, short_actual, session_id="short")

        corpus = aggregate([long_score, short_score])
        self.assertEqual(corpus.sessions, 2)
        self.assertEqual(corpus.expected_lines, 21)
        # 20 of 21 quantities correct — the one-line session must not drag this
        # toward 0.5 the way a macro average would.
        self.assertAlmostEqual(corpus.quantity_accuracy, 20 / 21, places=4)

    def test_empty_corpus_is_safe(self):
        corpus = aggregate([])
        self.assertEqual(corpus.sessions, 0)
        self.assertEqual(corpus.line_recall, 0.0)
        self.assertEqual(corpus.line_f1, 0.0)
        self.assertEqual(corpus.mean_text_similarity, 0.0)


class PayloadTests(unittest.TestCase):
    def test_reads_wrapper_object(self):
        rows = lines_from_payload({"lines": [{"raw_text": "2x10 joist", "quantity": 25}]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quantity, 25.0)

    def test_reads_bare_list(self):
        rows = lines_from_payload([{"raw_text": "2x10 joist"}])
        self.assertEqual(len(rows), 1)

    def test_tolerates_missing_and_malformed_fields(self):
        rows = lines_from_payload({"lines": [{}, "not a dict", {"quantity": "abc"}]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].raw_text, "")
        self.assertIsNone(rows[1].quantity)

    def test_string_quantity_coerced(self):
        rows = lines_from_payload({"lines": [{"raw_text": "x", "quantity": "25"}]})
        self.assertEqual(rows[0].quantity, 25.0)

    def test_header_flag(self):
        rows = lines_from_payload({"lines": [{"raw_text": "Cove", "section_type": "header"}]})
        self.assertTrue(rows[0].is_header)


if __name__ == "__main__":
    unittest.main()
