"""loader.py — Snowflake load layer: PUT + COPY (full) + COPY->MERGE (incremental).

Namespace (Option B): one database (MIGRATION_DB); each MySQL schema maps to its
own RAW/SILVER pair -> MIGRATION_DB.<SOURCE_DB>_RAW / <SOURCE_DB>_SILVER.
Shared objects (stage, file formats, RUN_LOG, USP_BUILD_SILVER) live in META.

FULL (mysqlsh / TSV+zstd):
  PUT data files -> stage, then TRUNCATE + COPY INTO RAW using an explicit
  column projection (CSV has no MATCH_BY_COLUMN_NAME, so order matters).

INCREMENTAL (connectorx / Parquet):
  PUT parquet -> stage, COPY INTO a transient temp table (MATCH_BY_COLUMN_NAME),
  then MERGE INTO RAW on the primary key.
"""
from __future__ import annotations

from pathlib import Path

import snowflake.connector

DB = "MIGRATION_DB"
STAGE = "@MIGRATION_DB.META.MIGRATION_STAGE"
PARQUET_FMT = "MIGRATION_DB.META.PARQUET_FMT"
TSV_FMT = "MIGRATION_DB.META.TSV_ZSTD_FMT"


def get_sf_conn(sf_cfg: dict):
    return snowflake.connector.connect(**sf_cfg)


def schema_names(source_db: str):
    """Map a MySQL schema name to its Snowflake RAW/SILVER schema pair."""
    base = source_db.strip().upper()
    return f"{base}_RAW", f"{base}_SILVER"


def scd2_schema(source_db: str) -> str:
    """Per-source SCD2 dimension schema name."""
    return f"{source_db.strip().upper()}_SCD2"


def raw_table(tbl: dict) -> str:
    raw_schema, _ = schema_names(tbl["source_db"])
    return f"{DB}.{raw_schema}.{tbl['target_table']}"


def merge_keys(tbl: dict) -> list:
    """Resolve the MERGE/dedupe key(s) for a table.

    Uses the optional "merge_keys" list (composite key) when present, otherwise
    falls back to the single "primary_key". Names are uppercased to match the
    Snowflake identifiers created by ddl_generator.
    """
    keys = tbl.get("merge_keys")
    if keys:
        return [str(k).upper() for k in keys]
    pk = tbl.get("primary_key")
    return [str(pk).upper()] if pk else []


def _stage_path(tbl: dict, subdir: str) -> str:
    base = tbl["source_db"].strip().upper()
    return f"{STAGE}/{base}/{tbl['target_table']}/{subdir}"


def clear_stage_safe(cur, tbl: dict, subdir: str) -> None:
    """Remove any stale files in a table's stage subdir (best-effort)."""
    try:
        cur.execute(f"REMOVE {_stage_path(tbl, subdir)}/")
    except Exception:
        pass


def put_file(cur, local_file, tbl: dict, subdir: str):
    """PUT one local file to the table's stage subdir."""
    stage_path = _stage_path(tbl, subdir)
    local = Path(local_file).resolve().as_posix()
    cur.execute(
        f"PUT 'file://{local}' {stage_path}/ "
        f"AUTO_COMPRESS=FALSE OVERWRITE=TRUE"
    )
    for row in cur.fetchall():
        print(f"   PUT {row[0]} -> {row[6] if len(row) > 6 else row[-1]}")


def copy_into_full(cur, tbl: dict, columns):
    """TRUNCATE RAW + COPY INTO from TSV data files (explicit column order)."""
    target = raw_table(tbl)
    stage_path = _stage_path(tbl, "full")
    col_list = ", ".join(f'"{name}"' for name, _ in columns)
    select_list = ", ".join(f"${i + 1}" for i in range(len(columns)))

    print(f"   TRUNCATE {target}")
    cur.execute(f"TRUNCATE TABLE IF EXISTS {target}")

    print(f"   COPY INTO {target} (full, TSV)")
    cur.execute(
        f"COPY INTO {target} ({col_list})\n"
        f"FROM (SELECT {select_list} FROM {stage_path}/)\n"
        f"PATTERN = '.*\\.tsv\\.zst'\n"
        f"FILE_FORMAT = (FORMAT_NAME = {TSV_FMT})\n"
        f"ON_ERROR = ABORT_STATEMENT\n"
        f"PURGE = TRUE"
    )
    return _copy_rows_loaded(cur)


