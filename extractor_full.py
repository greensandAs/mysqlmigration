"""extractor_full.py — Full-load engine using MySQL Shell (mysqlsh).

Uses `util.dumpTables` for a fast, parallel, zstd-compressed dump. Output is a
bundle (metadata .json/.sql + DDL .sql + data as <db>@<table>@@<chunk>.tsv.zst).
Only the *.tsv.zst data files are loaded into Snowflake (see loader.py PATTERN).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import quote_plus


def extract_full_mysqlsh(tbl: dict, src_cfg: dict, export_dir: str):
    """Run mysqlsh dumpTables for one table. Returns (data_files, out_dir)."""
    out_dir = Path(export_dir) / tbl["source_table"] / "full"
    # mysqlsh requires the output directory to NOT already exist.
    if out_dir.exists():
        for p in sorted(out_dir.glob("**/*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        out_dir.rmdir()
    out_dir.parent.mkdir(parents=True, exist_ok=True)

    threads = int(tbl.get("partition_num", 8))
    out_uri = str(out_dir).replace("\\", "/")
    js_cmd = (
        f'util.dumpTables("{tbl["source_db"]}", ["{tbl["source_table"]}"], '
        f'"{out_uri}", '
        f'{{threads: {threads}, compression: "zstd", showProgress: true}})'
    )
    uri = (f'mysql://{quote_plus(src_cfg["user"])}:{quote_plus(src_cfg["password"])}'
           f'@{src_cfg["host"]}:{src_cfg["port"]}')
    cmd = ["mysqlsh", f"--uri={uri}", "--js", "--execute", js_cmd]

    print(f"   mysqlsh full dump starting -> {out_dir}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"mysqlsh failed:\n{result.stderr}")

    data_files = sorted(out_dir.glob("*.tsv.zst"))
    if not data_files:
        raise RuntimeError(
            f"mysqlsh produced no .tsv.zst data files in {out_dir}"
        )
    print(f"   full dump complete: {len(data_files)} data file(s)")
    return data_files, out_dir
