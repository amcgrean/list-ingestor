import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent / "list-ingestor"))

from app import create_app, db
from app.models import ERPItem
from sqlalchemy import inspect

def verify():
    app = create_app()
    with app.app_context():
        print(f"ERPItem table name: {ERPItem.__tablename__}")
        print(f"Columns in class: {[c.name for c in ERPItem.__table__.columns]}")
        
        mapper = inspect(ERPItem)
        print(f"Mapped attributes: {[a.key for a in mapper.attrs]}")
        
        # Try to instantiate
        try:
            item = ERPItem()
            item.branch_system_id = "test"
            print("Successfully set branch_system_id on instance.")
        except AttributeError as e:
            print(f"FAILED to set branch_system_id on instance: {e}")

if __name__ == "__main__":
    verify()
