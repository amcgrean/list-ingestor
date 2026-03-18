"""Sync customer/job context from an external cloud database into the app DB."""

from __future__ import annotations

import sys

from app import create_app
from app.services.customer_job_context import sync_contexts_from_cloud


def build_default_sync_query(customer_table: str, ship_to_table: str) -> str:
    return f"""
SELECT
    CONCAT(c.system_id, ':', c.cust_key, ':', COALESCE(s.seq_num::text, '0')) AS external_id,
    c.branch_code AS branch_code,
    c.cust_name AS customer_name,
    s.shipto_name AS project_name,
    json_build_array(
        c.cust_name,
        c.cust_code,
        s.shipto_name,
        s.city,
        s.state
    ) AS aliases,
    NULL::text AS material_context,
    CONCAT_WS(', ',
        NULLIF(s.address_1, ''),
        NULLIF(s.address_2, ''),
        NULLIF(s.address_3, ''),
        NULLIF(s.city, ''),
        NULLIF(s.state, ''),
        NULLIF(s.zip, ''),
        NULLIF(s.phone, '')
    ) AS job_notes,
    json_build_object(
        'customer_id', c.id,
        'ship_to_id', s.id,
        'system_id', c.system_id,
        'cust_key', c.cust_key,
        'cust_code', c.cust_code,
        'seq_num', s.seq_num
    ) AS metadata,
    (NOT COALESCE(c.is_deleted, false) AND NOT COALESCE(s.is_deleted, false)) AS is_active
FROM {customer_table} c
JOIN {ship_to_table} s
    ON s.system_id = c.system_id
   AND s.cust_key = c.cust_key
WHERE NOT COALESCE(c.is_deleted, false)
  AND NOT COALESCE(s.is_deleted, false)
"""


def main() -> int:
    app = create_app()
    with app.app_context():
        database_url = app.config.get("CLOUD_CONTEXT_DATABASE_URL", "")
        query = app.config.get("CLOUD_CONTEXT_SYNC_QUERY", "")
        source_system = app.config.get("CLOUD_CONTEXT_SOURCE_SYSTEM", "cloud")
        customer_table = app.config.get("CLOUD_CONTEXT_CUSTOMER_TABLE", "")
        ship_to_table = app.config.get("CLOUD_CONTEXT_SHIP_TO_TABLE", "")

        if not database_url:
            print("CLOUD_CONTEXT_DATABASE_URL is not set.", file=sys.stderr)
            return 1
        if not query:
            if customer_table and ship_to_table:
                query = build_default_sync_query(customer_table, ship_to_table)
            else:
                print(
                    "CLOUD_CONTEXT_SYNC_QUERY is not set and table names are missing.",
                    file=sys.stderr,
                )
                return 1

        stats = sync_contexts_from_cloud(
            database_url=database_url,
            query=query,
            source_system=source_system,
        )
        print(
            "Customer/job context sync complete: "
            f"seen={stats['seen']} inserted={stats['inserted']} "
            f"updated={stats['updated']} deactivated={stats['deactivated']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
