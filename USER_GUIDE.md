# MySQL → Snowflake Migration Tool — User Guide

A batch ELT tool that migrates MySQL databases into Snowflake with **full** and
**incremental** loads, a **RAW → SILVER / SCD2** medallion model, delete
reconciliation, schema-drift handling, and a Tiger Analytics branded Streamlit
control panel.

---

## 1. What this app is

Snowflake cannot reach a `localhost` MySQL directly. So this tool runs the
**extraction locally** and pushes files to a Snowflake internal stage; the
**loading and transforms run inside Snowflake**.

```
┌── YOUR MACHINE ───────────────┐        ┌── SNOWFLAKE ────────────────────────┐
│ orchestrator.py                │ PUT    │ @MIGRATION_DB.META.MIGRATION_STAGE  │
│  • mysqlsh  (full dump)        │ ─────▶ │  COPY → RAW → MERGE/SCD2 build      │
│  • connectorx (incremental)    │        │  RUN_LOG audit                      │
└────────────────────────────────┘        └─────────────────────────────────────┘
        ▲ app.py (Streamlit) = config + run + monitor
```

- **Two extract engines**: `mysqlsh` parallel dump for full loads (TSV+zstd),
  `connectorx` for incremental deltas (Arrow → Parquet).
- **Medallion layers** per source schema:
  - `RAW` — faithful landing copy (history/audit, `_LOAD_TS`, `_IS_DELETED`, …)
  - `SILVER` — deduped current view (SCD Type 1), or
  - `SCD2` — versioned dimension (SCD Type 2)
- **Watermark** source of truth = Snowflake (`MAX(watermark)` from RAW).

---

## 2. Namespace model

One database `MIGRATION_DB`. Each MySQL schema maps to its own Snowflake schemas:

| MySQL | Snowflake |
|-------|-----------|
| `sales.orders` | `MIGRATION_DB.SALES_RAW.ORDERS` |
| (Type 1) | `MIGRATION_DB.SALES_SILVER.ORDERS` |
| (Type 2) | `MIGRATION_DB.SALES_SCD2.ORDERS` (+ `ORDERS_CURRENT` view) |

Shared objects live in `MIGRATION_DB.META`: the internal stage, file formats,
`RUN_LOG`, and the build procedures. **Column names are created UPPERCASE** so
you can query without quotes (`SELECT id` works).

---

## 3. One-time setup

1. **Install deps** (locally): `pip install -r requirements.txt` and ensure
   `mysqlsh --version` works (MySQL Shell is the full-load engine).
2. **Snowflake objects**: run `setup.sql` once. It creates `MIGRATION_DB`, the
   `META` schema, stage, `PARQUET_FMT` / `TSV_ZSTD_FMT`, `RUN_LOG`,
   `USP_BUILD_SILVER`, and `USP_BUILD_SCD2`.
3. **Credentials**: copy `.env.example` → `.env` and fill `MYSQL_*` and `SF_*`.
   Env vars override the (placeholder) passwords in `migration_config.json`.
4. Launch the app from the project folder: `streamlit run app.py`.

---

## 4. Configuration (`migration_config.json`)

Per-table fields:

| Field | Meaning |
|-------|---------|
| `source_db` / `source_table` | MySQL schema and table |
| `target_table` | Snowflake table name (UPPERCASE) |
| `primary_key` | dedupe / MERGE key (UPPERCASE) |
| `load_type` | `full` or `incremental` |
| `watermark_col` | timestamp column for incremental (UPPERCASE) |
| `last_loaded_at` | last synced watermark (managed by the tool) |
| `partition_col` / `partition_num` | parallel connectorx reads |
| `rows_per_file` | split large incremental deltas (0 = off) |
| `reconcile` | enable delete reconciliation for this table |
| `table_type` | omit for standard (SILVER); `"scd2"` for Type 2 |
| `scd2.track_columns` | columns whose change creates a new version |
| `active` | include in runs |

You can build config three ways: **Generate** from a MySQL schema (Config tab),
**➕ Add table** manually (Config tab), or edit `migration_config.json` directly.

---

## 5. Load types

