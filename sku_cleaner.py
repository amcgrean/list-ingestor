import pandas as pd
import re
from datetime import datetime

INPUT_FILE = "stock items to parse.xlsx"
OUTPUT_FILE = "erp_items_ai_ready.csv"

TODAY = pd.Timestamp.today().normalize()


def normalize(text):
    if pd.isna(text):
        return ""
    text = str(text).lower()
    text = re.sub(r"[^\w\s'x.-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip()


def extract_size(text):
    """
    Pulls common lumber/item sizing patterns like:
    1x4, 2x6, 4x4, 1-1/4x8, etc.
    """
    if not text:
        return ""

    text = str(text).lower()

    patterns = [
        r"\b\d+(?:-\d+/\d+)?x\d+(?:-\d+/\d+)?\b",   # 1x4 / 1-1/4x8
        r"\b\d+\s*x\s*\d+\b"
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return re.sub(r"\s+", "", m.group(0))

    return ""


def extract_length(text):
    """
    Pulls common length patterns:
    08', 10', 12', 16ft, 20 foot, etc.
    Returns a normalized numeric string where possible.
    """
    if not text:
        return ""

    text = str(text).lower()

    patterns = [
        r"\b(\d{1,2})\s*'\b",
        r"\b(\d{1,2})\s*ft\b",
        r"\b(\d{1,2})\s*foot\b",
        r"\b(\d{2})\b"
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).lstrip("0") or "0"

    return ""


def detect_category(row):
    """
    Uses multiple columns, not just description.
    """
    text = " ".join([
        clean_text(row.get("description")),
        clean_text(row.get("ext_description")),
        clean_text(row.get("major_description")),
        clean_text(row.get("minor_description")),
        clean_text(row.get("keyword_string")),
        clean_text(row.get("keyword_user_defined")),
    ]).lower()

    if any(x in text for x in ["joist hanger", "simpson", "bracket", "connector"]):
        return "hardware"

    if any(x in text for x in ["screw", "bolt", "nut", "nail", "fastener", "lag"]):
        return "fastener"

    if any(x in text for x in ["deck", "decking", "composite", "trex"]):
        return "decking"

    if any(x in text for x in ["rail", "baluster", "railing"]):
        return "railing"

    if any(x in text for x in ["post", "4x4", "6x6"]):
        return "post"

    if any(x in text for x in ["2x", "joist", "stud"]):
        return "framing"

    if any(x in text for x in ["trim", "fascia"]):
        return "trim"

    return "general_building_material"


def recency_weight(last_sold_date):
    """
    Higher score = more likely match candidate.
    Tweak these ranges later as needed.
    """
    if pd.isna(last_sold_date):
        return 0.20

    days_old = (TODAY - pd.to_datetime(last_sold_date).normalize()).days

    if days_old <= 30:
        return 1.00
    elif days_old <= 90:
        return 0.85
    elif days_old <= 180:
        return 0.65
    elif days_old <= 365:
        return 0.45
    else:
        return 0.25


def recency_bucket(last_sold_date):
    if pd.isna(last_sold_date):
        return "unknown"

    days_old = (TODAY - pd.to_datetime(last_sold_date).normalize()).days

    if days_old <= 30:
        return "0_30_days"
    elif days_old <= 90:
        return "31_90_days"
    elif days_old <= 180:
        return "91_180_days"
    elif days_old <= 365:
        return "181_365_days"
    else:
        return "365_plus_days"


def build_search_text(parts):
    """
    Combines fields into one AI-friendly text blob.
    This is useful for OCR/vision match pipelines and embeddings.
    """
    cleaned = [normalize(p) for p in parts if clean_text(p)]
    return " | ".join([p for p in cleaned if p])


def process_row(row):
    sku = clean_text(row.get("item"))
    description = clean_text(row.get("description"))
    ext_description = clean_text(row.get("ext_description"))
    major_description = clean_text(row.get("major_description"))
    minor_description = clean_text(row.get("minor_description"))
    keyword_string = clean_text(row.get("keyword_string"))
    keyword_user_defined = clean_text(row.get("keyword_user_defined"))
    system_id = clean_text(row.get("system_id")).upper()

    size = clean_text(row.get("size_"))
    if not size:
        size = extract_size(" ".join([description, ext_description, keyword_string]))

    length = extract_length(" ".join([description, ext_description, keyword_string]))
    category = detect_category(row)

    last_sold_date = pd.to_datetime(row.get("last_sold_date"), errors="coerce")
    sold_weight = recency_weight(last_sold_date)
    sold_bucket = recency_bucket(last_sold_date)

    # Good for exact-ish normalized comparisons
    normalized_name = normalize(
        f"{system_id} {category} {size} {length} {major_description} {minor_description}"
    )

    # Good for broader OCR / AI / semantic matching
    keywords = build_search_text([
        sku,
        description,
        ext_description,
        size,
        major_description,
        minor_description,
        keyword_string,
        keyword_user_defined,
        category,
        system_id
    ])

    # Stronger text payload for embeddings or candidate ranking
    ai_match_text = build_search_text([
        f"sku {sku}",
        f"branch {system_id}",
        description,
        ext_description,
        f"size {size}",
        f"length {length}",
        f"category {category}",
        f"major {major_description}",
        f"minor {minor_description}",
        keyword_string,
        keyword_user_defined,
        f"recency {sold_bucket}"
    ])

    return {
        "sku": sku,
        "branch_system_id": system_id,
        "description": description,
        "ext_description": ext_description,
        "major_description": major_description,
        "minor_description": minor_description,
        "material_category": category,
        "size": size,
        "length": length,
        "keyword_string": keyword_string,
        "keyword_user_defined": keyword_user_defined,
        "keywords": keywords,
        "normalized_name": normalized_name,
        "ai_match_text": ai_match_text,
        "last_sold_date": last_sold_date.date().isoformat() if not pd.isna(last_sold_date) else "",
        "days_since_last_sold": (TODAY - last_sold_date.normalize()).days if not pd.isna(last_sold_date) else "",
        "sold_recency_bucket": sold_bucket,
        "sold_weight": sold_weight
    }


def main():
    df = pd.read_excel(INPUT_FILE)

    # Normalize columns just in case the workbook changes slightly
    df.columns = [str(c).strip() for c in df.columns]

    required_columns = ["item", "description", "last_sold_date", "system_id"]
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    rows = []

    for _, row in df.iterrows():
        if pd.isna(row.get("item")):
            continue

        rows.append(process_row(row))

    out = pd.DataFrame(rows)

    # Optional: dedupe exact duplicates per branch/SKU if they exist
    out = out.drop_duplicates(subset=["branch_system_id", "sku"])

    # Sort so most recently sold / highest weighted items are first within each branch
    out = out.sort_values(
        by=["branch_system_id", "sold_weight", "days_since_last_sold"],
        ascending=[True, False, True]
    )

    out.to_csv(OUTPUT_FILE, index=False)
    
    print(f"AI catalog created: {OUTPUT_FILE}")
    print(f"Rows exported: {len(out)}")
    
    # Export branch-specific catalog files
    branches = out['branch_system_id'].unique()
    print(f"Branches found: {len(branches)}")
    
    for branch in branches:
        if not branch:
            continue
        branch_file = f"erp_items_{branch.lower()}.csv"
        branch_out = out[out['branch_system_id'] == branch]
        branch_out.to_csv(branch_file, index=False)
        print(f" - Created {branch_file} ({len(branch_out)} items)")


if __name__ == "__main__":
    main()