#!/usr/bin/env python3
"""Convert an Agility stocked-items export into the worker's flat catalog file.

Accepts .xlsx or .csv with columns: item, description, ext_description, size_,
stocking_uom (extra columns ignored). Writes src/catalog.json, which is bundled
into the worker at deploy time — the catalog is never served publicly.

Usage:
    python scripts/build_catalog.py items_stocking_uom.xlsx [--branch 10FD]
Then redeploy:
    npx wrangler deploy
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_file", type=Path)
    parser.add_argument("--branch", default="10FD")
    parser.add_argument("--out", type=Path, default=HERE / "src" / "catalog.json")
    args = parser.parse_args()

    if args.export_file.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        df = pd.read_excel(args.export_file)
    else:
        df = pd.read_csv(args.export_file)
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.fillna("")

    required = {"item", "description"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"Export is missing columns: {', '.join(sorted(missing))}")

    items = []
    for _, row in df.iterrows():
        sku = str(row["item"]).strip()
        desc = str(row["description"]).strip()
        if not sku or not desc:
            continue
        items.append(
            {
                "sku": sku,
                "description": desc,
                "ext": str(row.get("ext_description", "")).strip(),
                "size": str(row.get("size_", row.get("size", ""))).strip(),
                "uom": str(row.get("stocking_uom", row.get("unit_of_measure", ""))).strip(),
            }
        )

    out = {
        "branch": args.branch,
        "source": args.export_file.name,
        "count": len(items),
        "items": items,
    }
    args.out.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(items)} items -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
