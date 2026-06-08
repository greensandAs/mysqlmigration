"""extractor_incremental.py — Incremental engine using connectorx.

Reads only rows in the watermark window (last_loaded_at, now-lag] via a single
SQL query, returns Arrow, and writes Snappy Parquet (Snowflake-native).
Optional partitioned parallel reads for large windows.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import connectorx as cx
import mysql.connector
import pyarrow as pa
import pyarrow.parquet as pq

LAG_MINUTES = 5  # exclude in-flight rows near "now"
# Above this row count the delta is split into multiple parquet files so the
# Snowflake COPY parallelizes across files (large delta / backfill scenarios).
# Override per table with "rows_per_file"; set 0 to disable splitting.
DEFAULT_ROWS_PER_FILE = 1_000_000

# MySQL DECIMAL allows precision up to 65, but Snowflake NUMBER maxes at 38 and
# connectorx's Arrow decimal128 also caps at 38. Columns wider than this are
# read as text (CAST AS CHAR) so they land losslessly in a VARCHAR column.
SF_MAX_NUMERIC_PRECISION = 38

# Integer column types eligible for connectorx partitioned (parallel) reads.
_INT_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}


def _mysql_uri(src_cfg: dict, db: str) -> str:
    return (f'mysql://{quote_plus(src_cfg["user"])}:{quote_plus(src_cfg["password"])}'
            f'@{src_cfg["host"]}:{src_cfg["port"]}/{db}')


def _is_int_column(src_cfg: dict, db: str, table: str, col: str) -> bool:
    """True only if `col` is an integer type (safe for connectorx partition_on)."""
    try:
        conn = mysql.connector.connect(
            host=src_cfg["host"], port=int(src_cfg["port"]),
            user=src_cfg["user"], password=src_cfg["password"],
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT DATA_TYPE FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND UPPER(COLUMN_NAME)=UPPER(%s)",
            (db, table, col),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception:  # noqa: BLE001 — on doubt, don't partition
        return False
    return bool(row) and str(row[0]).lower() in _INT_TYPES


def _select_list(src_cfg: dict, db: str, table: str) -> str:
    """Explicit column projection for the incremental read.

    Two normalizations keep values consistent with how they're stored/compared:
      * decimal/numeric wider than Snowflake's 38 precision  -> CAST AS CHAR
        (lossless text; lands in a VARCHAR column).
      * datetime/timestamp  -> DATE_FORMAT to a session-local string, so
        connectorx/Arrow does NOT implicitly convert a MySQL TIMESTAMP to UTC.
        This keeps the watermark in one timezone end-to-end (matching NOW(), the
        WHERE literal, and the mysqlsh full-load), preventing a "stuck watermark"
        from a session-local vs. UTC frame mismatch.

    Returns "*" if column metadata can't be read (falls back to SELECT *).
    """
    try:
        conn = mysql.connector.connect(
            host=src_cfg["host"], port=int(src_cfg["port"]),
            user=src_cfg["user"], password=src_cfg["password"],
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, NUMERIC_PRECISION "
            "FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            (db, table),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:  # noqa: BLE001 — degrade gracefully to SELECT *
        print(f"   (column probe failed, using SELECT *: {e})")
        return "*"

    if not rows:
        return "*"
    parts = []
    for name, dtype, prec in rows:
        dt = str(dtype).lower()
        if (dt in ("decimal", "numeric")
                and prec is not None and int(prec) > SF_MAX_NUMERIC_PRECISION):
            parts.append(f"CAST(`{name}` AS CHAR) AS `{name}`")
        elif dt in ("datetime", "timestamp"):
            # Session-local string -> no UTC conversion on read.
            parts.append(
                f"DATE_FORMAT(`{name}`, '%Y-%m-%d %H:%i:%s.%f') AS `{name}`")
        else:
            parts.append(f"`{name}`")
    return ", ".join(parts)


def _source_ceiling(src_cfg: dict) -> str:
    """Window upper bound (wm_to) in the SOURCE database's clock.

    Uses MySQL NOW() rather than the extractor host's datetime.now() so wm_to is
    in the same timezone/clock as the watermark column values. Otherwise a
    host-vs-source timezone skew makes freshly added rows (whose watermark is in
    source time) fall outside (wm_from, wm_to] and get skipped until the host
    clock catches up. LAG_MINUTES still trims the most recent, possibly in-flight
    rows. Falls back to the host clock if the probe fails.
    """
    try:
        conn = mysql.connector.connect(
            host=src_cfg["host"], port=int(src_cfg["port"]),
            user=src_cfg["user"], password=src_cfg["password"],
        )
        cur = conn.cursor()
        cur.execute("SELECT NOW()")
        now = cur.fetchone()[0]
        cur.close()
        conn.close()
    except Exception as e:  # noqa: BLE001 — degrade to host clock
        print(f"   (source clock probe failed, using host clock: {e})")
        now = datetime.now()
    return (now - timedelta(minutes=LAG_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")


def extract_incremental_connectorx(tbl: dict, src_cfg: dict, export_dir: str):
    """Extract the incremental delta for one table.

    Returns (parquet_files: list[Path], row_count, watermark_to).
    Large deltas are split into multiple parquet files (see DEFAULT_ROWS_PER_FILE)
    so the Snowflake COPY parallelizes across files.
    """
    out_dir = Path(export_dir) / tbl["source_table"] / "incremental"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    wm_to = _source_ceiling(src_cfg)
    wm_from = tbl.get("last_loaded_at")
    wm_col = tbl["watermark_col"]

    if wm_from:
        where = (f"`{wm_col}` > '{wm_from}' AND `{wm_col}` <= '{wm_to}'")
    else:
        where = "1=1"  # first incremental run -> pull everything up to wm_to
        if wm_col:
            where = f"`{wm_col}` <= '{wm_to}'"

    select_list = _select_list(src_cfg, tbl["source_db"], tbl["source_table"])
    query = (f"SELECT {select_list} FROM `{tbl['source_db']}`.`{tbl['source_table']}` "
             f"WHERE {where}")
    print(f"   query: {query}")

    uri = _mysql_uri(src_cfg, tbl["source_db"])

    read_kwargs = {"return_type": "arrow"}
    # Partitioned parallel read only when partition_col is an INTEGER column —
    # connectorx requires int bounds ("Partition can only be done on int
    # columns"), so a decimal/varchar/string key falls back to a single read.
    pcol = tbl.get("partition_col")
    if pcol and int(tbl.get("partition_num", 1)) > 1:
        if _is_int_column(src_cfg, tbl["source_db"], tbl["source_table"], pcol):
            read_kwargs["partition_on"] = pcol
            read_kwargs["partition_num"] = int(tbl["partition_num"])
        else:
            print(f"   partition skipped: '{pcol}' is not an integer column "
                  f"(single-threaded read)")

    arrow_table = cx.read_sql(uri, query, **read_kwargs)
    rows = arrow_table.num_rows
    if rows == 0:
        print(f"   no new rows since {wm_from} — skipping.")
        return [], 0, wm_to

    rows_per_file = int(tbl.get("rows_per_file", DEFAULT_ROWS_PER_FILE) or 0)
    files = []
    if rows_per_file and rows > rows_per_file:
        # Split into multiple parquet files for parallel COPY.
        for i, batch in enumerate(arrow_table.to_batches(max_chunksize=rows_per_file)):
            part = out_dir / f"{tbl['source_table']}_{stamp}_part{i:04d}.parquet"
            pq.write_table(pa.Table.from_batches([batch]), part, compression="snappy")
            files.append(part)
        print(f"   extracted {rows:,} rows -> {len(files)} parquet files "
              f"(~{rows_per_file:,}/file)")
    else:
        out_file = out_dir / f"{tbl['source_table']}_{stamp}.parquet"
        pq.write_table(arrow_table, out_file, compression="snappy")
        files.append(out_file)
        print(f"   extracted {rows:,} rows -> {out_file.name}")

    return files, rows, wm_to
