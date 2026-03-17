import unittest

import pandas as pd

from app.services.sku_pipeline import CatalogValidationError, preprocess_raw_catalog


class SkuPipelineTests(unittest.TestCase):
    def test_preprocess_raw_catalog_builds_ai_fields(self):
        df = pd.DataFrame([
            {
                "item": "ABC123",
                "description": "2x6 PT 16",
                "size_": "",
                "ext_description": "treated lumber",
                "major_description": "lumber",
                "minor_description": "dimensional",
                "keyword_string": "2x6 treated",
                "keyword_user_defined": "pt board",
                "last_sold_date": "2026-01-15",
                "system_id": "BR1",
            }
        ])

        out = preprocess_raw_catalog(df)
        self.assertEqual(out.iloc[0]["sku"], "abc123")
        self.assertEqual(out.iloc[0]["branch_system_id"], "br1")
        self.assertIn("2x6", out.iloc[0]["ai_match_text"])
        self.assertTrue(out.iloc[0]["sold_weight"] > 0)

    def test_preprocess_requires_columns(self):
        df = pd.DataFrame([{"item": "A", "description": "x"}])
        with self.assertRaises(CatalogValidationError):
            preprocess_raw_catalog(df)


if __name__ == "__main__":
    unittest.main()
