import sys
from pathlib import Path

# Add the ingestor app to the path
sys.path.append(str(Path(__file__).resolve().parent / "list-ingestor"))

from app.services import ocr_service

def debug_ocr(file_path):
    print(f"DEBUG: Running OCR on {file_path}")
    text = ocr_service.extract_text(file_path)
    print("--- RAW OCR TEXT ---")
    print(text)
    print("--- END ---")

if __name__ == "__main__":
    # Test with one of the images
    img_path = r"C:\Users\amcgrean\python\list-ingest\test files\16108.jpg"
    debug_ocr(img_path)
