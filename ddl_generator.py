"""ddl_generator.py — Generate Snowflake RAW & SILVER DDL from MySQL metadata.

Reads MySQL information_schema.columns for each configured table, maps MySQL
types to Snowflake types, and creates:
  - MIGRATION_DB.RAW.<table>     typed business columns + audit columns
  - MIGRATION_DB.SILVER.<table>  typed business columns only

Column order is preserved (ORDINAL_POSITION) so the TSV/CSV full-load path
loads correctly (CSV has no MATCH_BY_COLUMN_NAME).
"""
from __future__ import annotations

import mysql.connector

DB = "MIGRATION_DB"

# Audit columns appended to every RAW table (not present in SILVER).
RAW_AUDIT_COLS = [
    ("_LOAD_TS", "TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()"),
    ("_SRC_FILE", "VARCHAR"),
    ("_BATCH_ID", "VARCHAR"),
    ("_IS_DELETED", "BOOLEAN DEFAULT FALSE"),
    ("_DELETED_AT", "TIMESTAMP_NTZ"),
]


def schema_names(source_db: str):
    """Map a MySQL schema name to its Snowflake RAW/SILVER schema pair."""
    base = source_db.strip().upper()
    return f"{base}_RAW", f"{base}_SILVER"


def scd2_schema(source_db: str) -> str:
    """Per-source SCD2 dimension schema name."""
    return f"{source_db.strip().upper()}_SCD2"


# SCD2 dimension control columns (appended after the business columns).
SCD2_CONTROL_COLS = [
    ("DIM_KEY", "NUMBER AUTOINCREMENT START 1 INCREMENT 1"),
    ("EFF_FROM", "TIMESTAMP_NTZ"),
    ("EFF_TO", "TIMESTAMP_NTZ"),
    ("IS_CURRENT", "BOOLEAN DEFAULT TRUE"),
    ("IS_DELETED", "BOOLEAN DEFAULT FALSE"),
    ("ROW_HASH", "NUMBER"),
    ("_LOAD_TS", "TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()"),
    ("_BATCH_ID", "VARCHAR"),
]


def map_mysql_type(data_type: str, col_type: str, num_prec, num_scale,
                   char_len) -> str:
    """Map a MySQL column type to a Snowflake type."""
    dt = (data_type or "").lower()
    ct = (col_type or "").lower()

    if dt in ("tinyint",) and "tinyint(1)" in ct:
        return "BOOLEAN"
    if dt in ("tinyint", "smallint", "mediumint", "int", "integer", "bigint",
              "year"):
        return "NUMBER(38,0)"
    if dt in ("decimal", "numeric"):
        p = int(num_prec) if num_prec is not None else 38
        s = int(num_scale) if num_scale is not None else 0
        return f"NUMBER({p},{s})"
    if dt in ("float", "double", "real"):
        return "FLOAT"
    if dt in ("datetime", "timestamp"):
        return "TIMESTAMP_NTZ"
    if dt == "date":
        return "DATE"
    if dt == "time":
        return "TIME"
    if dt == "json":
        return "VARIANT"
    if dt in ("blob", "tinyblob", "mediumblob", "longblob", "binary",
              "varbinary"):
        return "BINARY"
    if dt in ("char", "varchar"):
        n = int(char_len) if char_len else 16777216
        return f"VARCHAR({n})"
    # text/enum/set/geometry and anything else -> VARCHAR (max).
    return "VARCHAR(16777216)"


def get_mysql_columns(mysql_conn, source_db: str, source_table: str):
    """Return ordered list of (NAME, snowflake_type) for a MySQL table.

    Column names are UPPERCASED so Snowflake stores them as conventional
    uppercase identifiers (queryable without quotes). MySQL column names are
    case-insensitive, so uppercasing is safe for the extract side too.
    """
    cur = mysql_conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE,
               NUMERIC_PRECISION, NUMERIC_SCALE, CHARACTER_MAXIMUM_LENGTH
        FROM information_schema.columns
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (source_db, source_table),
    )
    cols = []
    for r in cur.fetchall():
        sf_type = map_mysql_type(
            r["DATA_TYPE"], r["COLUMN_TYPE"], r["NUMERIC_PRECISION"],
            r["NUMERIC_SCALE"], r["CHARACTER_MAXIMUM_LENGTH"],
        )
        cols.append((r["COLUMN_NAME"].upper(), sf_type))
    cur.close()
    if not cols:
        raise ValueError(
            f"No columns found for {source_db}.{source_table} — check name/grants."
        )
    return cols


