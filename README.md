# Material List Ingestor

A Python/Flask web application that converts uploaded images or PDFs of contractor material lists into structured ERP-ready order data.

## How It Works

1. **Upload** — Drag and drop a photo (JPG/PNG) or PDF of a material list
2. **OCR** — pytesseract extracts raw text with preprocessing (grayscale, contrast, threshold)
3. **AI Parse** — Claude normalizes the text into structured `{quantity, description}` rows
4. **Match** — Hybrid fuzzy + vector search matches each row to an ERP catalog item
5. **Review** — Edit quantities, swap matched items, confirm/skip rows
6. **Export** — Download as CSV, Excel, or JSON

---

## Local Development Setup

### Prerequisites

- Python 3.12+
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract)
- [Poppler](https://poppler.freedesktop.org/) (for PDF support)

**macOS:**
```bash
brew install tesseract poppler
```

**Ubuntu/Debian:**
```bash
sudo apt-get install tesseract-ocr poppler-utils
```

**Windows:** Use the installers from the links above, then add them to PATH.

### Install & Run

```bash
# 1. Clone
git clone <repo-url>
cd list-ingestor

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY

# 5. Run
python run.py
```

Open http://localhost:5000 in your browser.

The SQLite database is created automatically at `data/app.db` on first run.

---

## Loading the ERP Catalog

1. Navigate to **ERP Catalog** in the top nav
2. Click **Upload & Index** with your CSV/XLSX file
3. Raw ERP exports are validated + preprocessed into AI-ready catalog fields
4. The system computes embeddings for all items immediately

### Raw ERP File Format (recommended)

Upload `stock items to parse.xlsx` (or equivalent export) with required columns:
`item`, `description`, `size_`, `ext_description`, `major_description`, `minor_description`,
`keyword_string`, `keyword_user_defined`, `last_sold_date`, `system_id`.

The app preprocesses this into AI-friendly fields like `ai_match_text`, `normalized_name`,
`keywords`, `days_since_last_sold`, `sold_weight`, and `branch_system_id`.

### Processed Catalog Format (also accepted)

You can still upload a prepared catalog with at minimum:
- `item_code`
- `description`

Optional fields include `keywords`, `material_category`, `size`, `length`, `normalized_name`,
`ai_match_text`, and `branch_system_id`.

An example starter file is included at `example_catalog.csv`.

### Bulk Branch Import

If you already have per-branch processed catalog CSVs, you can batch import them with:

```bash
python import_branch_catalogs.py data/catalog/recovered_pi_20260318/branches --replace-all
```

Expected filenames include `10FD_catalog.csv` or `ai_catalog_system_10FD.csv`.

---

## Docker / Production

### Docker Compose (with PostgreSQL)

```bash
cp .env.example .env
# Set OPENAI_API_KEY, ANTHROPIC_API_KEY, and SECRET_KEY in .env
# If port 8000 is already in use, set APP_PORT=8001

docker compose up --build
```

App runs on `http://localhost:${APP_PORT:-8000}`.

### Deploy to Render

1. Connect your GitHub repo to Render
2. Create a new **Web Service** pointing to the repo
3. Set **Build Command:** `pip install -r requirements.txt`
4. Set **Start Command:** `gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 120 run:app`
5. Add environment variables: `ANTHROPIC_API_KEY`, `DATABASE_URL`, `SECRET_KEY`
6. Add a **PostgreSQL** database and link via `DATABASE_URL`

### Deploy to Fly.io

```bash
fly launch
fly secrets set ANTHROPIC_API_KEY=sk-ant-...
fly secrets set SECRET_KEY=$(openssl rand -hex 32)
fly postgres create
fly postgres attach <pg-app-name>
fly deploy
```

---

## Configuration

All settings are controlled via environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required.** Your Claude API key |
| `DATABASE_URL` | `sqlite:///data/app.db` | SQLAlchemy DB URL |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Claude model to use |
| `FUZZY_WEIGHT` | `0.4` | Weight for fuzzy text matching |
| `VECTOR_WEIGHT` | `0.6` | Weight for vector similarity |
| `CONFIDENCE_THRESHOLD` | `0.45` | Scores below this are flagged low-confidence |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model |

---

## Project Structure

```
list-ingestor/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── models.py            # SQLAlchemy models
│   ├── routes.py            # All HTTP routes
│   ├── services/
│   │   ├── ocr_service.py   # OCR extraction + preprocessing
│   │   ├── ai_parser.py     # Claude AI structuring
│   │   └── item_matcher.py  # Fuzzy + vector matching
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html       # Upload + session history
│   │   ├── review.html      # Review & edit matched items
│   │   └── catalog.html     # ERP catalog management
│   └── static/
│       ├── css/style.css
│       └── js/
│           ├── upload.js
│           └── review.js
├── config.py                # App configuration
├── run.py                   # Entry point
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── example_catalog.csv
```

---

## AI Prompt Design

The Claude prompt (`app/services/ai_parser.py`) instructs the model to:

- Normalize spelling errors and construction shorthand
- Expand abbreviations (lf → linear feet, pt → pressure treated)
- Extract quantities even when written as words ("sixteen" → 16)
- Return strict JSON — no markdown, no commentary
- Default quantity to 1 when not specified

Example input → output:
```
Input:  "2 6x6 posts\n25 2x10 jsts\n1lb lus210 hangers\n100 trex 16 sand"

Output: [
  {"quantity": 2,   "description": "6x6 pressure treated post"},
  {"quantity": 25,  "description": "2x10 SPF joist"},
  {"quantity": 1,   "description": "LUS210 joist hanger 1lb box"},
  {"quantity": 100, "description": "Trex decking 16ft sandy tan"}
]
```


## Matching Architecture

The matcher now combines: vector similarity (FAISS + sentence-transformers), RapidFuzz string similarity, structured size/length extraction, and feedback-based reranking from historical review outcomes. User review overrides are persisted to an `item_aliases` table so repeated contractor phrasing resolves to the corrected SKU before running semantic search. Every review save also appends a `match_feedback_events` record, which is aggregated by normalized description to boost previously corrected/confirmed SKUs.

CSV catalog uploads can include these optional AI-ready columns in addition to `item_code` and `description`: `material_category`, `size`, `length`, `brand`, `keywords`, `normalized_name`.


## SKU Refresh Workflow

See `docs/SKU_REFRESH_WORKFLOW.md` for the full admin workflow and generated artifact layout.
