"""validator.py — source (MySQL) vs target (Snowflake) parity checks.

Primary parity signal = MySQL row count vs RAW *live* rows (excluding
soft-deleted). For full/incremental tables these should match when in sync.
The transform layer (SILVER Type-1 dedupe, or SCD2) is reported too, but its
count legitimately differs from RAW (dedupe / versioning), so it is informational.
"""
from __future__ import annotations

import loader


def target_fqn(tbl: dict) -> str:
    if tbl.get("table_type") == "scd2":
        s = loader.scd2_schema(tbl["source_db"]) if hasattr(loader, "scd2_schema") \
            else f"{tbl['source_db'].strip().upper()}_SCD2"
    else:
        _, s = loader.schema_names(tbl["source_db"])
    return f"{loader.DB}.{s}.{tbl['target_table']}"


def _mysql_count(mysql_conn, tbl: dict) -> int:
    cur = mysql_conn.cursor()
    cur.execute(
        f"SELECT COUNT(*) FROM `{tbl['source_db']}`.`{tbl['source_table']}`")
    n = cur.fetchone()[0]
    cur.close()
    return int(n)


def _sf_count(cur, fqn: str, where: str = "") -> int:
    q = f"SELECT COUNT(*) FROM {fqn}"
    if where:
        q += f" WHERE {where}"
    cur.execute(q)
    return int(cur.fetchone()[0])


def validate_table(sf_cur, mysql_conn, tbl: dict) -> dict:
    """Return parity counts/flags for one table.

    ok == source matches RAW live rows (the migration-completeness check).
    """
    source = _mysql_count(mysql_conn, tbl)
    raw_live = _sf_count(sf_cur, loader.raw_table(tbl),
                         'COALESCE("_IS_DELETED", FALSE) = FALSE')
    is_scd2 = tbl.get("table_type") == "scd2"
    target = _sf_count(sf_cur, target_fqn(tbl),
                       '"IS_CURRENT" = TRUE' if is_scd2 else "")
    return {
        "source": source,
        "raw_live": raw_live,
        "target": target,
        "target_layer": "SCD2 (current)" if is_scd2 else "SILVER",
        "ok": source == raw_live,
        "delta": source - raw_live,
    }
