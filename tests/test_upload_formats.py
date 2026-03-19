import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import FileStorage

from config import Config


class UploadFormatTests(unittest.TestCase):
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

        self.db = db
        self.app = create_app(TestConfig)

    def tearDown(self):
        with self.app.app_context():
            self.db.session.remove()
            self.db.engine.dispose()
        Path(self.db_path).unlink(missing_ok=True)

    def test_allowed_extensions_include_heic(self):
        self.assertIn("heic", self.app.config["ALLOWED_EXTENSIONS"])
        self.assertIn("heif", self.app.config["ALLOWED_EXTENSIONS"])

    def test_save_upload_converts_heic_to_jpg(self):
        from app.routes import save_upload

        class FakeImage:
            def convert(self, _mode):
                return self

            def save(self, path, format, quality):
                Path(path).write_bytes(b"jpeg-data")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        upload = FileStorage(
            stream=io.BytesIO(b"heic-data"),
            filename="phone-photo.heic",
            content_type="image/heic",
        )

        with self.app.app_context(), patch("app.routes.Image.open", return_value=FakeImage()):
            saved_path = save_upload(upload)

        try:
            self.assertEqual(saved_path.suffix.lower(), ".jpg")
            self.assertTrue(saved_path.exists())
        finally:
            saved_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
