import sqlite3
import pandas as pd
import os
import sys
from pathlib import Path

def import_catalog(csv_path):
    print(f"Starting import from {csv_path}...")
    
    # Paths
    base_dir = Path(__file__).resolve().parent
    db_path = base_dir / "list-ingestor" / "data" / "app.db"
    
    # Read CSV
    if not os.path.exists(csv_path):
        print(f"Error: {csv_path} not found.")
        return
        
    df = pd.read_csv(csv_path)
    df.columns = df.columns.str.lower()
    
    # Deduplicate by SKU (item_code)
    # If multiple entries exist for the same SKU, keep the one with the latest sold date if available, 
    # or just the last one in the list.
    print(f"Original row count: {len(df)}")
    if 'sku' in df.columns:
        df = df.drop_duplicates(subset=['sku'], keep='last')
    elif 'item_code' in df.columns:
        df = df.drop_duplicates(subset=['item_code'], keep='last')
    print(f"Deduplicated row count: {len(df)}")
    
    # Connect directly to SQLite
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    print("Clearing erp_items table...")
    cursor.execute("DELETE FROM erp_items")
    
    print(f"Importing {len(df)} rows via sqlite3 executemany...")
    sql = """
        INSERT INTO erp_items 
        (item_code, description, keywords, category, unit_of_measure, branch_system_id, sold_weight, ai_match_text) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    
    rows_to_insert = []
    for _, row in df.iterrows():
        code = str(row.get("sku", row.get("item_code", ""))).strip()
        desc = str(row.get("description", "")).strip()
        if not code or not desc:
            continue
            
        rows_to_insert.append((
            code,
            desc,
            str(row.get("keywords", "")).strip(),
            str(row.get("material_category", row.get("category", ""))).strip(),
            str(row.get("unit_of_measure", "EA")).strip(),
            str(row.get("branch_system_id", "")).strip(),
            float(row.get("sold_weight", 0.25)) if pd.notna(row.get("sold_weight")) else 0.25,
            str(row.get("ai_match_text", "")).strip()
        ))
        
    cursor.executemany(sql, rows_to_insert)
    conn.commit()
    conn.close()
    print(f"Successfully imported {len(rows_to_insert)} unique items via sqlite3.")
    
    # Now run embeddings via SQLAlchemy
    print("Computing embeddings for the new catalog (this will take a minute)...")
    sys.path.append(str(base_dir / "list-ingestor"))
    from app import create_app, db
    from app.models import ERPItem
    from app.services import item_matcher
    
    app = create_app()
    with app.app_context():
        # Force a refresh of the DB session
        db.session.expire_all()
        all_items = ERPItem.query.all()
        print(f"Loaded {len(all_items)} items for embedding.")
        item_matcher.compute_catalog_embeddings(all_items, app.config["EMBEDDING_MODEL"])
        db.session.commit()
    print("Embeddings computed. DONE.")

if __name__ == "__main__":
    import_catalog("erp_items_ai_ready.csv")