def build_raw_ddl(raw_schema: str, target_table: str, cols) -> str:
    col_defs = [f'    "{name}" {sf_type}' for name, sf_type in cols]
    col_defs += [f'    "{name}" {sf_type}' for name, sf_type in RAW_AUDIT_COLS]
    body = ",\n".join(col_defs)
    return (
        f"CREATE TABLE IF NOT EXISTS {DB}.{raw_schema}.{target_table} (\n"
        f"{body}\n);"
    )


def build_silver_ddl(silver_schema: str, target_table: str, cols) -> str:
    col_defs = [f'    "{name}" {sf_type}' for name, sf_type in cols]
    body = ",\n".join(col_defs)
    return (
        f"CREATE TABLE IF NOT EXISTS {DB}.{silver_schema}.{target_table} (\n"
        f"{body}\n);"
    )


def build_scd2_ddl(sc_schema: str, target_table: str, cols) -> str:
    """SCD2 dimension: surrogate key + business cols + version control cols."""
    dim_key = SCD2_CONTROL_COLS[0]
    control = SCD2_CONTROL_COLS[1:]
    col_defs = [f'    "{dim_key[0]}" {dim_key[1]}']
    col_defs += [f'    "{name}" {sf_type}' for name, sf_type in cols]
    col_defs += [f'    "{name}" {sf_type}' for name, sf_type in control]
    body = ",\n".join(col_defs)
    return (
        f"CREATE TABLE IF NOT EXISTS {DB}.{sc_schema}.{target_table} (\n"
        f"{body}\n);"
    )


def build_scd2_current_view(sc_schema: str, target_table: str, cols) -> str:
    """A convenience view exposing only the current version of each key."""
    col_list = ", ".join(f'"{name}"' for name, _ in cols)
    return (
        f"CREATE OR REPLACE VIEW {DB}.{sc_schema}.{target_table}_CURRENT AS\n"
        f"SELECT {col_list}, \"EFF_FROM\"\n"
        f"FROM {DB}.{sc_schema}.{target_table}\n"
        f"WHERE \"IS_CURRENT\" = TRUE;"
    )


def generate_and_apply(sf_conn, mysql_conn, tbl: dict) -> dict:
    """Generate + execute DDL for one table.

    Standard tables get RAW + SILVER; tables with table_type='scd2' get
    RAW + a <DB>_SCD2 dimension (versioned). Returns the ordered
    MySQL->Snowflake column list (used by the full-load column projection).
    """
    cols = get_mysql_columns(mysql_conn, tbl["source_db"], tbl["source_table"])
    raw_schema, silver_schema = schema_names(tbl["source_db"])
    is_scd2 = tbl.get("table_type") == "scd2"

    cur = sf_conn.cursor()
    try:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {DB}.{raw_schema}")
        cur.execute(build_raw_ddl(raw_schema, tbl["target_table"], cols))
        if is_scd2:
            sc = scd2_schema(tbl["source_db"])
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {DB}.{sc}")
            cur.execute(build_scd2_ddl(sc, tbl["target_table"], cols))
            cur.execute(build_scd2_current_view(sc, tbl["target_table"], cols))
            print(f"   DDL ready: {raw_schema}.{tbl['target_table']} + "
                  f"{sc}.{tbl['target_table']} [SCD2] (+_CURRENT view, "
                  f"{len(cols)} cols)")
        else:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {DB}.{silver_schema}")
            cur.execute(build_silver_ddl(silver_schema, tbl["target_table"], cols))
            print(f"   DDL ready: {raw_schema}.{tbl['target_table']} + "
                  f"{silver_schema}.{tbl['target_table']} ({len(cols)} cols)")
    finally:
        cur.close()

    return {"columns": cols}


if __name__ == "__main__":
    import json
    import os
    import snowflake.connector

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    with open("migration_config.json") as f:
        cfg = json.load(f)

    # Connections come purely from the environment (.env); the config file no
    # longer stores source/snowflake credential blocks.
    myc = mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
    )
    sfc = snowflake.connector.connect(
        account=os.getenv("SF_ACCOUNT"),
        user=os.getenv("SF_USER"),
        password=os.getenv("SF_PASSWORD"),
        role=os.getenv("SF_ROLE"),
        warehouse=os.getenv("SF_WAREHOUSE"),
        database=os.getenv("SF_DATABASE", "MIGRATION_DB"),
        schema=os.getenv("SF_SCHEMA", "META"),
    )
    try:
        for t in cfg["tables"]:
            if t.get("active", True):
                generate_and_apply(sfc, myc, t)
    finally:
        myc.close()
        sfc.close()
