"""run_log.py — Write one audit row per table per run to META.RUN_LOG."""
from __future__ import annotations


def write_run_log(cur, rec: dict):
    """Insert an audit record. `rec` keys map to RUN_LOG columns."""
    cur.execute(
        """
        INSERT INTO MIGRATION_DB.META.RUN_LOG
            (BATCH_ID, SOURCE_DB, SOURCE_TABLE, TARGET_TABLE, LOAD_TYPE,
             ENGINE, ROWS_EXTRACTED, ROWS_RAW, ROWS_SILVER,
             WATERMARK_FROM, WATERMARK_TO, STATUS, ERROR_MESSAGE,
             RUN_START_UTC, RUN_END_UTC)
        SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
               TO_TIMESTAMP_NTZ(%s), TO_TIMESTAMP_NTZ(%s)
        """,
        (
            rec.get("batch_id"), rec.get("source_db"), rec.get("source_table"),
            rec.get("target_table"), rec.get("load_type"), rec.get("engine"),
            rec.get("rows_extracted"), rec.get("rows_raw"),
            rec.get("rows_silver"), rec.get("watermark_from"),
            rec.get("watermark_to"), rec.get("status"),
            rec.get("error_message"), rec.get("run_start_utc"),
            rec.get("run_end_utc"),
        ),
    )
