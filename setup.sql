-- ============================================================================
-- MySQL -> Snowflake Migration Tool  |  Snowflake-side setup (idempotent)
-- Namespace (Option B): one database MIGRATION_DB; each MySQL schema maps to its
-- own RAW/SILVER pair (MIGRATION_DB.<SOURCE_DB>_RAW / <SOURCE_DB>_SILVER),
-- created at runtime by ddl_generator.py. Shared objects live in META.
-- Creates: MIGRATION_DB, META schema, internal stage, PARQUET + TSV file
--          formats, META.RUN_LOG, META.USP_BUILD_SILVER.
-- ============================================================================

-- ─── Database & shared schema ───────────────────────────────────────────────
CREATE DATABASE IF NOT EXISTS MIGRATION_DB;
CREATE SCHEMA   IF NOT EXISTS MIGRATION_DB.META;
-- Per-source <SCHEMA>_RAW / <SCHEMA>_SILVER schemas are created by ddl_generator.py.

-- ─── Internal stage (extractor PUTs files here) — shared, in META ───────────
CREATE STAGE IF NOT EXISTS MIGRATION_DB.META.MIGRATION_STAGE
    DIRECTORY = (ENABLE = TRUE)
    COMMENT   = 'Landing stage for MySQL extracts (parquet + tsv.zst)';

-- ─── File formats (shared, in META) ─────────────────────────────────────────
-- Parquet: used by the connectorx incremental path (self-describing schema).
-- USE_LOGICAL_TYPE=TRUE makes Snowflake honor Parquet logical types so INT64
-- microsecond timestamps load into TIMESTAMP_NTZ correctly (without it, the raw
-- integer is read as seconds -> corrupted far-future dates).
CREATE OR REPLACE FILE FORMAT MIGRATION_DB.META.PARQUET_FMT
    TYPE = PARQUET
    USE_LOGICAL_TYPE = TRUE;

-- TSV+zstd: used by the mysqlsh dumpTables full-load path.
-- mysqlsh writes tab-delimited, NULL as \N, no header, zstd-compressed.
CREATE FILE FORMAT IF NOT EXISTS MIGRATION_DB.META.TSV_ZSTD_FMT
    TYPE             = CSV
    FIELD_DELIMITER  = '\t'
    COMPRESSION      = ZSTD
    NULL_IF          = ('\\N')
    EMPTY_FIELD_AS_NULL = FALSE
    SKIP_HEADER      = 0
    FIELD_OPTIONALLY_ENCLOSED_BY = NONE
    ESCAPE_UNENCLOSED_FIELD = '\\';

-- ─── Audit / run log ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS MIGRATION_DB.META.RUN_LOG (
    RUN_ID          VARCHAR        DEFAULT UUID_STRING(),
    BATCH_ID        VARCHAR,
    SOURCE_DB       VARCHAR,
    SOURCE_TABLE    VARCHAR,
    TARGET_TABLE    VARCHAR,
    LOAD_TYPE       VARCHAR,                 -- full | incremental
    ENGINE          VARCHAR,                 -- mysqlsh | connectorx
    ROWS_EXTRACTED  NUMBER,
    ROWS_RAW        NUMBER,                  -- rows landed/merged into RAW
    ROWS_SILVER     NUMBER,                  -- rows merged into SILVER
    WATERMARK_FROM  VARCHAR,
    WATERMARK_TO    VARCHAR,
    STATUS          VARCHAR,                 -- success | failed | skipped
    ERROR_MESSAGE   VARCHAR,
    RUN_START_UTC   TIMESTAMP_NTZ,
    RUN_END_UTC     TIMESTAMP_NTZ,
    INSERTED_AT     TIMESTAMP_NTZ  DEFAULT CURRENT_TIMESTAMP()
);

