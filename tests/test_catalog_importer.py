import tempfile
import unittest
from pathlib import Path

import pandas as pd

from config import Config


class CatalogImporterTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        Path(self.db_path).touch(exist_ok=True)

        class TestConfig(Config):
            TESTING = True
            DATABASE_URL = f"sqlite:///{self.db_path}"
            SQLALCHEMY_DATABASE_URI = DATABASE_URL
            DEFAULT_BRANCH_CODES = ["10FD", "20GR"]
            BOOTSTRAP_ADMIN_EMAIL = "admin@example.com"
            OPENAI_API_KEY = ""

        from app import create_app, db

        self.db = db
        self.app = create_app(TestConfig)

    def tearDown(self):
        with self.app.app_context():
            self.db.session.remove()
            self.db.engine.dispose()
        try:
            Path(self.db_path).unlink(missing_ok=True)
        except PermissionError:
            pass

    def test_branch_import_links_shared_items_across_branches(self):
        from app.models import Branch, BranchCatalogItem, ERPItem
        from app.services.catalog_importer import (
            import_catalog_dataframe,
            prepare_catalog_dataframe,
        )

        with self.app.app_context():
            branch_10 = Branch.query.filter_by(code="10FD").first()
            branch_20 = Branch.query.filter_by(code="20GR").first()

            df_10 = prepare_catalog_dataframe(
                pd.DataFrame(
                    [
                        {
                            "item_code": "SKU1",
                            "description": "Shared Item",
                            "keywords": "shared",
                        },
                        {
                            "item_code": "SKU2",
                            "description": "Ten Only",
                            "keywords": "ten",
                        },
                    ]
                )
            )
            df_20 = prepare_catalog_dataframe(
                pd.DataFrame(
                    [
                        {
                            "item_code": "SKU1",
                            "description": "Shared Item",
                            "keywords": "shared",
                        },
                        {
                            "item_code": "SKU3",
                            "description": "Twenty Only",
                            "keywords": "twenty",
                        },
                    ]
                )
            )

            import_catalog_dataframe(
                branch=branch_10,
                df=df_10,
                replace_all=True,
                embedding_model=self.app.config["EMBEDDING_MODEL"],
                output_dir=Path(self.app.root_path).parent / "data" / "catalog",
            )
            import_catalog_dataframe(
                branch=branch_20,
                df=df_20,
                replace_all=True,
                embedding_model=self.app.config["EMBEDDING_MODEL"],
                output_dir=Path(self.app.root_path).parent / "data" / "catalog",
            )

            self.assertEqual(ERPItem.query.count(), 3)
            self.assertEqual(
                BranchCatalogItem.query.filter_by(branch_id=branch_10.id).count(),
                2,
            )
            self.assertEqual(
                BranchCatalogItem.query.filter_by(branch_id=branch_20.id).count(),
                2,
            )
            shared = ERPItem.query.filter_by(item_code="SKU1").first()
            self.assertEqual(len(shared.branch_links), 2)
