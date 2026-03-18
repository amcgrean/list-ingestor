from __future__ import annotations

from pathlib import Path

import pandas as pd

from app import db
from app.models import BranchCatalogItem, ERPItem
from app.services import item_matcher
from app.services.sku_pipeline import (
    CatalogValidationError,
    looks_like_raw_file,
    normalise_input_columns,
    preprocess_raw_catalog,
    write_catalog_outputs,
)


def clean_csv_value(value, max_length: int | None = None) -> str:
    if value is None or pd.isna(value):
        return ""
    cleaned = str(value).strip()
    if cleaned.lower() == "nan":
        cleaned = ""
    if max_length is not None:
        cleaned = cleaned[:max_length]
    return cleaned


def prepare_catalog_dataframe(incoming: pd.DataFrame) -> pd.DataFrame:
    normalized_incoming = normalise_input_columns(incoming)
    if looks_like_raw_file(incoming):
        df = preprocess_raw_catalog(incoming).rename(columns={"sku": "item_code"})
    else:
        df = normalized_incoming
        if "item" in df.columns and "item_code" not in df.columns:
            df = df.rename(columns={"item": "item_code"})

    required_cols = {"item_code", "description"}
    missing = required_cols - set(df.columns)
    if missing:
        raise CatalogValidationError(
            f"Catalog is missing required columns: {', '.join(sorted(missing))}"
        )
    return df


def prune_orphan_erp_items() -> None:
    orphans = (
        ERPItem.query.outerjoin(
            BranchCatalogItem, BranchCatalogItem.erp_item_id == ERPItem.id
        )
        .filter(BranchCatalogItem.id.is_(None))
        .all()
    )
    for item in orphans:
        db.session.delete(item)
    if orphans:
        db.session.commit()


def export_catalog_artifacts(output_dir: str | Path) -> None:
    rows = [item.to_dict() for item in ERPItem.query.order_by(ERPItem.item_code).all()]
    if not rows:
        return
    df = pd.DataFrame(rows)
    if "item_code" in df.columns and "sku" not in df.columns:
        df["sku"] = df["item_code"]
    if "ai_match_text" not in df.columns:
        df["ai_match_text"] = ""
    write_catalog_outputs(df, output_dir)


def import_catalog_dataframe(
    *,
    branch,
    df: pd.DataFrame,
    replace_all: bool,
    embedding_model: str,
    output_dir: str | Path,
    chunk_size: int = 500,
) -> dict[str, int]:
    if replace_all:
        BranchCatalogItem.query.filter_by(branch_id=branch.id).delete()
        db.session.commit()
        prune_orphan_erp_items()

    valid_rows = []
    for _, row in df.iterrows():
        code = clean_csv_value(row["item_code"], 100)
        desc = clean_csv_value(row["description"], 500)
        if code and desc:
            valid_rows.append((code, row))

    all_codes = sorted({code for code, _ in valid_rows})
    existing_map: dict[str, ERPItem] = {}
    if all_codes:
        existing_map = {
            item.item_code: item
            for item in ERPItem.query.filter(ERPItem.item_code.in_(all_codes)).all()
        }

    existing_links = {
        link.erp_item_id
        for link in BranchCatalogItem.query.filter_by(branch_id=branch.id).all()
    }

    added = 0
    updated = 0
    linked = 0

    for i, (code, row) in enumerate(valid_rows):
        existing = existing_map.get(code)
        payload = {
            "description": clean_csv_value(row.get("description", ""), 500),
            "keywords": clean_csv_value(row.get("keywords", ""), None),
            "category": clean_csv_value(
                row.get("category", row.get("major_description", "")), 100
            ),
            "material_category": clean_csv_value(
                row.get("material_category", row.get("major_description", "")), 100
            ),
            "size": clean_csv_value(row.get("size", ""), 50),
            "length": clean_csv_value(row.get("length", ""), 20),
            "brand": clean_csv_value(row.get("brand", ""), 150),
            "normalized_name": clean_csv_value(row.get("normalized_name", ""), 255),
            "unit_of_measure": clean_csv_value(row.get("unit_of_measure", "EA"), 50)
            or "EA",
            "branch_system_id": clean_csv_value(
                row.get("branch_system_id", row.get("system_id", branch.code)), 100
            ),
            "ext_description": clean_csv_value(row.get("ext_description", ""), 500),
            "major_description": clean_csv_value(
                row.get("major_description", ""), 255
            ),
            "minor_description": clean_csv_value(
                row.get("minor_description", ""), 255
            ),
            "keyword_user_defined": clean_csv_value(
                row.get("keyword_user_defined", ""), None
            ),
            "ai_match_text": clean_csv_value(row.get("ai_match_text", ""), None),
            "last_sold_date": clean_csv_value(row.get("last_sold_date", ""), 20),
            "days_since_last_sold": row.get("days_since_last_sold")
            if pd.notna(row.get("days_since_last_sold"))
            else None,
            "sold_recency_bucket": clean_csv_value(
                row.get("sold_recency_bucket", "unknown"), 50
            )
            or "unknown",
            "sold_weight": float(row.get("sold_weight", 0.25) or 0.25),
        }

        if existing:
            for key, val in payload.items():
                setattr(existing, key, val)
            existing.embedding = None
            updated += 1
        else:
            item = ERPItem(item_code=code, **payload)
            db.session.add(item)
            db.session.flush()
            existing_map[code] = item
            added += 1
            existing = item

        if existing.id not in existing_links:
            db.session.add(
                BranchCatalogItem(branch_id=branch.id, erp_item_id=existing.id)
            )
            existing_links.add(existing.id)
            linked += 1

        if (i + 1) % chunk_size == 0:
            db.session.flush()

    db.session.commit()

    branch_items = (
        ERPItem.query.join(
            BranchCatalogItem, BranchCatalogItem.erp_item_id == ERPItem.id
        )
        .filter(BranchCatalogItem.branch_id == branch.id)
        .order_by(ERPItem.item_code)
        .all()
    )

    export_catalog_artifacts(output_dir)
    idx = item_matcher.build_index(
        branch_items,
        embedding_model,
        cache_key=f"branch:{branch.id}",
    )

    return {
        "added": added,
        "updated": updated,
        "linked": linked,
        "catalog_count": len(branch_items),
        "vector_count": len(idx.catalog_refs) if idx is not None else 0,
    }
