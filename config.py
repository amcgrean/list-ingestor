import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "uploads"))
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32 MB

    ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "pdf", "webp", "csv"}

    # Database
    DATABASE_URL = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"
    )
    # SQLAlchemy uses postgresql:// but some providers give postgres://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Recycle connections every 5 min to avoid server-side idle timeouts;
    # pre_ping drops stale connections before handing them to the app.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }

    # Anthropic / Claude
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

    # OpenAI / ChatGPT
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
    OPENAI_EXTRACTION_MODEL = os.environ.get("OPENAI_EXTRACTION_MODEL", OPENAI_MODEL)
    OPENAI_CONTEXT_MODEL = os.environ.get("OPENAI_CONTEXT_MODEL", OPENAI_MODEL)

    ENABLE_CONTEXT_PIPELINE = os.environ.get("ENABLE_CONTEXT_PIPELINE", "true").lower() == "true"
    CONTEXT_PIPELINE_FALLBACK_TO_LEGACY = os.environ.get("CONTEXT_PIPELINE_FALLBACK_TO_LEGACY", "true").lower() == "true"
    PARSE_DEBUG_SAVE_JSON = os.environ.get("PARSE_DEBUG_SAVE_JSON", "false").lower() == "true"

    # Authentication / tenancy
    CLOUDFLARE_ACCESS_EMAIL_HEADER = os.environ.get(
        "CLOUDFLARE_ACCESS_EMAIL_HEADER", "Cf-Access-Authenticated-User-Email"
    )
    ALLOW_LOCAL_LOGIN = os.environ.get("ALLOW_LOCAL_LOGIN", "true").lower() == "true"
    DEFAULT_BRANCH_CODES = [
        code.strip()
        for code in os.environ.get("DEFAULT_BRANCH_CODES", "10FD,20GR,25BW,40CV").split(",")
        if code.strip()
    ]
    BOOTSTRAP_ADMIN_EMAIL = os.environ.get(
        "BOOTSTRAP_ADMIN_EMAIL", "amcgrean@beisserlumber.com"
    ).strip().lower()

    # AI provider used for parsing: "claude" or "openai"
    # Can be overridden per-upload via the upload form.
    DEFAULT_AI_PROVIDER = os.environ.get("DEFAULT_AI_PROVIDER", "claude")

    # Matching weights
    FUZZY_WEIGHT = float(os.environ.get("FUZZY_WEIGHT", "0.4"))
    VECTOR_WEIGHT = float(os.environ.get("VECTOR_WEIGHT", "0.6"))
    CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.45"))

    # Sentence-Transformers model (downloaded on first use)
    EMBEDDING_MODEL = os.environ.get(
        "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
    )

    BRANCH_MATCH_FALLBACK_GLOBAL = os.environ.get("BRANCH_MATCH_FALLBACK_GLOBAL", "1") in {"1", "true", "True"}
