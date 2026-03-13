import os
import sys
import json
import logging
from pathlib import Path
from dotenv import load_dotenv

# Add the ingestor app to the path
sys.path.append(str(Path(__file__).resolve().parent / "list-ingestor"))

from app import create_app, db
from app.models import ERPItem, ProcessingSession, ExtractedItem
from app.services import ocr_service, ai_parser, item_matcher

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_batch")

def run_test_batch(test_dir_path):
    load_dotenv(Path(__file__).resolve().parent / "list-ingestor" / ".env")
    
    app = create_app()
    with app.app_context():
        test_dir = Path(test_dir_path)
        files = list(test_dir.glob("*"))
        
        results = []
        
        for file_path in files:
            ext = file_path.suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".pdf", ".bmp", ".tiff", ".webp"}:
                logger.warning(f"Skipping unsupported file: {file_path.name}")
                continue
            
            logger.info(f"Processing {file_path.name}...")
            
            try:
                # 1. OCR
                ocr_text = ocr_service.extract_text(file_path)
                if not ocr_text:
                    logger.warning(f"No text extracted from {file_path.name}")
                    continue
                
                # 2. AI Parse
                provider = app.config.get("DEFAULT_AI_PROVIDER", "openai").strip('"').strip("'")
                api_key = app.config.get("OPENAI_API_KEY") if provider == "openai" else app.config.get("ANTHROPIC_API_KEY")
                model = app.config.get("OPENAI_MODEL") if provider == "openai" else app.config.get("CLAUDE_MODEL")
                
                if provider == "openai":
                    from app.services import chatgpt_parser as parser
                else:
                    from app.services import ai_parser as parser
                    
                extracted_items = parser.parse_material_list(ocr_text, api_key=api_key, model=model)
                
                # 3. Match
                erp_items = ERPItem.query.all()
                model_name = app.config["EMBEDDING_MODEL"]
                
                # No branch context for batch test (global search)
                match_results = item_matcher.match_items_batch(
                    [it["description"] for it in extracted_items],
                    erp_items,
                    model_name
                )
                
                # Collect results
                for it, match in zip(extracted_items, match_results):
                    results.append({
                        "file": file_path.name,
                        "raw_qty": it["quantity"],
                        "raw_desc": it["description"],
                        "matched_code": match["matched_item_code"],
                        "matched_desc": match["matched_description"],
                        "confidence": match["confidence_score"]
                    })
                    
            except Exception as e:
                logger.error(f"Failed to process {file_path.name}: {e}")
                
        # Save results to CSV
        import pandas as pd
        results_df = pd.DataFrame(results)
        output_path = "test_results.csv"
        results_df.to_csv(output_path, index=False)
        print(f"\nBatch test complete. Results saved to {output_path}")
        print(f"Total items processed: {len(results)}")
        if len(results) > 0:
            avg_conf = results_df["confidence"].mean()
            print(f"Average Confidence Score: {avg_conf:.4f}")

if __name__ == "__main__":
    test_folder = r"C:\Users\amcgrean\python\list-ingest\test files"
    run_test_batch(test_folder)
