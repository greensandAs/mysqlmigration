"""config_generator.py — Auto-build migration_config.json from a MySQL schema.

Introspects MySQL information_schema for a given schema (default: company) and
emits one table entry per BASE TABLE with:
  - primary_key      : single-column PK (composite/no PK are flagged, see below)
  - watermark_col    : first match of WATERMARK_PRIORITY -> load_type=incremental
                       (no match -> load_type=full)
  - partition_col    : the PK if it is an integer type (enables parallel reads)
  - target_table     : uppercased source table name

PK handling:
  * exactly one PK column  -> active=true
  * composite PK           -> active=true, primary_key=first col (FLAGGED in output)
  * no PK                  -> active=false (needs manual decision; SILVER needs a key)

Connection settings (source/snowflake/export_dir) are preserved from an existing
migration_config.json if present, otherwise read from environment (.env).

Usage:  python config_generator.py [SCHEMA_NAME] [output_path]
"""
from __future__ import annotations

import json
import os
import sys

import mysql.connector

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

WATERMARK_PRIORITY = ["updated_at", "modified_at", "last_updated"]
INT_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}
DEFAULT_PARTITION_NUM = 8
CONFIG_PATH = "migration_config.json"


def _source_from_env():
    return {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", "YOUR_MYSQL_PASSWORD"),
    }


def _snowflake_from_env():
    return {
        "account": os.getenv("SF_ACCOUNT", ""),
        "user": os.getenv("SF_USER", ""),
        "password": os.getenv("SF_PASSWORD", "YOUR_SF_PASSWORD"),
        "role": os.getenv("SF_ROLE", "ACCOUNTADMIN"),
        "warehouse": os.getenv("SF_WAREHOUSE", "COMPUTE_WH"),
        "database": os.getenv("SF_DATABASE", "MIGRATION_DB"),
        "schema": os.getenv("SF_SCHEMA", "META"),
    }


def _list_tables(cur, schema):
    cur.execute(
        "SELECT TABLE_NAME FROM information_schema.tables "
        "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' "
        "ORDER BY TABLE_NAME",
        (schema,),
    )
    return [r[0] for r in cur.fetchall()]


def _pk_columns(cur, schema, table):
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.key_column_usage "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
        "AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION",
        (schema, table),
    )
    return [r[0] for r in cur.fetchall()]


def _columns(cur, schema, table):
    """Return {column_name_lower: data_type_lower}."""
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.columns "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
        (schema, table),
    )
    return {r[0].lower(): (r[1] or "").lower() for r in cur.fetchall()}


def _detect_watermark(cols):
    for name in WATERMARK_PRIORITY:
        if name in cols:
            return name
    return None


def build_table_entry(cur, schema, table):
    pk_cols = _pk_columns(cur, schema, table)
    cols = _columns(cur, schema, table)
    wm = _detect_watermark(cols)
    flags = []

    if len(pk_cols) == 1:
        pk = pk_cols[0]
        active = True
    elif len(pk_cols) > 1:
        pk = pk_cols[0]
        active = True
        flags.append(f"composite PK {pk_cols} — MERGE/dedupe uses '{pk}' only; review")
    else:
        # No PK -> full reload + SILVER passthrough (no dedupe). Still active.
        pk = None
        active = True
        flags.append("no primary key — full reload + SILVER passthrough (no dedupe)")

    # No single dedupe key -> force full load (incremental MERGE needs a PK).
    load_type = "incremental" if (wm and pk) else "full"
    # Use the PK as partition col only when it is an integer type.
    partition_col = pk if (pk and cols.get(pk.lower()) in INT_TYPES) else None

    entry = {
        "source_db": schema,
        "source_table": table,
        "target_table": table.upper(),
        "primary_key": pk,
        "load_type": load_type,
        "watermark_col": wm,
        "last_loaded_at": None,
        "partition_col": partition_col,
        "partition_num": DEFAULT_PARTITION_NUM if partition_col else 1,
        "reconcile": False,
        "active": active,
        "last_run_status": None,
    }
    if flags:
        entry["_review"] = "; ".join(flags)
    return entry


def main():
    schema = sys.argv[1] if len(sys.argv) > 1 else "company"
    out_path = sys.argv[2] if len(sys.argv) > 2 else CONFIG_PATH

    # Preserve connection blocks from an existing config if present + valid.
    cfg = None
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        try:
            with open(out_path) as f:
                cfg = json.load(f)
        except json.JSONDecodeError:
            print(f"WARNING: {out_path} is not valid JSON — rebuilding from env.")
            cfg = None
    if cfg is None:
        cfg = {}
    cfg.setdefault("source", _source_from_env())
    cfg.setdefault("snowflake", _snowflake_from_env())
    cfg.setdefault("export_dir", "./export")

    src = cfg["source"]
    # Overlay env vars for the live connection (keeps secrets out of the JSON).
    conn_src = dict(src)
    for key, env in (("host", "MYSQL_HOST"), ("port", "MYSQL_PORT"),
                     ("user", "MYSQL_USER"), ("password", "MYSQL_PASSWORD")):
        val = os.getenv(env)
        if val is not None:
            conn_src[key] = int(val) if key == "port" else val
    con = mysql.connector.connect(
        host=conn_src["host"], port=int(conn_src["port"]),
        user=conn_src["user"], password=conn_src["password"],
    )
    cur = con.cursor()
    try:
        tables = _list_tables(cur, schema)
        if not tables:
            print(f"No BASE TABLE found in schema '{schema}'. "
                  f"Check the schema name and grants.")
            return 1
        entries = [build_table_entry(cur, schema, t) for t in tables]
    finally:
        cur.close()
        con.close()

    # Merge: keep tables from other schemas; for this schema, preserve existing
    # (tuned) entries and only add genuinely new tables. Never clobbers other
    # schemas or your manual settings.
    existing = cfg.get("tables", [])
    other_schema = [t for t in existing if t.get("source_db") != schema]
    existing_here = {t["source_table"]: t for t in existing
                     if t.get("source_db") == schema}

    merged, added, kept = list(other_schema), 0, 0
    for e in entries:
        if e["source_table"] in existing_here:
            merged.append(existing_here[e["source_table"]])  # preserve tuning
            kept += 1
        else:
            merged.append(e)
            added += 1

    cfg["tables"] = merged
    with open(out_path, "w") as f:
        json.dump(cfg, f, indent=2)

    review = [e["source_table"] for e in entries if "_review" in e]
    print(f"Schema '{schema}': {added} new table(s) added, {kept} preserved; "
          f"{len(other_schema)} table(s) from other schemas kept. -> {out_path}")
    if review:
        print(f"Review (new tables): {', '.join(review)} "
              f"(see _review notes; some are inactive).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
