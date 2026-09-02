"""Regression tests for the real-document fixture set.

These run the transcribed handwritten lists (tests/fixtures/real_docs/) through
the production match path against example_catalog.csv using the fuzzy-only
fallback (vector model patched out), so they run in CI without downloading the
sentence-transformers model. Full hybrid runs use scripts/match_real_docs.py.
"""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import item_matcher

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "real_docs"
CATALOG_CSV = REPO_ROOT / "example_catalog.csv"


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "match_real_docs", REPO_ROOT / "scripts" / "match_real_docs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_harness()


class RealDocFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = harness.load_catalog(CATALOG_CSV)
        cls.fixtures = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(FIXTURES_DIR.glob("*.json"))
        }

    def setUp(self):
        item_matcher.clear_index()
        # Force the fuzzy-only fallback so no model download happens in CI.
        patcher = patch("app.services.vector_index.SentenceTransformer", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(item_matcher.clear_index)

    def _run(self, name):
        return harness.run_fixture(self.fixtures[name], self.catalog, "unused-model")

    def test_fixture_files_present_and_well_formed(self):
        self.assertGreaterEqual(len(self.fixtures), 6)
        for name, fixture in self.fixtures.items():
            with self.subTest(fixture=name):
                self.assertIn("document_context", fixture)
                self.assertTrue(fixture.get("lines"), f"{name} has no lines")
                for line in fixture["lines"]:
                    self.assertTrue(line.get("id"))
                    self.assertTrue(line.get("description"))
                    self.assertIsInstance(line.get("quantity"), (int, float))

    def test_all_lines_return_bounded_scores(self):
        for name in self.fixtures:
            with self.subTest(fixture=name):
                rows = self._run(name)
                self.assertEqual(len(rows), len(self.fixtures[name]["lines"]))
                for row in rows:
                    self.assertGreaterEqual(row["confidence_score"], 0.0)
                    self.assertLessEqual(row["confidence_score"], 1.0)

    def test_espelund_treated_posts_match_treated_post_lumber(self):
        rows = self._run("espelund_deck")
        by_id = {row["id"]: row for row in rows}
        # "10' 6x6 posts treated" / "12' 6x6 post treated" — size, length, and
        # treatment signals should stack onto the 6x6 PT posts even fuzzy-only.
        self.assertTrue((by_id["L2"]["matched_item_code"] or "").startswith("POST-6X6"))
        self.assertEqual(by_id["L3"]["matched_item_code"], "POST-6X6-PT-12")
        self.assertGreaterEqual(by_id["L3"]["confidence_score"], 0.7)

    def test_oakwood_2x4_blocks_match_2x4_lumber(self):
        rows = self._run("oakwood_deck")
        by_id = {row["id"]: row for row in rows}
        # "2x4x12' blocks" — the NxNxL rewrite + size bonus should land on a
        # 2x4 item, not context-polluted railing hardware.
        self.assertIn("2X4", by_id["L4"]["matched_item_code"] or "")


if __name__ == "__main__":
    unittest.main()
