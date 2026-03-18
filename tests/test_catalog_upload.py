import io
import os
import tempfile
import unittest
from pathlib import Path

from config import Config


class CatalogUploadTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        class TestConfig(Config):
            TESTING = True
            DATABASE_URL = f"sqlite:///{self.db_path}"
            SQLALCHEMY_DATABASE_URI = DATABASE_URL
            ALLOW_LOCAL_LOGIN = True
            DEFAULT_BRANCH_CODES = ["BR01"]
            BOOTSTRAP_ADMIN_EMAIL = "admin@example.com"
            OPENAI_API_KEY = ""

        from app import create_app, db
        from app.models import Branch

        self.db = db
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()

        with self.app.app_context():
            self.branch = Branch.query.filter_by(code="BR01").first()

        login = self.client.post(
            "/login",
            data={"email": "admin@example.com"},
            follow_redirects=True,
        )
        self.assertEqual(login.status_code, 200)

    def tearDown(self):
        with self.app.app_context():
            self.db.session.remove()
            self.db.engine.dispose()
        Path(self.db_path).unlink(missing_ok=True)

    def test_processed_catalog_upload_persists_items(self):
        from app.models import BranchCatalogItem, ERPItem

        resp = self.client.post(
            "/catalog/upload",
            data={
                "branch_id": str(self.branch.id),
                "replace_all": "1",
                "file": (
                    io.BytesIO(
                        b"item_code,description,keywords,category,unit_of_measure\n"
                        b"SKU1,Treated 2x6 16,treated joist,Lumber,EA\n"
                    ),
                    "catalog.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"catalog refreshed", resp.data.lower())

        with self.app.app_context():
            item = ERPItem.query.filter_by(item_code="SKU1").first()
            self.assertIsNotNone(item)
            self.assertEqual(item.description, "Treated 2x6 16")
            self.assertEqual(
                BranchCatalogItem.query.filter_by(branch_id=self.branch.id).count(),
                1,
            )


if __name__ == "__main__":
    unittest.main()
