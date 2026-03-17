# SKU Refresh Workflow

## Base source file (required)
The raw SKU export is the source of truth for catalog refreshes. Upload:

- `stock items to parse.xlsx` (preferred), or
- equivalent `.xlsx/.xls/.csv` export with the required columns.

### Required raw columns
- `item`
- `description`
- `size_`
- `ext_description`
- `major_description`
- `minor_description`
- `keyword_string`
- `keyword_user_defined`
- `last_sold_date`
- `system_id`

The pipeline normalizes minor naming differences (for example `SKU` -> `item`, `branch` -> `system_id`).

## What preprocessing does
`sku_cleaner.py` and the app upload flow both run the same preprocessing module:

1. Validate required columns.
2. Normalize/clean text.
3. Build match-friendly fields (`keywords`, `normalized_name`, `ai_match_text`).
4. Parse size/length from descriptions.
5. Derive sales-recency fields from `last_sold_date`:
   - `days_since_last_sold`
   - `sold_recency_bucket`
   - `sold_weight`
6. Preserve branch context in `branch_system_id` from `system_id`.

## Files generated
Generated under `data/catalog/`:

- `ai_catalog_master.csv` (all processed SKUs)
- `branches/ai_catalog_system_<system_id>.csv` (one file per branch)

## How the app uses generated data
- SQL catalog (`erp_items`) is refreshed from the processed data.
- Embedding index is rebuilt from `ai_match_text` + core fields.
- During matching, the app checks the user/request `system_id` branch catalog first, then optionally falls back to the global catalog.

## Refresh options
### In-app (recommended)
1. Open **ERP Catalog**.
2. Upload raw ERP export or already-processed catalog file.
3. Optional: check **Replace entire catalog**.
4. App validates, preprocesses (if raw), stores catalog, regenerates files, and rebuilds vector index.

### CLI
```bash
python sku_cleaner.py "stock items to parse.xlsx" --output-dir data/catalog
```

This generates the same master + branch CSV outputs for review or automation.
