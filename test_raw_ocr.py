import pytesseract
from PIL import Image
import os

def test_raw_ocr(file_path):
    print(f"Testing raw OCR on {file_path}")
    img = Image.open(file_path)
    # No preprocessing
    text = pytesseract.image_to_string(img)
    print("--- RAW TEXT ---")
    print(text)
    print("--- END ---")

if __name__ == "__main__":
    img_path = r"C:\Users\amcgrean\python\list-ingest\test files\16108.jpg"
    test_raw_ocr(img_path)
