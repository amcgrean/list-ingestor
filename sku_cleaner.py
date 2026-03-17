#!/usr/bin/env python3
"""Convert raw ERP SKU export to AI-ready catalog CSV files."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from app.services.sku_pipeline import (
    CatalogValidationError,
    looks_like_raw_file,
    normalise_input_columns,
    preprocess_raw_catalog,
    write_catalog_outputs,
)


def _read_input(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    return pd.read_csv(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_file", help="Raw ERP export (.xlsx/.xls/.csv)")
    parser.add_argument("--output-dir", default="data/catalog", help="Directory to write generated files")
    args = parser.parse_args()

    src_path = Path(args.input_file)
    if not src_path.exists():
        print(f"ERROR: File not found: {src_path}")
        return 1

    try:
        df = _read_input(src_path)
        if not looks_like_raw_file(df):
            df = normalise_input_columns(df)
            raise CatalogValidationError(
                "Input file does not match required raw SKU export structure. "
                "Expected columns include item, description, size_, ext_description, "
                "major_description, minor_description, keyword_string, "
                "keyword_user_defined, last_sold_date, system_id."
            )

        processed = preprocess_raw_catalog(df)
        outputs = write_catalog_outputs(processed, args.output_dir)

    except CatalogValidationError as exc:
        print(f"ERROR: {exc}")
        return 2

    print(f"Processed {len(processed)} SKUs")
    print(f"Master catalog: {outputs['master']}")
    branch_keys = [k for k in outputs if k.startswith('branch:')]
    print(f"Branch catalogs: {len(branch_keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
