import io
import os
import tempfile
import unittest
from pathlib import Path

from config import Config


class UploadContextTests(unittest.TestCase):
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

        self.client.post(
            "/login",
            data={"email": "admin@example.com"},
            follow_redirects=True,
        )

    def tearDown(self):
        with self.app.app_context():
            self.db.session.remove()
            self.db.engine.dispose()
        Path(self.db_path).unlink(missing_ok=True)

    def test_csv_upload_persists_manual_upload_context(self):
        from app.models import ProcessingSession

        response = self.client.post(
            "/upload",
            data={
                "branch_id": str(self.branch.id),
                "upload_context": "Use Trex toasted sand unless the row says otherwise.",
                "files": (
                    io.BytesIO(b"quantity,description\n12,deck boards 12ft\n"),
                    "materials.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)

        with self.app.app_context():
            session = ProcessingSession.query.order_by(ProcessingSession.id.desc()).first()
            self.assertIsNotNone(session)
            self.assertEqual(
                session.upload_context,
                "Use Trex toasted sand unless the row says otherwise.",
            )
            self.assertIn("Upload context:", session.raw_ocr_text)

    def test_review_page_shows_detected_context(self):
        from app import db
        from app.models import ExtractedItem, ProcessingSession

        with self.app.app_context():
            session = ProcessingSession(
                filename="materials.csv",
                file_type="csv",
                branch_id=self.branch.id,
                status="matched",
                upload_context="Customer: Smith patio job",
                extracted_context_json=(
                    '{"summary":"Trex order","customer_name":"Smith","project_name":"Patio","global_material_context":["Trex Toasted Sand"],"job_notes":["Deliver to rear gate"],"warnings":[]}'
                ),
                matched_context_json=(
                    '{"customer_name":"Smith","project_name":"Patio","material_context":"Trex Toasted Sand","job_notes":"Rear gate delivery"}'
                ),
            )
            db.session.add(session)
            db.session.flush()
            db.session.add(
                ExtractedItem(
                    session_id=session.id,
                    quantity=12,
                    raw_description="deck boards 12ft",
                    matched_item_code="SKU1",
                    matched_description="Trex Toasted Sand deck board 12ft",
                    confidence_score=0.9,
                )
            )
            db.session.commit()
            session_id = session.id

        response = self.client.get(f"/review/{session_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Detected Context", response.data)
        self.assertIn(b"Trex Toasted Sand", response.data)


if __name__ == "__main__":
    unittest.main()
