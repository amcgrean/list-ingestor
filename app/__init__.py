from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from config import Config
import os

db = SQLAlchemy()


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
        from app.models import ERPItem

        db.create_all()
        _sync_table_columns(ERPItem)

    return app
