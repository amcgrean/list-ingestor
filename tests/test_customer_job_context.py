import os
import tempfile
import unittest
from pathlib import Path

from config import Config


class CustomerJobContextTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        class TestConfig(Config):
            TESTING = True
            DATABASE_URL = f"sqlite:///{self.db_path}"
            SQLALCHEMY_DATABASE_URI = DATABASE_URL
            ALLOW_LOCAL_LOGIN = True
            DEFAULT_BRANCH_CODES = ["10FD"]
            BOOTSTRAP_ADMIN_EMAIL = "admin@example.com"

        from app import create_app, db

        self.db = db
        self.app = create_app(TestConfig)

    def tearDown(self):
        with self.app.app_context():
            self.db.session.remove()
            self.db.engine.dispose()
        Path(self.db_path).unlink(missing_ok=True)

    def test_matches_customer_context_from_upload_signals(self):
        from app import db
        from app.models import CustomerJobContext
        from app.services.customer_job_context import match_customer_job_context

        with self.app.app_context():
            db.session.add(
                CustomerJobContext(
                    source_system="cloud",
                    external_id="job-1",
                    branch_code="10FD",
                    customer_name="Smith",
                    project_name="Patio Refresh",
                    aliases_json='["smith patio"]',
                    material_context="Trex Toasted Sand",
                    job_notes="Rear gate delivery",
                    is_active=True,
                )
            )
            db.session.commit()

            match = match_customer_job_context(
                customer_name="Smith",
                project_name="Patio Refresh",
                upload_context="Customer smith patio needs toasted sand boards",
                branch_code="10FD",
            )

            self.assertIsNotNone(match)
            self.assertEqual(match.context.external_id, "job-1")
            self.assertEqual(match.context.material_context, "Trex Toasted Sand")


if __name__ == "__main__":
    unittest.main()