- **Full** (`load_type: full`, or any table's first run): `mysqlsh` dumps the whole
  table → `TRUNCATE` + `COPY` into RAW. Deletes propagate automatically (full
  snapshot each run).
- **Incremental** (`load_type: incremental`, after first run): `connectorx` pulls
  `WHERE watermark_col > last_loaded_at` (with a 5-min lag) → `COPY` to a temp
  table → `MERGE` into RAW on the primary key.
- **No primary key**: full reload + SILVER passthrough (no dedupe).

> The **first run of any table is always a FULL seed**, even if marked
> incremental — it establishes the baseline and stamps `last_loaded_at`. The next
> run switches to incremental.

---

## 6. SCD Type 1 (default / SILVER)

This is the default. `SILVER.<table>` holds **one current row per primary key**
(latest by watermark) — old attribute values are overwritten, no history.

**Configure:** nothing special — just leave `table_type` unset. The orchestrator
calls `USP_BUILD_SILVER` after loading RAW.

Query: `SELECT * FROM MIGRATION_DB.<DB>_SILVER.<TABLE>;`

---

## 7. SCD Type 2 (versioned history / SCD2)

Keeps **full history**: when a tracked attribute changes, the current version is
expired and a new version is inserted.

**Configure** (Config tab → table → **Target type = scd2**, or in JSON):
```json
{
  "source_db": "company", "source_table": "worker", "target_table": "WORKER",
  "primary_key": "WORKER_ID", "load_type": "incremental",
  "watermark_col": "UPDATED_AT", "table_type": "scd2",
  "scd2": { "track_columns": ["WORKER_NAME", "SALARY", "DEPARTMENT"] }
}
```
- Builds `MIGRATION_DB.<DB>_SCD2.<TABLE>` with control columns: `DIM_KEY`
  (surrogate), `EFF_FROM`, `EFF_TO`, `IS_CURRENT`, `IS_DELETED`, `ROW_HASH`.
- Change detection = `HASH(track_columns)` (blank `track_columns` = all non-PK).
- `EFF_FROM` = the watermark/event time when present, else load time.
- A deleted key (via reconcile) expires its current version with `IS_DELETED=TRUE`.
- A `<TABLE>_CURRENT` view exposes just `IS_CURRENT=TRUE` rows.

Query patterns:
```sql
-- current state
SELECT * FROM MIGRATION_DB.COMPANY_SCD2.WORKER_CURRENT;
-- point-in-time
SELECT * FROM MIGRATION_DB.COMPANY_SCD2.WORKER
WHERE '2025-01-01' BETWEEN EFF_FROM AND EFF_TO;
```

---

## 8. Deletes (reconciliation)

The watermark cannot see hard-deletes. For incremental tables, set
`reconcile: true` and run a reconcile pass:
- It diffs MySQL primary keys vs `<DB>_RAW`, and **soft-deletes** missing keys
  (`_IS_DELETED=TRUE`, `_DELETED_AT`). SILVER/SCD2 builds then drop/expire them.
- Run from the app (**Run → 🗑️ Reconcile Deletes** for all, or the per-table
  **🗑️ Reconcile**), or `python orchestrator.py --reconcile [--table NAME]`.
- Full-load tables don't need this — deletes propagate via the full reload.

---

## 9. Schema drift
Before each load the tool diffs MySQL columns vs the RAW table:
- **New** MySQL columns → auto `ALTER TABLE ADD COLUMN` (typed) on RAW + SILVER/SCD2.
- **Dropped** columns → warning only (data preserved).
- Type changes are not auto-applied.

---

## 9b. Source ↔ target validation (parity)

Before cutover you need to prove the data is fully in sync. The tool compares
**MySQL row counts** against **Snowflake RAW live rows** (excluding soft-deleted):
- **Counts tab → 🔍 Validate vs MySQL**, or `python orchestrator.py --validate [--table NAME]`.
- `Parity ✅` = `source == RAW live`. `⚠️` = mismatch (re-run the table / reconcile).
- The transform layer (SILVER dedupe / SCD2 versions) is shown for reference but
  legitimately differs from RAW, so parity is judged on **source vs RAW**.
- `--validate` writes `RUN_LOG` rows (status `failed` on mismatch) and exits
  non-zero, so a scheduler can gate cutover on a clean validation.

---

## 10. The app (tabs)

- **📊 Dashboard** — run summary + per-table status cards (load type, SCD2 /
  RECONCILE pills, last sync, PK).
- **▶️ Run** — Run Migration / Reconcile Deletes / Force Full Reload; Run/Reconcile
  a single table; live streaming log.
- **⚙️ Config** — Generate config from a MySQL schema, ➕ Add table (popup), and a
  per-table editor (load type, watermark, PK, partitions, rows/file, reconcile,
  **Target type standard/scd2** + tracked columns).
- **📜 History** — `META.RUN_LOG` with color-coded status and failed-run drill-down.
- **🔢 Counts** — RAW vs SILVER / SCD2-current row counts and match check.

The **sidebar** shows connection status (auto-checked, re-check button), active
table counts, the namespace map, and this **User Guide**.

---

## 11. End-to-end process

1. `pip install -r requirements.txt`; confirm `mysqlsh --version`.
2. Run `setup.sql` once in Snowflake.
3. Fill `.env`; launch `streamlit run app.py`.
4. **Config tab → Generate** for your MySQL schema (or ➕ Add table).
5. Review/edit each table: load type, watermark, PK, reconcile, **Target type**.
6. **Run tab → ▶️ Run Migration** (first run = full seed for every table).
7. Re-run later → incremental tables pick up changes via watermark + MERGE.
8. Periodically **🗑️ Reconcile Deletes** for tables with `reconcile: true`.
9. Verify in **Counts** and **History**; query `SILVER` / `SCD2_CURRENT` in Snowflake.

---

## 12. Notes & troubleshooting

- **First run is always full** per table (baseline). Incremental kicks in next run.
- **Incremental shows "skipped"** = no rows past the watermark (+5-min lag). To
  test, update a row's watermark to a value newer than `last_loaded_at` but older
  than `now − 5 min` (or set `LAG_MINUTES = 0` in `extractor_incremental.py`).
- **Existing lowercase tables**: columns are UPPERCASE only for tables created
  after enabling Option B. Drop and recreate older tables to convert them.
- **Connection errors**: use the sidebar **Re-check Connections**; passwords come
  from `.env`. MFA on Snowflake may need `authenticator=username_password_mfa` or
  key-pair auth.
- **Monitoring**: `monitoring.sql` has failed-run, stale-table, and RAW-vs-SILVER
  reconciliation queries. The orchestrator exits non-zero on any failure, so a
  scheduler can alert.

---

## 13. Scale considerations & known limits

The extractor and reconciler run on the **local host**, so memory there is the
main constraint at very large scale:

- **Incremental extraction memory**: `extractor_incremental.py` uses `connectorx`,
  which materializes the **entire delta in memory** (Arrow) before writing Parquet.
  A very large incremental spike (e.g. tens of millions of changed rows in one
  window) can trigger an out-of-memory error on the host.
  - Note: `rows_per_file` **splits the already-in-memory table** — it improves
    Snowflake COPY parallelism but does **not** reduce peak extraction memory.
  - Mitigations: run incrementals on a smaller cadence (smaller windows), give the
    host enough RAM, or — for known huge backfills — use a **full** load (mysqlsh
    streams to disk) instead of one giant incremental.

- **Reconciliation memory/transfer**: `reconciler.py` pulls the **full set of
  primary keys** from both MySQL and Snowflake into local memory to diff them.
  For tables with hundreds of millions of rows this is a large transfer and
  memory footprint, and runtime grows with table size.
  - Mitigations: only enable `reconcile: true` on tables that actually need
    delete detection; run reconcile on a less frequent cadence (e.g. weekly); or
    keep delete-sensitive large tables on **full** load (deletes propagate for free
    via the truncate+reload).

These are batch-on-host realities, not correctness issues. For very large
workloads, a future enhancement could chunk extraction by key/time ranges and
push the reconcile diff into Snowflake as a set-based anti-join.
