"""reconciler.py — Delete reconciliation (Level 3B, soft-delete).

Compares the set of primary keys in MySQL against those in <DB>_RAW.<table>.
Keys present in Snowflake but absent in MySQL were deleted at the source; they
are soft-deleted in RAW (_IS_DELETED=TRUE, _DELETED_AT=now). The next SILVER
build removes them from SILVER (USP_BUILD_SILVER handles the flag).

Invoked via `python orchestrator.py --reconcile`. Run on its own cadence
(e.g. daily/weekly) — it scans full PK sets, so it's heavier than an
incremental run. Tables without a single-column PK are skipped.
"""
from __future__ import annotations

import loader

# Soft-delete missing keys in batches to keep the IN-list bounded.
DELETE_BATCH = 5000


def _mysql_pks(mysql_conn, tbl: dict) -> set:
    cur = mysql_conn.cursor()
    cur.execute(
        f"SELECT `{tbl['primary_key']}` "
        f"FROM `{tbl['source_db']}`.`{tbl['source_table']}`"
    )
    pks = {r[0] for r in cur.fetchall()}
    cur.close()
    return pks


def _snowflake_live_pks(cur, tbl: dict) -> set:
    """Current (not-yet-deleted) PKs in RAW."""
    pk = tbl["primary_key"]
    cur.execute(
        f'SELECT "{pk}" FROM {loader.raw_table(tbl)} '
        f'WHERE COALESCE("_IS_DELETED", FALSE) = FALSE'
    )
    return {r[0] for r in cur.fetchall()}


def _sql_literal(v) -> str:
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def reconcile_table(cur, mysql_conn, tbl: dict) -> dict:
    """Soft-delete RAW rows whose PK no longer exists in MySQL.

    Returns {"deleted": n, "skipped": reason|None}.
    """
    pk = tbl.get("primary_key")
    if not pk:
        return {"deleted": 0, "skipped": "no primary key"}

    mysql_pks = _mysql_pks(mysql_conn, tbl)
    sf_pks = _snowflake_live_pks(cur, tbl)
    deleted_pks = list(sf_pks - mysql_pks)

    if not deleted_pks:
        return {"deleted": 0, "skipped": None}

    target = loader.raw_table(tbl)
    total = 0
    for i in range(0, len(deleted_pks), DELETE_BATCH):
        chunk = deleted_pks[i:i + DELETE_BATCH]
        in_list = ", ".join(_sql_literal(v) for v in chunk)
        cur.execute(
            f'UPDATE {target} '
            f'SET "_IS_DELETED" = TRUE, "_DELETED_AT" = CURRENT_TIMESTAMP() '
            f'WHERE "{pk}" IN ({in_list}) '
            f'AND COALESCE("_IS_DELETED", FALSE) = FALSE'
        )
        total += cur.rowcount or 0

    return {"deleted": total, "skipped": None}
