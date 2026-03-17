from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from config import Config
import json
import logging
import os
import sys
from datetime import datetime, timezone

db = SQLAlchemy()


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line.

    Extra context can be attached per-call with the ``extra`` kwarg, e.g.::

        logger.info("ocr_complete", extra={"session_id": 3, "stage": "ocr", "duration_ms": 421})
    """

    _KNOWN_EXTRAS = ("session_id", "stage", "duration_ms", "provider", "items",
                     "error_detail", "ai_provider")

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        obj: dict = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in self._KNOWN_EXTRAS:
            if hasattr(record, key):
                obj[key] = getattr(record, key)
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj)


def _configure_logging() -> None:
    """Replace the root handler with a structured JSON handler on stderr.

    Log level is controlled via the ``LOG_LEVEL`` environment variable
    (default ``INFO``).  Set to ``DEBUG`` in development for verbose output.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    # Remove any handlers added by Flask / Gunicorn before ours
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)

    # Suppress noisy third-party loggers unless we're in DEBUG
    if level > logging.DEBUG:
        for noisy in ("sentence_transformers", "transformers", "faiss", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _sync_table_columns(model):
    """Add newly declared model columns to existing tables when possible."""
    inspector = inspect(db.engine)
    table_name = model.__table__.name
    if table_name not in inspector.get_table_names():
        return

    existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
    for column in model.__table__.columns:
        if column.name in existing_columns:
            continue

        column_type = column.type.compile(dialect=db.engine.dialect)
        nullable_clause = "" if column.nullable else " NOT NULL"
        db.session.execute(
            text(f"ALTER TABLE {table_name} ADD COLUMN {column.name} {column_type}{nullable_clause}")
        )

    db.session.commit()


def create_app(config_class=Config):
    _configure_logging()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(config_class)

    # Ensure required directories exist
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if db_uri.startswith("sqlite:///"):
        os.makedirs(os.path.dirname(db_uri.replace("sqlite:///", "")), exist_ok=True)

    db.init_app(app)

    from app.routes import main
    app.register_blueprint(main)

    with app.app_context():
        from app.models import ERPItem, IngesterMetrics

        db.create_all()
        _sync_table_columns(ERPItem)
        _sync_table_columns(IngesterMetrics)

        # Pre-warm the vector index from the catalog so the first upload request
        # doesn't stall building it from scratch (especially after a cold start).
        _warm_vector_index(app)

    return app


def _warm_vector_index(app) -> None:
    """Load the SKU catalog from DB and build the in-memory vector index.

    Called once at startup so matching works immediately without waiting for
    the first catalog upload or the first user upload request.
    """
    logger = logging.getLogger(__name__)
    try:
        from app.models import ERPItem
        from app.services import item_matcher

        catalog = ERPItem.query.all()
        if not catalog:
            logger.info("startup_index_skip: catalog is empty, skipping vector index warm-up")
            return

        model_name = app.config.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        logger.info("startup_index_build: building vector index for %d catalog items", len(catalog))
        idx = item_matcher.build_index(catalog, model_name)
        if idx and idx.catalog_refs:
            logger.info("startup_index_ready: index built with %d items", len(idx.catalog_refs))
        else:
            logger.warning("startup_index_empty: index built but no refs — sentence-transformers may have failed to load")
    except Exception:
        logger.exception("startup_index_error: failed to warm vector index at startup")
