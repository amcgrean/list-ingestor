-- =============================================================================
-- List Ingestor — Neon Postgres Schema
-- =============================================================================
-- Run once against your Neon database to create all required tables.
--
-- Usage (psql):
--   psql "$DATABASE_URL" -f database/init.sql
--
-- All statements use IF NOT EXISTS / ON CONFLICT so the script is safe to
-- re-run without data loss.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- skus: canonical SKU catalogue
-- ---------------------------------------------------------------------------
-- Mirrors the fields in app/models.py::ERPItem so that both the Flask app
-- (SQLAlchemy) and the serverless Vercel functions (raw psycopg) work against
-- the same data.  Load this table from your ERP CSV export.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS skus (
    sku               TEXT        PRIMARY KEY,
    description       TEXT        NOT NULL DEFAULT '',
    material_category TEXT        NOT NULL DEFAULT '',
    size              TEXT        NOT NULL DEFAULT '',
    length            TEXT        NOT NULL DEFAULT '',
    brand             TEXT        NOT NULL DEFAULT '',
    keywords          TEXT        NOT NULL DEFAULT '',
    normalized_name   TEXT        NOT NULL DEFAULT ''
);

-- Full-text search index speeds up keyword queries
CREATE INDEX IF NOT EXISTS skus_description_idx
    ON skus USING gin(to_tsvector('english', description));

CREATE INDEX IF NOT EXISTS skus_normalized_name_idx
    ON skus (normalized_name);

CREATE INDEX IF NOT EXISTS skus_keywords_idx
    ON skus (keywords);


-- ---------------------------------------------------------------------------
-- aliases: learned alias → SKU mappings
-- ---------------------------------------------------------------------------
-- Populated automatically when a user overrides a matched SKU during review.
-- The alias text is normalised (lowercase, whitespace-collapsed) before insert
-- so lookups are case- and whitespace-insensitive.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aliases (
    alias       TEXT        PRIMARY KEY,       -- normalised input text
    sku         TEXT        NOT NULL,          -- confirmed SKU code
    usage_count INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS aliases_sku_idx ON aliases (sku);


-- ---------------------------------------------------------------------------
-- match_history: audit trail of every match decision
-- ---------------------------------------------------------------------------
-- Records both the system prediction and the user's final choice so match
-- quality can be analysed over time and used to retrain future heuristics.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS match_history (
    id            SERIAL      PRIMARY KEY,
    input_text    TEXT,                        -- raw description that was matched
    predicted_sku TEXT,                        -- SKU the system predicted
    final_sku     TEXT,                        -- SKU the user confirmed
    corrected     BOOLEAN     NOT NULL DEFAULT FALSE,  -- user overrode prediction
    timestamp     TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS match_history_timestamp_idx ON match_history (timestamp);
CREATE INDEX IF NOT EXISTS match_history_predicted_sku_idx ON match_history (predicted_sku);
CREATE INDEX IF NOT EXISTS match_history_final_sku_idx    ON match_history (final_sku);
