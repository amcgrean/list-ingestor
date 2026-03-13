import sys
from pathlib import Path
from dotenv import load_dotenv
import os

# Add the ingestor app to the path
sys.path.append(str(Path(__file__).resolve().parent / "list-ingestor"))

from app import create_app
from app.services import ai_parser

def debug_ai(ocr_text):
    load_dotenv(Path(__file__).resolve().parent / "list-ingestor" / ".env")
    app = create_app()
    with app.app_context():
        provider = app.config.get("DEFAULT_AI_PROVIDER", "openai").strip('"').strip("'")
        api_key = app.config.get("OPENAI_API_KEY") if provider == "openai" else app.config.get("ANTHROPIC_API_KEY")
        model = app.config.get("OPENAI_MODEL") if provider == "openai" else app.config.get("CLAUDE_MODEL")
        
        print(f"DEBUG: Provider={provider}, Model={model}")
        
        extracted_items = ai_parser.parse_material_list(ocr_text, provider=provider, api_key=api_key, model=model)
        print("--- EXTRACTED ITEMS ---")
        print(extracted_items)
        print("--- END ---")

if __name__ == "__main__":
    with open("screenshot_ocr.txt", "r") as f:
        text = f.read()
    debug_ai(text)