def copy_into_merge(cur, tbl: dict):
    """COPY parquet into temp table, then MERGE into RAW on the (composite) key."""
    raw_schema, _ = schema_names(tbl["source_db"])
    target = raw_table(tbl)
    tmp = f"{DB}.{raw_schema}.{tbl['target_table']}_STAGE_TMP"
    stage_path = _stage_path(tbl, "incremental")
    keys = merge_keys(tbl)
    if not keys:
        raise ValueError(
            f"{tbl['source_table']}: incremental MERGE requires primary_key or merge_keys")
    key_set = {k.upper() for k in keys}

    print(f"   CREATE TEMP {tmp}")
    cur.execute(f"CREATE OR REPLACE TEMPORARY TABLE {tmp} LIKE {target}")

    print(f"   COPY INTO {tmp} (parquet)")
    cur.execute(
        f"COPY INTO {tmp}\n"
        f"FROM {stage_path}/\n"
        f"FILE_FORMAT = (FORMAT_NAME = {PARQUET_FMT})\n"
        f"MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE\n"
        f"ON_ERROR = ABORT_STATEMENT\n"
        f"PURGE = TRUE"
    )

    # Build column list from the RAW table (audit cols are auto-defaulted).
    cur.execute(
        "SELECT COLUMN_NAME FROM MIGRATION_DB.INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
        "ORDER BY ORDINAL_POSITION",
        (raw_schema, tbl["target_table"]),
    )
    # Exclude audit columns (leading underscore) in Python to avoid a literal
    # '%' in SQL colliding with the connector's %-style parameter binding.
    cols = [r[0] for r in cur.fetchall() if not r[0].startswith("_")]
    set_clause = ", ".join(
        f't."{c}" = s."{c}"' for c in cols if c.upper() not in key_set
    )
    insert_cols = ", ".join(f'"{c}"' for c in cols)
    insert_vals = ", ".join(f's."{c}"' for c in cols)

    # Dedupe the staged delta to one row per (composite) key so the MERGE is
    # deterministic. Multiple source rows matching one target row would otherwise
    # raise "Duplicate row detected during DML action" (error 100090).
    part_by = ", ".join(f'"{k}"' for k in keys)
    wm = tbl.get("watermark_col")
    order_by = f'"{wm}" DESC NULLS LAST' if wm else part_by
    source = (f"(SELECT * FROM {tmp} "
              f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {part_by} "
              f"ORDER BY {order_by}) = 1)")
    on_clause = " AND ".join(f't."{k}" = s."{k}"' for k in keys)

    print(f"   MERGE INTO {target} on {', '.join(keys)}")
    matched = (f" WHEN MATCHED THEN UPDATE SET {set_clause}"
               if set_clause else "")
    cur.execute(
        f"MERGE INTO {target} t USING {source} s "
        f"ON {on_clause}"
        f"{matched} "
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
    )
    return _rows_loaded(cur)


def _rows_loaded(cur) -> int:
    """Best-effort row count from the last DML statement."""
    try:
        return int(cur.rowcount) if cur.rowcount and cur.rowcount > 0 else 0
    except Exception:
        return 0


def _copy_rows_loaded(cur) -> int:
    """Sum the ROWS_LOADED column from a COPY INTO result set.

    For COPY, cur.rowcount is the number of result rows (one per file), NOT the
    rows loaded — so we read the 'rows_loaded' column instead.
    """
    try:
        cols = [d[0].lower() for d in cur.description]
        idx = cols.index("rows_loaded")
        return sum(int(r[idx]) for r in cur.fetchall() if r[idx] is not None)
    except Exception:
        return _rows_loaded(cur)


def call_build_silver(cur, tbl: dict) -> str:
    cur.execute(
        "CALL MIGRATION_DB.META.USP_BUILD_SILVER(%s, %s, %s, %s)",
        (tbl["source_db"], tbl["target_table"], ",".join(merge_keys(tbl)),
         tbl.get("watermark_col")),
    )
    return cur.fetchone()[0]


def call_build_scd2(cur, tbl: dict) -> str:
    track = tbl.get("scd2", {}).get("track_columns")
    track_csv = ", ".join(f'"{c}"' for c in track) if track else None
    cur.execute(
        "CALL MIGRATION_DB.META.USP_BUILD_SCD2(%s, %s, %s, %s, %s)",
        (tbl["source_db"], tbl["target_table"], ",".join(merge_keys(tbl)),
         tbl.get("watermark_col"), track_csv),
    )
    return cur.fetchone()[0]


def build_target(cur, tbl: dict) -> str:
    """Dispatch the transform step: SCD2 dimension or SILVER (Type 1)."""
    if tbl.get("table_type") == "scd2":
        return call_build_scd2(cur, tbl)
    return call_build_silver(cur, tbl)


def current_max_watermark(cur, tbl: dict):
    """Read MAX(watermark) from RAW — Snowflake is the source of truth.

    Returns the value as a VARCHAR (via TO_VARCHAR) so the connector never has
    to convert a large TIMESTAMP/NUMBER to a C int (avoids error 252005), and
    because the watermark is used as a string literal in the next MySQL query.
    """
    wm = tbl.get("watermark_col")
    if not wm:
        return None
    cur.execute(f'SELECT TO_VARCHAR(MAX("{wm}")) FROM {raw_table(tbl)}')
    val = cur.fetchone()[0]
    return str(val) if val is not None else None
