"""watermark.py — Watermark persistence.

Snowflake (MAX of the watermark column in RAW) is the source of truth.
The local migration_config.json holds a cached copy for convenience/visibility.
"""
from __future__ import annotations

import json
import tempfile
import os


def update_config_watermark(config_path: str, source_db: str, source_table: str,
                            new_value, status: str):
    """Persist last_loaded_at + last_run_status for one table (atomic write).

    Matches on BOTH source_db and source_table so tables with the same name in
    different schemas don't collide.
    """
    with open(config_path) as f:
        cfg = json.load(f)

    for t in cfg["tables"]:
        if t.get("source_db") == source_db and t.get("source_table") == source_table:
            if new_value is not None:
                t["last_loaded_at"] = new_value
            t["last_run_status"] = status
            break

    # Atomic replace to avoid corrupting config on a crash mid-write.
    d = os.path.dirname(os.path.abspath(config_path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, config_path)
