"""SKU preprocessing and catalog artifact generation utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Iterable

import pandas as pd

from app.services.size_parser import parse_size_and_length

RAW_REQUIRED_COLUMNS = {
    "item",
    "description",
    "size_",
    "ext_description",
    "major_description",
    "minor_description",
    "keyword_string",
    "keyword_user_defined",
    "last_sold_date",
    "system_id",
}

COLUMN_ALIASES = {
    "item code": "item",
    "item_code": "item",
    "sku": "item",
    "size": "size_",
    "system": "system_id",
    "branch": "system_id",
    "branch_system_id": "system_id",
}


@dataclass
class CatalogValidationError(Exception):
    message: str

    def __str__(self):
        return self.message


def _clean_col(col: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (col or "").strip().lower()).strip("_")


def normalise_input_columns(df: pd.DataFrame) -> pd.DataFrame:
    cols = []
    for col in df.columns:
        cleaned = _clean_col(str(col))
        cols.append(COLUMN_ALIASES.get(cleaned, cleaned))
    out = df.copy()
    out.columns = cols
    return out


def _clean_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _dedupe_tokens(parts: Iterable[str]) -> str:
    seen = set()
    ordered: list[str] = []
    for part in parts:
        for token in re.split(r"\s+", _clean_text(part)):
            if token and token not in seen:
                seen.add(token)
                ordered.append(token)
    return " ".join(ordered)


def validate_raw_columns(df: pd.DataFrame) -> None:
    missing = RAW_REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise CatalogValidationError(
            "Raw SKU file is missing required columns: " + ", ".join(sorted(missing))
        )


# Known composite/decking color names mapped to canonical form.
# Sources: Trex, TimberTech/AZEK distributor pricelists and spec sheets (through Aug 2025).
# Extend this list as new brands/colors are encountered in catalog imports.
# Order matters: more-specific patterns first to avoid partial matches.
_KNOWN_COLORS: list[tuple[re.Pattern, str]] = [
    # ── Trex Transcend ──────────────────────────────────────────────────────
    (re.compile(r"\bcinnamon\s+cove\b", re.I), "Cinnamon Cove"),
    (re.compile(r"\btiki\s+torch\b", re.I), "Tiki Torch"),
    (re.compile(r"\bhavana\s+gold\b", re.I), "Havana Gold"),
    (re.compile(r"\bspiced?\s+rum\b", re.I), "Spiced Rum"),
    (re.compile(r"\blava\s+rock\b", re.I), "Lava Rock"),
    (re.compile(r"\bgravel\s+path\b", re.I), "Gravel Path"),
    (re.compile(r"\bcloudy\s+day\b", re.I), "Cloudy Day"),
    (re.compile(r"\bisland\s+mist\b", re.I), "Island Mist"),
    (re.compile(r"\brope\s+swing\b", re.I), "Rope Swing"),
    (re.compile(r"\brocky\s+harbor\b", re.I), "Rocky Harbor"),
    (re.compile(r"\bvintage\s+lantern\b", re.I), "Vintage Lantern"),
    (re.compile(r"\btoasted\s+sand\b", re.I), "Toasted Sand"),
    (re.compile(r"\bweathered\s+wood\b", re.I), "Weathered Wood"),
    (re.compile(r"\bmoonlight\s+decking\b", re.I), "Moonlight Decking"),
    # ── Trex Select ─────────────────────────────────────────────────────────
    (re.compile(r"\bpebble\s+gr[ae]y\b", re.I), "Pebble Grey"),
    (re.compile(r"\bsaddle\b", re.I), "Saddle"),
    # Winchester Grey also appears in TimberTech PRO Reserve — ambiguous
    (re.compile(r"\bwinchester\s+gr[ae]y\b", re.I), "Winchester Grey"),
    (re.compile(r"\bwoodland\s+brown\b", re.I), "Woodland Brown"),
    # ── Trex Enhance ────────────────────────────────────────────────────────
    (re.compile(r"\bclam\s+shell\b", re.I), "Clam Shell"),
    (re.compile(r"\bbeach\s+dune\b", re.I), "Beach Dune"),
    (re.compile(r"\bfoggy\s+wharf\b", re.I), "Foggy Wharf"),
    (re.compile(r"\bseaside\s+gr[ae]y\b", re.I), "Seaside Grey"),
    (re.compile(r"\btree\s+house\b", re.I), "Tree House"),
    (re.compile(r"\bfire\s+pit\b", re.I), "Fire Pit"),
    (re.compile(r"\btorchlight\b", re.I), "Torchlight"),
    # ── TimberTech PRO Reserve (capped composite) ────────────────────────────
    (re.compile(r"\bantique\s+leather\b", re.I), "Antique Leather"),
    (re.compile(r"\btigerwood\b", re.I), "Tigerwood"),
    (re.compile(r"\bweathered\s+teak\b", re.I), "Weathered Teak"),
    (re.compile(r"\btropical\s+walnut\b", re.I), "Tropical Walnut"),
    (re.compile(r"\bsandy\s+birch\b", re.I), "Sandy Birch"),
    (re.compile(r"\bstormy\s+night\b", re.I), "Stormy Night"),
    (re.compile(r"\brushtic\s+elm\b", re.I), "Rustic Elm"),
    (re.compile(r"\bcanyon\s+dusk\b", re.I), "Canyon Dusk"),
    (re.compile(r"\bcobalt\s+coast\b", re.I), "Cobalt Coast"),
    (re.compile(r"\bdark\s+sienna\b", re.I), "Dark Sienna"),
    # ── TimberTech PRO Legacy ────────────────────────────────────────────────
    (re.compile(r"\bterrain\s+teak\b", re.I), "Terrain Teak"),
    (re.compile(r"\brushtic\s+cedar\b", re.I), "Rustic Cedar"),
    (re.compile(r"\bdriftwood\b", re.I), "Driftwood"),
    # ── TimberTech Edge ──────────────────────────────────────────────────────
    (re.compile(r"\bashwood\b", re.I), "Ashwood"),
    (re.compile(r"\bwhitewood\b", re.I), "Whitewood"),
    # ── Azek / TimberTech AZEK (PVC) ─────────────────────────────────────────
    (re.compile(r"\bslate\s+gr[ae]y\b", re.I), "Slate Grey"),
    (re.compile(r"\bbrownstone\b", re.I), "Brownstone"),
    (re.compile(r"\bwhite\s+oak\b", re.I), "White Oak"),
    (re.compile(r"\bcoastline\b", re.I), "Coastline"),
    (re.compile(r"\bcypress\b", re.I), "Cypress"),
    (re.compile(r"\bsilver\s+maple\b", re.I), "Silver Maple"),
    (re.compile(r"\bkona\b", re.I), "Kona"),
    (re.compile(r"\bhazel\b", re.I), "Hazel"),
    (re.compile(r"\benglish\s+walnut\b", re.I), "English Walnut"),
    (re.compile(r"\bsedona\b", re.I), "Sedona"),
    (re.compile(r"\brushtic\s+autumn\b", re.I), "Rustic Autumn"),
    (re.compile(r"\bcement\s+gr[ae]y\b", re.I), "Cement Grey"),
    (re.compile(r"\bpaver\s+gr[ae]y\b", re.I), "Paver Grey"),
    # ── Shared / generic ─────────────────────────────────────────────────────
    (re.compile(r"\bmocha\b", re.I), "Mocha"),
    (re.compile(r"\bpecan\b", re.I), "Pecan"),
    (re.compile(r"\bkhaki\b", re.I), "Khaki"),
    (re.compile(r"\bmahogany\b", re.I), "Mahogany"),
    (re.compile(r"\bcedar\s+tone\b", re.I), "Cedar Tone"),
    (re.compile(r"\bnatural\s+cedar\b", re.I), "Natural Cedar"),
]


def _extract_color(text: str) -> str:
    """Return the first known color name found in text, or empty string."""
    for pattern, canonical in _KNOWN_COLORS:
        if pattern.search(text):
            return canonical
    return ""


def _sold_bucket(days_since: float | None) -> tuple[str, float]:
    if days_since is None:
        return "unknown", 0.25
    if days_since <= 30:
        return "0_30", 1.0
    if days_since <= 90:
        return "31_90", 0.8
    if days_since <= 180:
        return "91_180", 0.65
    if days_since <= 365:
        return "181_365", 0.45
    return "365_plus", 0.3


def preprocess_raw_catalog(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    now = now or datetime.now(timezone.utc)
    src = normalise_input_columns(df)
    validate_raw_columns(src)

    out = pd.DataFrame()
    out["sku"] = src["item"].map(_clean_text)
    out["branch_system_id"] = src["system_id"].map(_clean_text)
    out["description"] = src["description"].map(_clean_text)
    out["ext_description"] = src["ext_description"].map(_clean_text)
    out["major_description"] = src["major_description"].map(_clean_text)
    out["minor_description"] = src["minor_description"].map(_clean_text)
    out["material_category"] = out["major_description"]

    out["size"] = src["size_"].map(_clean_text)
    inferred_size_len = out.apply(
        lambda row: parse_size_and_length(f"{row['description']} {row['size']}"), axis=1
    )
    out["size"] = [s or row_size for (s, _), row_size in zip(inferred_size_len, out["size"]) ]
    out["length"] = [l or "" for (_, l) in inferred_size_len]

    # Color extraction: scan description + ext_description for known color names
    out["color"] = out.apply(
        lambda row: _extract_color(
            f"{row['description']} {row['ext_description']} {row['minor_description']}"
        ),
        axis=1,
    )

    out["keyword_string"] = src["keyword_string"].map(_clean_text)
    out["keyword_user_defined"] = src["keyword_user_defined"].map(_clean_text)

    out["keywords"] = out.apply(
        lambda row: _dedupe_tokens([
            row["keyword_string"],
            row["keyword_user_defined"],
            row["description"],
            row["ext_description"],
            row["major_description"],
            row["minor_description"],
            row["size"],
            f"{row['length']}ft" if row["length"] else "",
        ]),
        axis=1,
    )

    out["normalized_name"] = out.apply(
        lambda row: _dedupe_tokens([
            row["description"],
            row["size"],
            f"{row['length']} foot" if row["length"] else "",
            row["material_category"],
        ]),
        axis=1,
    )

    out["ai_match_text"] = out.apply(
        lambda row: _dedupe_tokens([
            row["sku"],
            row["description"],
            row["ext_description"],
            row["major_description"],
            row["minor_description"],
            row["size"],
            row["length"],
            f"{row['length']}ft" if row["length"] else "",
            f"{row['length']} foot" if row["length"] else "",
            row.get("color", ""),
            row["keywords"],
        ]),
        axis=1,
    )

    sold_dates = pd.to_datetime(src["last_sold_date"], errors="coerce")
    out["last_sold_date"] = sold_dates.dt.strftime("%Y-%m-%d").fillna("")
    days_since = sold_dates.map(lambda d: (now - d.to_pydatetime().replace(tzinfo=timezone.utc)).days if pd.notna(d) else None)
    out["days_since_last_sold"] = [int(d) if d is not None else None for d in days_since]

    buckets = [_sold_bucket(d) for d in days_since]
    out["sold_recency_bucket"] = [b for b, _ in buckets]
    out["sold_weight"] = [w for _, w in buckets]

    out = out[out["sku"] != ""].drop_duplicates(subset=["sku", "branch_system_id"], keep="first")
    return out.reset_index(drop=True)


def looks_like_raw_file(df: pd.DataFrame) -> bool:
    normalized = set(normalise_input_columns(df).columns)
    return RAW_REQUIRED_COLUMNS.issubset(normalized)


def write_catalog_outputs(processed_df: pd.DataFrame, output_dir: str | Path) -> dict[str, Path]:
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    branches_dir = output_root / "branches"
    branches_dir.mkdir(parents=True, exist_ok=True)

    master_path = output_root / "ai_catalog_master.csv"
    processed_df.to_csv(master_path, index=False)

    branch_paths: dict[str, Path] = {}
    for branch_id, group in processed_df.groupby("branch_system_id"):
        if not str(branch_id).strip():
            continue
        branch_path = branches_dir / f"ai_catalog_system_{branch_id}.csv"
        group.to_csv(branch_path, index=False)
        branch_paths[str(branch_id)] = branch_path

    return {"master": master_path, **{f"branch:{k}": v for k, v in branch_paths.items()}}
