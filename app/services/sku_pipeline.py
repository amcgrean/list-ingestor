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
