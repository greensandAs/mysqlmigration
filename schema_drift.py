"""schema_drift.py — additive schema-drift handling.

Before each load, compares MySQL columns against the existing <DB>_RAW.<table>:
  * New MySQL columns  -> ALTER TABLE ADD COLUMN on RAW and SILVER (typed via the
                          MySQL->Snowflake map), so the load no longer fails.
  * Dropped MySQL cols -> warn only (data in Snowflake is preserved).

Only additive changes are auto-applied; drops/type changes are surfaced for a
human to handle.
"""
from __future__ import annotations

import ddl_generator


def _snowflake_business_cols(cur, schema: str, table: str) -> set:
    """Uppercased non-audit (non '_'-prefixed) columns of a Snowflake table."""
    cur.execute(
        "SELECT COLUMN_NAME FROM MIGRATION_DB.INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
        "ORDER BY ORDINAL_POSITION",
        (schema, table),
    )
    return {r[0].upper() for r in cur.fetchall() if not r[0].startswith("_")}


def detect_and_apply(cur, mysql_conn, tbl: dict) -> dict:
    """Reconcile additive schema drift for one table.

    Returns {"added": [...], "dropped": [...]}. Assumes RAW/SILVER already exist
    (ddl_generator runs first); no-ops cleanly if a table is brand new.
    """
    raw_schema, silver_schema = ddl_generator.schema_names(tbl["source_db"])
    # For SCD2 tables the secondary layer is the <DB>_SCD2 dimension.
    secondary_schema = (ddl_generator.scd2_schema(tbl["source_db"])
                        if tbl.get("table_type") == "scd2" else silver_schema)
    target = tbl["target_table"]

    sf_cols = _snowflake_business_cols(cur, raw_schema, target)
    if not sf_cols:
        # Table not created yet (first run) — ddl_generator will build it.
        return {"added": [], "dropped": []}

    mysql_cols = ddl_generator.get_mysql_columns(
        mysql_conn, tbl["source_db"], tbl["source_table"])
    mysql_by_upper = {name.upper(): (name, sf_type) for name, sf_type in mysql_cols}

    new_cols = [mysql_by_upper[u] for u in mysql_by_upper if u not in sf_cols]
    dropped = [c for c in sf_cols if c not in mysql_by_upper]

    added = []
    for name, sf_type in new_cols:
        for schema in (raw_schema, secondary_schema):
            cur.execute(
                f'ALTER TABLE MIGRATION_DB.{schema}.{target} '
                f'ADD COLUMN IF NOT EXISTS "{name}" {sf_type}'
            )
        added.append(name)
        print(f"   schema drift: added \"{name}\" {sf_type} to "
              f"{raw_schema}/{secondary_schema}.{target}")

    if dropped:
        print(f"   schema drift WARNING: columns dropped in MySQL but kept in "
              f"Snowflake: {sorted(dropped)}")

    return {"added": added, "dropped": sorted(dropped)}
