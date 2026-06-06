-- ============================================================================
-- monitoring.sql — health checks for the MySQL -> Snowflake migration.
-- All read-only. Run ad hoc, or wrap #1 in a Task + notification for alerting.
-- Namespace: audit in MIGRATION_DB.META.RUN_LOG; data in <SOURCE_DB>_RAW /
--            <SOURCE_DB>_SILVER schemas under MIGRATION_DB.
-- ============================================================================

-- ─── 1. Failed runs in the last 24h (the alert query) ───────────────────────
-- Wrap this in a scheduled Task; if it returns rows, send a notification.
SELECT BATCH_ID, SOURCE_DB, SOURCE_TABLE, TARGET_TABLE, LOAD_TYPE, ENGINE,
       STATUS, ERROR_MESSAGE, RUN_START_UTC, RUN_END_UTC
FROM MIGRATION_DB.META.RUN_LOG
WHERE STATUS = 'failed'
  AND INSERTED_AT > DATEADD('hour', -24, CURRENT_TIMESTAMP())
ORDER BY INSERTED_AT DESC;

-- ─── 2. Latest run per table (current state at a glance) ────────────────────
SELECT SOURCE_DB, TARGET_TABLE, LOAD_TYPE, ENGINE, STATUS,
       ROWS_EXTRACTED, ROWS_RAW, WATERMARK_FROM, WATERMARK_TO,
       RUN_END_UTC
FROM MIGRATION_DB.META.RUN_LOG
QUALIFY ROW_NUMBER() OVER (
            PARTITION BY SOURCE_DB, TARGET_TABLE
            ORDER BY INSERTED_AT DESC) = 1
ORDER BY SOURCE_DB, TARGET_TABLE;

-- ─── 3. Stale tables: no SUCCESSFUL run in the last 24h ─────────────────────
-- Adjust the interval to your schedule (e.g. -2 'hour' for hourly loads).
SELECT SOURCE_DB, TARGET_TABLE,
       MAX(CASE WHEN STATUS = 'success' THEN INSERTED_AT END) AS last_success
FROM MIGRATION_DB.META.RUN_LOG
GROUP BY SOURCE_DB, TARGET_TABLE
HAVING last_success IS NULL
    OR last_success < DATEADD('hour', -24, CURRENT_TIMESTAMP())
ORDER BY last_success NULLS FIRST;

-- ─── 4. RAW vs SILVER reconciliation (catches silent partial/failed builds) ──
-- Uses maintained ROW_COUNT from INFORMATION_SCHEMA.TABLES.
-- For PK tables SILVER <= RAW (dedupe is expected); the red flag is
-- silver_rows = 0 while raw_rows > 0 (SILVER build did not populate).
SELECT raw.TABLE_SCHEMA                       AS raw_schema,
       raw.TABLE_NAME                         AS table_name,
       raw.ROW_COUNT                          AS raw_rows,
       slv.ROW_COUNT                          AS silver_rows,
       raw.ROW_COUNT - slv.ROW_COUNT          AS dedupe_diff,
       CASE WHEN raw.ROW_COUNT > 0 AND slv.ROW_COUNT = 0
            THEN 'CHECK: SILVER empty' END    AS flag
FROM MIGRATION_DB.INFORMATION_SCHEMA.TABLES raw
JOIN MIGRATION_DB.INFORMATION_SCHEMA.TABLES slv
  ON LEFT(raw.TABLE_SCHEMA, LENGTH(raw.TABLE_SCHEMA) - 4)   -- strip '_RAW'
   = LEFT(slv.TABLE_SCHEMA, LENGTH(slv.TABLE_SCHEMA) - 7)   -- strip '_SILVER'
 AND raw.TABLE_NAME = slv.TABLE_NAME
WHERE raw.TABLE_SCHEMA LIKE '%\_RAW'    ESCAPE '\'
  AND slv.TABLE_SCHEMA LIKE '%\_SILVER' ESCAPE '\'
  AND raw.TABLE_TYPE = 'BASE TABLE'
  AND raw.TABLE_NAME NOT LIKE '%\_STAGE\_TMP' ESCAPE '\'    -- skip temp tables
ORDER BY flag NULLS LAST, dedupe_diff DESC;

-- ─── 5. Error frequency (recurring failure modes) ───────────────────────────
SELECT LEFT(ERROR_MESSAGE, 120) AS error_prefix,
       COUNT(*)                 AS occurrences,
       MAX(INSERTED_AT)         AS last_seen
FROM MIGRATION_DB.META.RUN_LOG
WHERE STATUS = 'failed' AND ERROR_MESSAGE IS NOT NULL
GROUP BY error_prefix
ORDER BY occurrences DESC;

-- ─── 6. (Optional) Watermark drift — per-table template ─────────────────────
-- The watermark column differs per table, so this can't be fully generic.
-- Example for company.title (watermark_col = AFFECTED_FROM):
--   SELECT 'COMPANY.TITLE' AS tbl,
--          (SELECT WATERMARK_TO FROM MIGRATION_DB.META.RUN_LOG
--             WHERE TARGET_TABLE='TITLE' AND SOURCE_DB='company'
--             QUALIFY ROW_NUMBER() OVER (ORDER BY INSERTED_AT DESC)=1) AS logged_wm,
--          (SELECT TO_VARCHAR(MAX("AFFECTED_FROM"))
--             FROM MIGRATION_DB.COMPANY_RAW.TITLE)                     AS actual_max;
