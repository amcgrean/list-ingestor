import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "uploads"))
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32 MB

    ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "pdf"}

    # Database
    DATABASE_URL = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"
    )
    # SQLAlchemy uses postgresql:// but some providers give postgres://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Anthropic / Claude
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

    # Matching weights
    FUZZY_WEIGHT = float(os.environ.get("FUZZY_WEIGHT", "0.4"))
    VECTOR_WEIGHT = float(os.environ.get("VECTOR_WEIGHT", "0.6"))
    CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.45"))

    # Sentence-Transformers model (downloaded on first use)
    EMBEDDING_MODEL = os.environ.get(
        "EMBEDDING_MODEL", "all-MiniLM-L6-v2"
    )