-- ─── RAW -> SILVER builder ──────────────────────────────────────────────────
-- Dedupes <SOURCE_DB>_RAW.<table> on the primary key (latest row by watermark)
-- and MERGEs into <SOURCE_DB>_SILVER.<table>. SILVER table & business columns
-- are created beforehand by ddl_generator.py (no audit columns in SILVER).
CREATE OR REPLACE PROCEDURE MIGRATION_DB.META.USP_BUILD_SILVER(
    P_SOURCE_DB     VARCHAR,    -- MySQL schema name (drives <DB>_RAW / <DB>_SILVER)
    P_TABLE         VARCHAR,    -- target/base table name (same in RAW & SILVER)
    P_PRIMARY_KEY   VARCHAR,    -- PK column for dedupe + MERGE join
    P_WATERMARK_COL VARCHAR     -- ordering column for "latest wins" (nullable)
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    raw_schema    VARCHAR := UPPER(:P_SOURCE_DB) || '_RAW';
    silver_schema VARCHAR := UPPER(:P_SOURCE_DB) || '_SILVER';
    raw_tbl       VARCHAR := 'MIGRATION_DB.' || raw_schema    || '.' || :P_TABLE;
    silver_tbl    VARCHAR := 'MIGRATION_DB.' || silver_schema || '.' || :P_TABLE;
    order_col     VARCHAR := COALESCE(:P_WATERMARK_COL, :P_PRIMARY_KEY);
    cols_csv      VARCHAR;
    set_csv       VARCHAR;
    ins_vals      VARCHAR;
    merge_sql     VARCHAR;
    affected      NUMBER  := 0;
BEGIN
    -- Business columns = SILVER table columns (audit cols excluded by design).
    SELECT LISTAGG(COLUMN_NAME, ', ') WITHIN GROUP (ORDER BY ORDINAL_POSITION)
      INTO :cols_csv
      FROM MIGRATION_DB.INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = :silver_schema AND TABLE_NAME = :P_TABLE;

    IF (cols_csv IS NULL) THEN
        RETURN 'ERROR: SILVER table ' || :silver_tbl || ' not found or has no columns.';
    END IF;

    -- No primary key -> passthrough: full overwrite of SILVER from RAW (no dedupe).
    -- Soft-deleted RAW rows (_IS_DELETED=TRUE) are excluded so deletes propagate.
    IF (:P_PRIMARY_KEY IS NULL OR TRIM(:P_PRIMARY_KEY) = '') THEN
        EXECUTE IMMEDIATE 'TRUNCATE TABLE ' || silver_tbl;
        EXECUTE IMMEDIATE
            'INSERT INTO ' || silver_tbl || ' (' || cols_csv || ') ' ||
            'SELECT ' || cols_csv || ' FROM ' || raw_tbl ||
            ' WHERE COALESCE("_IS_DELETED", FALSE) = FALSE';
        affected := SQLROWCOUNT;
        RETURN 'SILVER passthrough OK for ' || silver_schema || '.' || :P_TABLE ||
               ' (no PK; rows inserted: ' || affected || ')';
    END IF;

    -- UPDATE SET for every non-PK column.
    SELECT LISTAGG('t.' || COLUMN_NAME || ' = s.' || COLUMN_NAME, ', ')
                WITHIN GROUP (ORDER BY ORDINAL_POSITION)
      INTO :set_csv
      FROM MIGRATION_DB.INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = :silver_schema AND TABLE_NAME = :P_TABLE
       AND UPPER(COLUMN_NAME) <> UPPER(:P_PRIMARY_KEY);

    SELECT LISTAGG('s.' || COLUMN_NAME, ', ')
                WITHIN GROUP (ORDER BY ORDINAL_POSITION)
      INTO :ins_vals
      FROM MIGRATION_DB.INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = :silver_schema AND TABLE_NAME = :P_TABLE;

    -- Dedupe RAW to the latest row per PK and carry _IS_DELETED so soft-deleted
    -- keys are removed from SILVER (Snowflake MERGE has no NOT MATCHED BY SOURCE,
    -- so we DELETE matched rows flagged deleted).
    merge_sql :=
        'MERGE INTO ' || silver_tbl || ' t USING (' ||
        '  SELECT ' || cols_csv || ', _is_del FROM (' ||
        '    SELECT ' || cols_csv || ', ' ||
        '           COALESCE("_IS_DELETED", FALSE) AS _is_del, ' ||
        '           ROW_NUMBER() OVER (PARTITION BY ' || :P_PRIMARY_KEY ||
        '             ORDER BY ' || order_col || ' DESC NULLS LAST) AS _rn' ||
        '    FROM ' || raw_tbl ||
        '  ) WHERE _rn = 1' ||
        ') s ON t.' || :P_PRIMARY_KEY || ' = s.' || :P_PRIMARY_KEY ||
        ' WHEN MATCHED AND s._is_del THEN DELETE' ||
        CASE WHEN set_csv IS NOT NULL
             THEN ' WHEN MATCHED AND NOT s._is_del THEN UPDATE SET ' || set_csv
             ELSE '' END ||
        ' WHEN NOT MATCHED AND NOT s._is_del THEN INSERT (' || cols_csv ||
        ') VALUES (' || ins_vals || ')';

    EXECUTE IMMEDIATE :merge_sql;
    affected := SQLROWCOUNT;

    RETURN 'SILVER build OK for ' || silver_schema || '.' || :P_TABLE ||
           ' (rows affected: ' || affected || ')';
END;
$$;

-- ─── RAW -> SCD2 dimension builder ──────────────────────────────────────────
-- Builds a Type-2 slowly-changing dimension in <SOURCE_DB>_SCD2.<table> from
-- the latest-per-PK snapshot in <SOURCE_DB>_RAW.<table>:
--   * changed attributes (ROW_HASH differs)  -> expire current version + insert new
--   * new keys                                -> insert as current
--   * soft-deleted keys (_IS_DELETED=TRUE)    -> expire current version (no new insert)
-- EFF_FROM uses the watermark (event time) when present, else CURRENT_TIMESTAMP.
-- The SCD2 table (DIM_KEY, business cols, EFF_FROM/EFF_TO/IS_CURRENT/IS_DELETED/
-- ROW_HASH/_LOAD_TS/_BATCH_ID) is created beforehand by ddl_generator.py.
CREATE OR REPLACE PROCEDURE MIGRATION_DB.META.USP_BUILD_SCD2(
    P_SOURCE_DB     VARCHAR,    -- MySQL schema -> <DB>_RAW / <DB>_SCD2
    P_TABLE         VARCHAR,    -- table name (same in RAW & SCD2)
    P_PRIMARY_KEY   VARCHAR,    -- business key
    P_WATERMARK_COL VARCHAR,    -- event-time column (nullable -> load time)
    P_TRACK_COLS    VARCHAR     -- comma list of tracked cols (nullable -> all non-PK)
)
RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS OWNER
AS
$$
DECLARE
    raw_schema  VARCHAR := UPPER(:P_SOURCE_DB) || '_RAW';
    scd2_schema VARCHAR := UPPER(:P_SOURCE_DB) || '_SCD2';
    raw_tbl     VARCHAR := 'MIGRATION_DB.' || raw_schema  || '.' || :P_TABLE;
    scd2_tbl    VARCHAR := 'MIGRATION_DB.' || scd2_schema || '.' || :P_TABLE;
    order_col   VARCHAR := COALESCE(:P_WATERMARK_COL, :P_PRIMARY_KEY);
    cols_csv    VARCHAR;       -- business columns of the SCD2 table
    track_csv   VARCHAR;       -- columns to hash for change detection
    ct_expr     VARCHAR;       -- change-time expression (event vs load time)
    src_sql     VARCHAR;
    expired     NUMBER := 0;
    inserted    NUMBER := 0;
    high_date   VARCHAR := '9999-12-31 00:00:00';
BEGIN
    -- Business columns = SCD2 columns minus control/audit columns.
    SELECT LISTAGG('"' || COLUMN_NAME || '"', ', ')
                WITHIN GROUP (ORDER BY ORDINAL_POSITION)
      INTO :cols_csv
      FROM MIGRATION_DB.INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = :scd2_schema AND TABLE_NAME = :P_TABLE
       AND COLUMN_NAME NOT IN ('DIM_KEY','EFF_FROM','EFF_TO','IS_CURRENT',
                               'IS_DELETED','ROW_HASH','_LOAD_TS','_BATCH_ID');

    IF (cols_csv IS NULL) THEN
        RETURN 'ERROR: SCD2 table ' || :scd2_tbl || ' not found or has no columns.';
    END IF;

    -- Tracked columns for the hash: explicit list, else all non-PK business cols.
    IF (:P_TRACK_COLS IS NOT NULL AND TRIM(:P_TRACK_COLS) <> '') THEN
        track_csv := :P_TRACK_COLS;
    ELSE
        SELECT LISTAGG('"' || COLUMN_NAME || '"', ', ')
                    WITHIN GROUP (ORDER BY ORDINAL_POSITION)
          INTO :track_csv
          FROM MIGRATION_DB.INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_SCHEMA = :scd2_schema AND TABLE_NAME = :P_TABLE
           AND COLUMN_NAME NOT IN ('DIM_KEY','EFF_FROM','EFF_TO','IS_CURRENT',
                                   'IS_DELETED','ROW_HASH','_LOAD_TS','_BATCH_ID')
           AND UPPER(COLUMN_NAME) <> UPPER(:P_PRIMARY_KEY);
    END IF;
    IF (track_csv IS NULL OR TRIM(track_csv) = '') THEN
        track_csv := cols_csv;  -- nothing else to track; hash all business cols
    END IF;

    ct_expr := CASE
        WHEN :P_WATERMARK_COL IS NOT NULL
        THEN 'COALESCE(TRY_TO_TIMESTAMP_NTZ(TO_VARCHAR("' || :P_WATERMARK_COL ||
             '")), CURRENT_TIMESTAMP())'
        ELSE 'CURRENT_TIMESTAMP()' END;

    -- Latest row per PK from RAW, carrying delete flag, change time, and hash.
    src_sql :=
        'SELECT ' || cols_csv || ', _is_del, _del_at, _ct, _hash FROM (' ||
        '  SELECT ' || cols_csv || ', ' ||
        '    COALESCE("_IS_DELETED", FALSE) AS _is_del, ' ||
        '    "_DELETED_AT" AS _del_at, ' ||
        '    ' || ct_expr || ' AS _ct, ' ||
        '    HASH(' || track_csv || ') AS _hash, ' ||
        '    ROW_NUMBER() OVER (PARTITION BY "' || :P_PRIMARY_KEY || '" ' ||
        '      ORDER BY ' || order_col || ' DESC NULLS LAST) AS _rn ' ||
        '  FROM ' || raw_tbl ||
        ') WHERE _rn = 1';

    -- 1) Expire current versions for changed or deleted keys.
    EXECUTE IMMEDIATE
        'UPDATE ' || scd2_tbl || ' t ' ||
        'SET EFF_TO = CASE WHEN s._is_del ' ||
        '                  THEN COALESCE(s._del_at, CURRENT_TIMESTAMP()) ' ||
        '                  ELSE s._ct END, ' ||
        '    IS_CURRENT = FALSE, ' ||
        '    IS_DELETED = (IS_DELETED OR s._is_del) ' ||
        'FROM (' || src_sql || ') s ' ||
        'WHERE t."' || :P_PRIMARY_KEY || '" = s."' || :P_PRIMARY_KEY || '" ' ||
        '  AND t.IS_CURRENT = TRUE ' ||
        '  AND (s._is_del OR t.ROW_HASH <> s._hash)';
    expired := SQLROWCOUNT;

    -- 2) Insert new current versions for new + changed (non-deleted) keys.
    EXECUTE IMMEDIATE
        'INSERT INTO ' || scd2_tbl || ' (' || cols_csv ||
        ', EFF_FROM, EFF_TO, IS_CURRENT, IS_DELETED, ROW_HASH, "_LOAD_TS", "_BATCH_ID") ' ||
        'SELECT ' || cols_csv || ', s._ct, ' ||
        '       TO_TIMESTAMP_NTZ(''' || high_date || '''), TRUE, FALSE, s._hash, ' ||
        '       CURRENT_TIMESTAMP(), NULL ' ||
        'FROM (' || src_sql || ') s ' ||
        'WHERE NOT s._is_del ' ||
        '  AND NOT EXISTS (SELECT 1 FROM ' || scd2_tbl || ' t ' ||
        '    WHERE t."' || :P_PRIMARY_KEY || '" = s."' || :P_PRIMARY_KEY || '" ' ||
        '      AND t.IS_CURRENT = TRUE)';
    inserted := SQLROWCOUNT;

    RETURN 'SCD2 build OK for ' || scd2_schema || '.' || :P_TABLE ||
           ' (expired: ' || expired || ', new versions: ' || inserted || ')';
END;
$$;
