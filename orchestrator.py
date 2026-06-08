"""orchestrator.py — MySQL -> Snowflake migration driver.

Per active table:
  1. Generate/ensure RAW + SILVER DDL (from MySQL information_schema).
  2. Decide engine: FULL (first run or load_type=full) -> mysqlsh,
     else INCREMENTAL -> connectorx.
  3. Extract -> PUT to stage -> load into RAW (TRUNCATE+COPY | COPY+MERGE).
  4. Build SILVER (dedupe latest-by-watermark MERGE).
  5. Read MAX(watermark) from RAW (source of truth) -> cache to config.
  6. Write an audit row to META.RUN_LOG.

Usage:  python orchestrator.py [path/to/migration_config.json]
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import mysql.connector

try:
    from dotenv import load_dotenv
    load_dotenv()  # load .env if present; silently ignored otherwise
except ImportError:
    pass

import ddl_generator
import extractor_full
import extractor_incremental
import loader
import reconciler
import run_log
import schema_drift
import validator
import watermark


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _build_src_cfg(src: dict) -> dict:
    """Merge env vars over the JSON source config (env wins on conflict)."""
    out = dict(src)
    env_map = {
        "host": "MYSQL_HOST", "port": "MYSQL_PORT",
        "user": "MYSQL_USER", "password": "MYSQL_PASSWORD",
    }
    for key, env in env_map.items():
        val = os.getenv(env)
        if val is not None:
            out[key] = int(val) if key == "port" else val
    return out


def _build_sf_cfg(sf: dict) -> dict:
    """Merge env vars over the JSON snowflake config (env wins on conflict)."""
    out = dict(sf)
    env_map = {
        "account": "SF_ACCOUNT", "user": "SF_USER", "password": "SF_PASSWORD",
        "role": "SF_ROLE", "warehouse": "SF_WAREHOUSE",
        "database": "SF_DATABASE", "schema": "SF_SCHEMA",
    }
    for key, env in env_map.items():
        val = os.getenv(env)
        if val is not None:
            out[key] = val
    return out


def run(config_path: str = "migration_config.json", force_full: bool = False,
        only_table: str | None = None):
    with open(config_path) as f:
        cfg = json.load(f)

    src_cfg = _build_src_cfg(cfg["source"])
    sf_cfg = _build_sf_cfg(cfg["snowflake"])
    export_dir = cfg["export_dir"]
    batch_id = uuid.uuid4().hex[:12]

    print("=" * 64)
    print(f" MySQL -> Snowflake migration | batch {batch_id} | {_utc_now()} UTC")
    print("=" * 64)

    mysql_conn = mysql.connector.connect(
        host=src_cfg["host"], port=src_cfg["port"],
        user=src_cfg["user"], password=src_cfg["password"],
    )
    sf_conn = loader.get_sf_conn(sf_cfg)

    failed = 0
    try:
        for tbl in cfg["tables"]:
            if not tbl.get("active", True):
                continue
            if only_table and tbl["source_table"] != only_table:
                continue
            status = _process_table(tbl, src_cfg, sf_conn, mysql_conn,
                                    export_dir, batch_id, config_path,
                                    force_full=force_full)
            if status == "failed":
                failed += 1
    finally:
        mysql_conn.close()
        sf_conn.close()

    print(f"\nRun complete: {_utc_now()} UTC (batch {batch_id}) | "
          f"failed tables: {failed}")
    return failed


def _process_table(tbl, src_cfg, sf_conn, mysql_conn, export_dir, batch_id,
                   config_path, force_full: bool = False):
    first_run = tbl.get("last_loaded_at") is None
    is_full = force_full or first_run or tbl.get("load_type") == "full"
    engine = "mysqlsh" if is_full else "connectorx"
    label = "FULL" if is_full else "INCREMENTAL"
    print(f"\n[{label}/{engine}] {tbl['source_db']}.{tbl['source_table']} "
          f"-> {tbl['target_table']}")

    rec = {
        "batch_id": batch_id, "source_db": tbl["source_db"],
        "source_table": tbl["source_table"], "target_table": tbl["target_table"],
        "load_type": label.lower(), "engine": engine,
        "rows_extracted": 0, "rows_raw": 0, "rows_silver": 0,
        "watermark_from": tbl.get("last_loaded_at"), "watermark_to": None,
        "status": "failed", "error_message": None, "failed_step": None,
        "duration_sec": None,
        "run_start_utc": _utc_now(), "run_end_utc": None,
    }
    cur = sf_conn.cursor()
    t0 = time.monotonic()
    step = "start"
    try:
        # 1. DDL
        step = "ddl"
        meta = ddl_generator.generate_and_apply(sf_conn, mysql_conn, tbl)
        columns = meta["columns"]

        # 1b. Additive schema-drift reconciliation (RAW + SILVER).
        step = "schema_drift"
        schema_drift.detect_and_apply(cur, mysql_conn, tbl)

        if is_full:
            # 2/3. mysqlsh -> PUT -> TRUNCATE+COPY
            step = "extract_full"
            files, _ = extractor_full.extract_full_mysqlsh(
                tbl, src_cfg, export_dir)
            step = "put"
            for fp in files:
                loader.put_file(cur, fp, tbl, "full")
            step = "copy_full"
            rec["rows_raw"] = loader.copy_into_full(cur, tbl, columns)
            rec["rows_extracted"] = rec["rows_raw"]
        else:
            # 2/3. connectorx -> PUT -> COPY+MERGE
            step = "extract_incremental"
            files, rows, wm_to = extractor_incremental.extract_incremental_connectorx(
                tbl, src_cfg, export_dir)
            rec["rows_extracted"] = rows
            rec["watermark_to"] = wm_to
            if files and rows > 0:
                step = "put"
                loader.clear_stage_safe(cur, tbl, "incremental")
                for fp in files:
                    loader.put_file(cur, fp, tbl, "incremental")
                step = "copy_merge"
                rec["rows_raw"] = loader.copy_into_merge(cur, tbl)
            else:
                rec["status"] = "skipped"

        if rec["status"] != "skipped":
            # 4. Build target layer (SILVER, or SCD2 dimension)
            step = "build_target"
            msg = loader.build_target(cur, tbl)
            print(f"   {msg}")
            # 5. watermark from RAW (source of truth)
            step = "watermark"
            wm = loader.current_max_watermark(cur, tbl)
            if wm:
                rec["watermark_to"] = wm
            watermark.update_config_watermark(
                config_path, tbl["source_db"], tbl["source_table"], wm, "success")
            rec["status"] = "success"
        else:
            print("   skipped — no new rows")
            watermark.update_config_watermark(
                config_path, tbl["source_db"], tbl["source_table"], None, "skipped")

        sf_conn.commit()
        print(f"   done ({rec['status']})")

    except Exception as e:  # noqa: BLE001 — log + continue to next table
        sf_conn.rollback()
        rec["status"] = "failed"
        rec["failed_step"] = step
        rec["error_message"] = str(e)[:4000]
        watermark.update_config_watermark(
            config_path, tbl["source_db"], tbl["source_table"], None, "failed")
        print(f"   FAILED at step '{step}': {e}")
    finally:
        rec["run_end_utc"] = _utc_now()
        rec["duration_sec"] = round(time.monotonic() - t0, 2)
        try:
            run_log.write_run_log(cur, rec)
            sf_conn.commit()
        except Exception as le:  # noqa: BLE001
            print(f"   (run_log write failed: {le})")
        cur.close()

    return rec["status"]


def run_reconcile(config_path: str = "migration_config.json", only_table: str | None = None):
    """Delete-reconciliation pass: soft-delete RAW rows whose PK no longer
    exists in MySQL, then rebuild SILVER so deletes propagate. Only processes
    active tables with "reconcile": true and a primary key (optionally limited
    to a single source_table via only_table). Returns fail count.
    """
    with open(config_path) as f:
        cfg = json.load(f)
    src_cfg = _build_src_cfg(cfg["source"])
    sf_cfg = _build_sf_cfg(cfg["snowflake"])
    batch_id = uuid.uuid4().hex[:12]

    print("=" * 64)
    print(f" Delete reconciliation | batch {batch_id} | {_utc_now()} UTC")
    print("=" * 64)

    mysql_conn = mysql.connector.connect(
        host=src_cfg["host"], port=src_cfg["port"],
        user=src_cfg["user"], password=src_cfg["password"],
    )
    sf_conn = loader.get_sf_conn(sf_cfg)
    failed = 0
    try:
        for tbl in cfg["tables"]:
            if not tbl.get("active", True) or not tbl.get("reconcile", False):
                continue
            if only_table and tbl["source_table"] != only_table:
                continue
            cur = sf_conn.cursor()
            t0 = time.monotonic()
            rec = {
                "batch_id": batch_id, "source_db": tbl["source_db"],
                "source_table": tbl["source_table"],
                "target_table": tbl["target_table"],
                "load_type": "reconcile", "engine": "reconciler",
                "rows_extracted": None, "rows_raw": 0, "rows_silver": None,
                "watermark_from": None, "watermark_to": None,
                "status": "failed", "error_message": None, "failed_step": None,
                "duration_sec": None,
                "run_start_utc": _utc_now(), "run_end_utc": None,
            }
            print(f"\n[RECONCILE] {tbl['source_db']}.{tbl['source_table']} "
                  f"-> {tbl['target_table']}")
            try:
                result = reconciler.reconcile_table(cur, mysql_conn, tbl)
                if result["skipped"]:
                    print(f"   skipped — {result['skipped']}")
                    rec["status"] = "skipped"
                else:
                    rec["rows_raw"] = result["deleted"]
                    print(f"   soft-deleted {result['deleted']} row(s)")
                    if result["deleted"] > 0:
                        msg = loader.build_target(cur, tbl)
                        print(f"   {msg}")
                    rec["status"] = "success"
                sf_conn.commit()
            except Exception as e:  # noqa: BLE001
                sf_conn.rollback()
                failed += 1
                rec["status"] = "failed"
                rec["failed_step"] = "reconcile"
                rec["error_message"] = str(e)[:4000]
                print(f"   FAILED: {e}")
            finally:
                rec["run_end_utc"] = _utc_now()
                rec["duration_sec"] = round(time.monotonic() - t0, 2)
                try:
                    run_log.write_run_log(cur, rec)
                    sf_conn.commit()
                except Exception as le:  # noqa: BLE001
                    print(f"   (run_log write failed: {le})")
                cur.close()
    finally:
        mysql_conn.close()
        sf_conn.close()

    print(f"\nReconcile complete: {_utc_now()} UTC (batch {batch_id}) | "
          f"failed tables: {failed}")
    return failed


def run_validate(config_path: str = "migration_config.json", only_table: str | None = None):
    """Parity check: MySQL row count vs Snowflake RAW-live (and target) per
    active table. Prints a report, writes RUN_LOG rows, returns mismatch count.
    """
    with open(config_path) as f:
        cfg = json.load(f)
    src_cfg = _build_src_cfg(cfg["source"])
    sf_cfg = _build_sf_cfg(cfg["snowflake"])
    batch_id = uuid.uuid4().hex[:12]

    print("=" * 64)
    print(f" Source↔Target validation | batch {batch_id} | {_utc_now()} UTC")
    print("=" * 64)

    mysql_conn = mysql.connector.connect(
        host=src_cfg["host"], port=src_cfg["port"],
        user=src_cfg["user"], password=src_cfg["password"],
    )
    sf_conn = loader.get_sf_conn(sf_cfg)
    mismatches = 0
    try:
        for tbl in cfg["tables"]:
            if not tbl.get("active", True):
                continue
            if only_table and tbl["source_table"] != only_table:
                continue
            cur = sf_conn.cursor()
            start = _utc_now()
            t0 = time.monotonic()
            try:
                r = validator.validate_table(cur, mysql_conn, tbl)
                ok = r["ok"]
                flag = "OK" if ok else f"MISMATCH (delta {r['delta']:+d})"
                print(f"  {tbl['source_db']}.{tbl['source_table']}: "
                      f"source={r['source']} raw_live={r['raw_live']} "
                      f"{r['target_layer']}={r['target']} -> {flag}")
                if not ok:
                    mismatches += 1
                run_log.write_run_log(cur, {
                    "batch_id": batch_id, "source_db": tbl["source_db"],
                    "source_table": tbl["source_table"],
                    "target_table": tbl["target_table"],
                    "load_type": "validate", "engine": "validator",
                    "rows_extracted": r["source"], "rows_raw": r["raw_live"],
                    "rows_silver": r["target"], "watermark_from": None,
                    "watermark_to": None,
                    "status": "success" if ok else "mismatch",
                    "error_message": None if ok else
                    f"source {r['source']} != raw_live {r['raw_live']} (delta {r['delta']:+d})",
                    "failed_step": None if ok else "parity",
                    "duration_sec": round(time.monotonic() - t0, 2),
                    "run_start_utc": start, "run_end_utc": _utc_now(),
                })
                sf_conn.commit()
            except Exception as e:  # noqa: BLE001
                mismatches += 1
                print(f"  {tbl['source_table']}: VALIDATION ERROR: {e}")
            finally:
                cur.close()
    finally:
        mysql_conn.close()
        sf_conn.close()

    print(f"\nValidation complete: {_utc_now()} UTC (batch {batch_id}) | "
          f"mismatches: {mismatches}")
    return mismatches


if __name__ == "__main__":
    args = sys.argv[1:]
    reconcile_mode = "--reconcile" in args
    validate_mode = "--validate" in args
    force_full = "--full" in args
    only_table = None
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--table":
            i += 1
            only_table = args[i] if i < len(args) else None
        elif not a.startswith("--"):
            positional.append(a)
        i += 1
    path = positional[0] if positional else "migration_config.json"

    if validate_mode:
        failed = run_validate(path, only_table=only_table)
    elif reconcile_mode:
        failed = run_reconcile(path, only_table=only_table)
    else:
        failed = run(path, force_full=force_full, only_table=only_table)
    # Non-zero exit code on any failure/mismatch so schedulers can alert.
    raise SystemExit(1 if failed else 0)
