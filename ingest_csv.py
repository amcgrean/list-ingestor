import os
import sys
import pprint

# Ensure the app imports work
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from app import create_app, db
from app.models import ERPItem

csv_path = r"C:\Users\amcgrean\python\list-ingest\erp_items_ai_ready.csv"

app = create_app()

with app.app_context():
    print(f"Uploading file: {csv_path}")
    
    # We can use the Flask test client to simulate the upload properly
    # which will then hit the route we modified.
    client = app.test_client()
    
    with open(csv_path, 'rb') as f:
        data = {
            'file': (f, 'erp_items_ai_ready.csv'),
            'replace_all': '1'
        }
        res = client.post('/catalog/upload', data=data, content_type='multipart/form-data')
        
    print("Response Status:", res.status_code)
    print("Checking item count...")
    count = ERPItem.query.count()
    print(f"ERPItem Count in Database: {count}")

