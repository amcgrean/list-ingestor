#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from app import create_app, db
from app.models import Branch
from app.services.catalog_importer import (
    import_catalog_dataframe,
    prepare_catalog_dataframe,
)


def _branch_code_from_path(path: Path) -> str:
    name = path.stem
    if name.endswith("_catalog"):
        return name[: -len("_catalog")]
    if name.startswith("ai_catalog_system_"):
        return name[len("ai_catalog_system_") :]
    raise ValueError(f"Could not infer branch code from filename: {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import branch-level processed catalog CSV files into the app database."
    )
    parser.add_argument("input_dir", help="Directory containing branch CSV files")
    parser.add_argument(
        "--replace-all",
        action="store_true",
        help="Replace each branch catalog before importing",
    )
    parser.add_argument(
        "--glob",
        default="*.csv",
        help="Glob for branch catalog files",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        print(f"ERROR: Input directory not found: {input_dir}")
        return 1

    files = sorted(input_dir.glob(args.glob))
    if not files:
        print(f"ERROR: No files matched {args.glob} in {input_dir}")
        return 2

    app = create_app()
    with app.app_context():
        for file_path in files:
            branch_code = _branch_code_from_path(file_path)
            branch = Branch.query.filter_by(code=branch_code).first()
            if not branch:
                print(f"SKIP {file_path.name}: no branch with code {branch_code}")
                continue

            incoming = pd.read_csv(file_path)
            df = prepare_catalog_dataframe(incoming)
            summary = import_catalog_dataframe(
                branch=branch,
                df=df,
                replace_all=args.replace_all,
                embedding_model=app.config["EMBEDDING_MODEL"],
                output_dir=Path(app.root_path).parent / "data" / "catalog",
            )
            print(
                f"{branch.code}: added={summary['added']} updated={summary['updated']} "
                f"linked={summary['linked']} total={summary['catalog_count']}"
            )
        db.session.remove()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
