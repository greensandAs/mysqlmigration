# MySQL → Snowflake Migration Tool

Standalone tool to migrate MySQL (e.g. `localhost`) tables into Snowflake with
**full** and **incremental** loads, building a **RAW** (bronze) and **SILVER**
(clean/deduped) layer.

> Snowflake cannot reach your local MySQL. Extraction runs **locally** and
> pushes files to a Snowflake internal stage; loading + transforms run **in
> Snowflake**.

## Architecture

```
migration_config.json
        │  per table: load_type / watermark / pk
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ FULL (first run or load_type=full) → mysqlsh dumpTables (TSV+zstd) │
 │ INCREMENTAL                        → connectorx → Parquet (Arrow) │
 └──────────────────────────────────────────────────────────────┘
        │  PUT
        ▼
 @MIGRATION_DB.META.MIGRATION_STAGE/<SOURCE_DB>/<TABLE>/{full|incremental}/
        ▼
 <SOURCE_DB>_RAW.<TABLE>     FULL: TRUNCATE + COPY (explicit column order)
                             INCR: COPY → temp → MERGE on primary key
        ▼
 <SOURCE_DB>_SILVER.<TABLE>  USP_BUILD_SILVER: dedupe latest-by-watermark → MERGE
        ▼
 META.RUN_LOG    one audit row per table per run
```

**Namespace mapping (Option B):** MySQL is 2-level (`schema.table`); Snowflake is
3-level (`database.schema.table`). One database `MIGRATION_DB` is used, and each
MySQL schema (`source_db`) maps to its own pair of Snowflake schemas:

| MySQL | Snowflake RAW | Snowflake SILVER |
|-------|---------------|------------------|
| `sales.orders` | `MIGRATION_DB.SALES_RAW.ORDERS` | `MIGRATION_DB.SALES_SILVER.ORDERS` |
| `crm.orders`   | `MIGRATION_DB.CRM_RAW.ORDERS`   | `MIGRATION_DB.CRM_SILVER.ORDERS`   |

These `<SOURCE_DB>_RAW` / `<SOURCE_DB>_SILVER` schemas are created automatically by
`ddl_generator.py`. Shared objects (stage, file formats, `RUN_LOG`,
`USP_BUILD_SILVER`) live in `MIGRATION_DB.META`.

Watermarks: **Snowflake is the source of truth** — after each load the tool
reads `MAX(watermark_col)` from `RAW.<TABLE>` and caches it back into
`migration_config.json` (`last_loaded_at`).

## Files

| File | Purpose |
|------|---------|
| `setup.sql` | Creates `MIGRATION_DB`, schemas (`RAW`/`SILVER`/`META`), stage, file formats, `META.RUN_LOG`, `USP_BUILD_SILVER`. Run once in Snowflake. |
| `ddl_generator.py` | MySQL `information_schema` → RAW + SILVER `CREATE TABLE` (typed). |
| `extractor_full.py` | mysqlsh `dumpTables` full dump. |
| `extractor_incremental.py` | connectorx incremental extract → Parquet. |
| `loader.py` | PUT + COPY (full) + COPY→MERGE (incremental) + SILVER call. |
| `watermark.py` | Atomic config watermark cache update. |
| `run_log.py` | Audit row writer. |
| `orchestrator.py` | Main driver. |
| `migration_config.json` | Connections + per-table config. |

## Prerequisites

```bash
pip install -r requirements.txt
mysqlsh --version      # MySQL Shell must be on PATH (full-load engine)
```

## Setup

1. Run `setup.sql` in Snowflake (creates DB, schemas, stage, formats, proc).
2. Edit `migration_config.json`:
   - MySQL + Snowflake credentials (consider key-pair auth instead of passwords).
   - One entry per table: `source_db`, `source_table`, `target_table`,
     `primary_key`, `load_type` (`full`|`incremental`), `watermark_col`,
     `partition_col`/`partition_num` (parallel reads for large tables).

## Run

```bash
python orchestrator.py                       # uses ./migration_config.json
python orchestrator.py /path/to/config.json  # explicit config
python orchestrator.py --reconcile           # delete-reconciliation pass
```

The process exits non-zero if any table fails, so a scheduler can alert on it.

## Local UI (optional)

A lightweight local Streamlit control panel:

```bash
streamlit run app.py
```

