import unittest
from unittest.mock import patch

from app.models import ERPItem
from app.services import item_matcher
from app.services.size_parser import parse_size_and_length


class FakeIndex:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, k=5):
        return self.hits[:k]


class Hit:
    def __init__(self, sku, score):
        self.sku = sku
        self.score = score


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [
            ERPItem(item_code="0210tre16", description="2x10 16ft #2 SYP Treated Joist", size="2x10", length="16", keywords="joist treated"),
            ERPItem(item_code="LUS210", description="LUS210 Face-Mount Joist Hanger", keywords="hanger joist"),
            ERPItem(item_code="DBSCREWBRZ", description="Bronze Deck Screws 3in", keywords="deck screws bronze"),
        ]

    def test_parse_size_and_length(self):
        self.assertEqual(parse_size_and_length("2x10 16 treated"), ("2x10", "16"))
        self.assertEqual(parse_size_and_length("5/4x6x16 decking"), ("5/4x6", "16"))

    def test_feedback_rerank_boosts_corrected_sku(self):
        candidates = [
            {"sku": "0210tre16", "description": "treated", "confidence_score": 0.70, "fuzzy_score": 0.6, "vector_score": 0.8},
            {"sku": "LUS210", "description": "hanger", "confidence_score": 0.74, "fuzzy_score": 0.7, "vector_score": 0.76},
        ]

        reranked = item_matcher._apply_feedback_rerank(candidates, {"0210tre16": 6, "LUS210": 1})

        self.assertEqual(reranked[0]["sku"], "0210tre16")
        self.assertGreater(reranked[0]["confidence_score"], reranked[1]["confidence_score"])
        self.assertGreater(reranked[0]["feedback_boost"], reranked[1]["feedback_boost"])

    @patch("app.services.item_matcher._alias_lookup", return_value=None)
    @patch("app.services.item_matcher._ensure_vector_index")
    def test_hybrid_match_prefers_lumber_item(self, mock_index, _mock_alias):
        mock_index.return_value = FakeIndex([
            Hit("0210tre16", 0.95),
            Hit("LUS210", 0.35),
            Hit("DBSCREWBRZ", 0.2),
        ])

        result = item_matcher.match_item("2x10 16 treated", self.catalog, "sentence-transformers/all-MiniLM-L6-v2")

        self.assertEqual(result["matched_item_code"], "0210tre16")
        self.assertGreaterEqual(result["confidence_score"], 0.8)
        self.assertEqual(len(result["candidates"]), 3)

    @patch("app.services.item_matcher._alias_lookup", return_value=None)
    @patch("app.services.item_matcher._ensure_vector_index")
    def test_hybrid_match_handles_messy_hanger_input(self, mock_index, _mock_alias):
        mock_index.return_value = FakeIndex([
            Hit("LUS210", 0.93),
            Hit("0210tre16", 0.2),
        ])

        result = item_matcher.match_item("lus210 hanger", self.catalog, "sentence-transformers/all-MiniLM-L6-v2")
        self.assertEqual(result["matched_item_code"], "LUS210")


if __name__ == "__main__":
    unittest.main()
