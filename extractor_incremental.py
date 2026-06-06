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
import pyarrow as pa
import pyarrow.parquet as pq

LAG_MINUTES = 5  # exclude in-flight rows near "now"
# Above this row count the delta is split into multiple parquet files so the
# Snowflake COPY parallelizes across files (large delta / backfill scenarios).
# Override per table with "rows_per_file"; set 0 to disable splitting.
DEFAULT_ROWS_PER_FILE = 1_000_000


def _mysql_uri(src_cfg: dict, db: str) -> str:
    return (f'mysql://{quote_plus(src_cfg["user"])}:{quote_plus(src_cfg["password"])}'
            f'@{src_cfg["host"]}:{src_cfg["port"]}/{db}')


def extract_incremental_connectorx(tbl: dict, src_cfg: dict, export_dir: str):
    """Extract the incremental delta for one table.

    Returns (parquet_files: list[Path], row_count, watermark_to).
    Large deltas are split into multiple parquet files (see DEFAULT_ROWS_PER_FILE)
    so the Snowflake COPY parallelizes across files.
    """
    out_dir = Path(export_dir) / tbl["source_table"] / "incremental"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    wm_to = (datetime.now() - timedelta(minutes=LAG_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S")
    wm_from = tbl.get("last_loaded_at")
    wm_col = tbl["watermark_col"]

    if wm_from:
        where = (f"`{wm_col}` > '{wm_from}' AND `{wm_col}` <= '{wm_to}'")
    else:
        where = "1=1"  # first incremental run -> pull everything up to wm_to
        if wm_col:
            where = f"`{wm_col}` <= '{wm_to}'"

    query = (f"SELECT * FROM `{tbl['source_db']}`.`{tbl['source_table']}` "
             f"WHERE {where}")
    print(f"   query: {query}")

    uri = _mysql_uri(src_cfg, tbl["source_db"])

    read_kwargs = {"return_type": "arrow"}
    # Partitioned parallel read only when a numeric partition col is configured.
    if tbl.get("partition_col") and int(tbl.get("partition_num", 1)) > 1:
        read_kwargs["partition_on"] = tbl["partition_col"]
        read_kwargs["partition_num"] = int(tbl["partition_num"])

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