Tabs: **Config** (view/edit `active`/`reconcile`/`load_type`/watermark and save),
**Run** (trigger a migration, `--reconcile`, or `config_generator`), **History**
(`META.RUN_LOG`), **Counts** (RAW vs SILVER per table). The sidebar tests MySQL +
Snowflake connectivity. Runs on your machine (it needs localhost MySQL); it shells
out to `orchestrator.py`/`config_generator.py` and reuses the same `.env`/config.

Behaviour per table:

| Run | `last_loaded_at` | Engine | RAW load |
|-----|------------------|--------|----------|
| First ever | `null` | mysqlsh | TRUNCATE + COPY |
| Subsequent (incremental) | set | connectorx | COPY temp → MERGE |
| Forced full | `load_type=full` | mysqlsh | TRUNCATE + COPY |

## Notes / limitations

- mysqlsh output is a bundle; only `*.tsv.zst` data files are loaded
  (`PATTERN='.*\.tsv\.zst'`). Full-load relies on column order matching the
  generated RAW DDL (CSV has no `MATCH_BY_COLUMN_NAME`).
- Incremental uses a 5-minute lag window to avoid in-flight rows.
- Large incremental deltas / backfills are split into multiple parquet files for
  parallel COPY. Controlled by `DEFAULT_ROWS_PER_FILE` in `extractor_incremental.py`
  (default 1,000,000 rows/file); override per table with `"rows_per_file"` in the
  config (`0` disables splitting).
- Passwords are read from `migration_config.json` — keep it out of version
  control and prefer key-pair auth for Snowflake in production.
- Parallel **table** processing is not implemented (tables run sequentially).

## Sync semantics

- **Inserts / updates** — synced via watermark + MERGE (incremental) or full reload.
- **Deletes**:
  - *Full-load tables* — deletes propagate automatically (TRUNCATE + reload).
  - *Incremental tables* — deletes are NOT seen by the watermark. Run
    `python orchestrator.py --reconcile` on a separate cadence (e.g. daily) for
    tables with `"reconcile": true` and a primary key. It diffs MySQL PKs vs
    `<DB>_RAW`, **soft-deletes** missing keys (`_IS_DELETED=TRUE`, `_DELETED_AT`),
    and rebuilds SILVER (which excludes soft-deleted rows). Set `"reconcile": true`
    per table in the config (default `false`).
- **Schema drift** — before each load, new MySQL columns are auto-added to RAW +
  SILVER (typed); dropped MySQL columns are warned about only (data preserved).
  Type changes are not auto-applied.
- **Real-time CDC** (binlog) — not implemented; this is batch.

## SCD Type 2 dimensions

Set `"table_type": "scd2"` on a table to build a Type-2 slowly-changing
dimension in `MIGRATION_DB.<DB>_SCD2.<TABLE>` (instead of SILVER). RAW loads
unchanged; the dimension is built **from RAW** by `USP_BUILD_SCD2`.

```json
{
  "source_db": "company", "source_table": "worker", "target_table": "WORKER",
  "primary_key": "WORKER_ID", "load_type": "incremental",
  "watermark_col": "UPDATED_AT", "table_type": "scd2",
  "scd2": { "track_columns": ["WORKER_NAME", "SALARY", "DEPARTMENT"] }
}
```

- **Control columns**: `DIM_KEY` (surrogate), `EFF_FROM`, `EFF_TO`, `IS_CURRENT`,
  `IS_DELETED`, `ROW_HASH`.
- **Change detection**: `HASH(tracked_columns)` vs the current version. If
  `scd2.track_columns` is omitted, all non-PK business columns are tracked.
- **EFF_FROM** = the watermark/event time when present, else load time.
- **On change**: expire the current version (`EFF_TO`, `IS_CURRENT=FALSE`) and
  insert a new current version.
- **On delete** (via `--reconcile` soft-delete): expire the current version with
  `IS_DELETED=TRUE`; no new version is inserted.

Query patterns:
```sql
-- current state
SELECT * FROM MIGRATION_DB.COMPANY_SCD2.WORKER WHERE IS_CURRENT = TRUE;
-- point-in-time
SELECT * FROM MIGRATION_DB.COMPANY_SCD2.WORKER
WHERE '2025-01-01' BETWEEN EFF_FROM AND EFF_TO;
```

### Known edge cases
- A previously soft-deleted PK that reappears in MySQL is re-loaded by the
  incremental MERGE but its `_IS_DELETED` flag is not auto-reset; run a `--reconcile`
  or a full reload to clear stale delete flags if undeletes are common.
