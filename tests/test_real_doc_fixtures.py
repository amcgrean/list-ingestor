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

    def test_espelund_post_base_matches_post_base_hardware(self):
        rows = self._run("espelund_deck")
        by_id = {row["id"]: row for row in rows}
        # "6x6 post base with anchor bolt" should surface AB66 6x6 Post Base.
        self.assertEqual(by_id["L4"]["matched_item_code"], "POSTBASE-AB66")

    def test_oakwood_joists_prefer_lumber_over_hangers(self):
        rows = self._run("oakwood_deck")
        by_id = {row["id"]: row for row in rows}
        # "2x10x12' joists" (green lumber section) must not land on a joist
        # hanger — the structural-member expansion should keep it on lumber.
        self.assertIn("JOIST-2X10", by_id["L1"]["matched_item_code"] or "")
        # "2x10-16 joist hangers" should land on hanger hardware, not lumber.
        self.assertIn("HANGER", by_id["L15"]["matched_item_code"] or "")


if __name__ == "__main__":
    unittest.main()
