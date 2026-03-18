from flask import Flask, g
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
        # Include server_default so NOT NULL columns can be added to non-empty tables
        if column.server_default is not None:
            default_sql = column.server_default.arg if hasattr(column.server_default, 'arg') else str(column.server_default)
            default_clause = f" DEFAULT {default_sql}"
        else:
            default_clause = ""
        nullable_clause = "" if column.nullable else " NOT NULL"
        db.session.execute(
            text(f"ALTER TABLE {table_name} ADD COLUMN {column.name} {column_type}{default_clause}{nullable_clause}")
        )

    db.session.commit()


def _ensure_default_branches(app):
    from app.models import Branch

    existing = {branch.code: branch for branch in Branch.query.all()}
    changed = False
    for code in app.config["DEFAULT_BRANCH_CODES"]:
        branch = existing.get(code)
        if branch is None:
            db.session.add(Branch(code=code, name=code))
            changed = True
        elif branch.name != code or not branch.is_active:
            branch.name = code
            branch.is_active = True
            changed = True
    if changed:
        db.session.commit()


def _ensure_admin_user(app):
    from app.models import Branch, User

    email = app.config.get("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    if not email:
        return

    default_branch = Branch.query.filter_by(code=app.config["DEFAULT_BRANCH_CODES"][0]).first()
    admin = User.query.filter_by(email=email).first()
    changed = False
    if admin is None:
        admin = User(
            email=email,
            full_name="AMC Grean",
            is_admin=True,
            is_active=True,
            default_branch=default_branch,
        )
        db.session.add(admin)
        changed = True
    else:
        if not admin.is_admin:
            admin.is_admin = True
            changed = True
        if not admin.is_active:
            admin.is_active = True
            changed = True
        if admin.default_branch is None and default_branch is not None:
            admin.default_branch = default_branch
            changed = True

    for user in User.query.filter(User.email != email, User.is_admin.is_(True)).all():
        user.is_admin = False
        changed = True

    if changed:
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
        from app.models import (
            Branch,
            BranchCatalogItem,
            CustomerJobContext,
            ERPItem,
            ExtractedItem,
            IngesterMetrics,
            MatchFeedbackEvent,
            ProcessingSession,
            SessionFeedbackEvent,
            User,
        )

        db.create_all()
        for model in (
            ERPItem,
            IngesterMetrics,
            ProcessingSession,
            ExtractedItem,
            MatchFeedbackEvent,
            SessionFeedbackEvent,
            Branch,
            User,
            BranchCatalogItem,
            CustomerJobContext,
        ):
            _sync_table_columns(model)
        _ensure_default_branches(app)
        _ensure_admin_user(app)

    @app.context_processor
    def inject_globals():
        return {
            "current_user": getattr(g, "current_user", None),
            "current_branch": getattr(g, "current_branch", None),
        }

    return app
