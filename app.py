"""app.py — MySQL → Snowflake Migration Control Panel (improved UI/UX).

Start with:  streamlit run app.py

Local app: reaches localhost MySQL and shells out to orchestrator.py /
config_generator.py, reusing the same .env / migration_config.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "migration_config.json"

# ─── Page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MySQL → Snowflake",
    page_icon="❄️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Global CSS ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Syne:wght@400;600;800&display=swap');
html, body, [class*="css"] { font-family: 'Syne', sans-serif; }
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
.metric-card { background:#0f1923; border:1px solid #1e3a5f; border-radius:10px; padding:18px 22px; margin-bottom:12px; }
.metric-card .label { font-size:0.72rem; letter-spacing:2px; text-transform:uppercase; color:#4a7fa5; margin-bottom:4px; }
.metric-card .value { font-family:'JetBrains Mono',monospace; font-size:2rem; font-weight:600; color:#e8f4fd; line-height:1; }
.metric-card .sub { font-size:0.72rem; color:#4a7fa5; margin-top:4px; }
.table-card { background:#0f1923; border:1px solid #1e3a5f; border-radius:10px; padding:16px 20px; margin-bottom:10px; position:relative; overflow:hidden; }
.table-card::before { content:''; position:absolute; left:0; top:0; bottom:0; width:3px; }
.table-card.success::before { background:#00c896; }
.table-card.failed::before { background:#ff4b6e; }
.table-card.skipped::before { background:#f59e0b; }
.table-card.pending::before { background:#4a7fa5; }
.table-card .tname { font-weight:600; font-size:0.95rem; color:#e8f4fd; }
.table-card .tmeta { font-family:'JetBrains Mono',monospace; font-size:0.72rem; color:#4a7fa5; margin-top:2px; }
.table-card .tstatus { font-size:0.7rem; font-weight:600; letter-spacing:1px; text-transform:uppercase; padding:2px 8px; border-radius:4px; float:right; }
.tstatus.success { background:#003d2e; color:#00c896; }
.tstatus.failed { background:#3d0014; color:#ff4b6e; }
.tstatus.skipped { background:#3d2800; color:#f59e0b; }
.tstatus.pending { background:#0a1929; color:#4a7fa5; }
.log-box { background:#060d14; border:1px solid #1e3a5f; border-radius:8px; padding:16px; font-family:'JetBrains Mono',monospace; font-size:0.75rem; color:#7eb8d4; height:340px; overflow-y:auto; white-space:pre-wrap; line-height:1.7; }
.log-box .log-ok { color:#00c896; }
.log-box .log-err { color:#ff4b6e; }
.log-box .log-warn { color:#f59e0b; }
.log-box .log-info { color:#a78bfa; }
.section-header { font-size:0.68rem; letter-spacing:3px; text-transform:uppercase; color:#4a7fa5; margin-bottom:12px; padding-bottom:6px; border-bottom:1px solid #1e3a5f; }
.pill { display:inline-block; padding:2px 10px; border-radius:20px; font-size:0.68rem; font-weight:600; letter-spacing:1px; text-transform:uppercase; }
.pill-full { background:#0a2540; color:#4a7fa5; }
.pill-incr { background:#003d2e; color:#00c896; }
section[data-testid="stSidebar"] { background:#060d14; border-right:1px solid #1e3a5f; }
.stButton > button { background:#0a2540; color:#4a9fd4; border:1px solid #1e3a5f; border-radius:6px; font-family:'JetBrains Mono',monospace; font-size:0.8rem; letter-spacing:1px; transition:all 0.2s; }
.stButton > button:hover { background:#1e3a5f; color:#e8f4fd; border-color:#4a7fa5; }
</style>
""", unsafe_allow_html=True)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _default_config() -> dict:
    """Skeleton config (connections from env) used when the file is missing,
    empty, or invalid — keeps the app usable so config can be regenerated."""
    return {
        "source": {
            "host": os.getenv("MYSQL_HOST", "localhost"),
            "port": int(os.getenv("MYSQL_PORT", "3306")),
            "user": os.getenv("MYSQL_USER", "root"),
            "password": os.getenv("MYSQL_PASSWORD", "YOUR_MYSQL_PASSWORD"),
        },
        "snowflake": {
            "account": os.getenv("SF_ACCOUNT", ""),
            "user": os.getenv("SF_USER", ""),
            "password": os.getenv("SF_PASSWORD", "YOUR_SF_PASSWORD"),
            "role": os.getenv("SF_ROLE", "ACCOUNTADMIN"),
            "warehouse": os.getenv("SF_WAREHOUSE", "COMPUTE_WH"),
            "database": os.getenv("SF_DATABASE", "MIGRATION_DB"),
            "schema": os.getenv("SF_SCHEMA", "META"),
        },
        "export_dir": "./export",
        "tables": [],
    }


def load_config() -> dict:
    """Load migration_config.json; fall back to a default skeleton if the file
    is missing, empty, or not valid JSON (so the app never hard-crashes)."""
    if not CONFIG_PATH.exists() or CONFIG_PATH.stat().st_size == 0:
        st.warning(f"{CONFIG_PATH.name} is missing or empty — using defaults. "
                   "Generate config in the Run tab.")
        return _default_config()
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        st.error(f"{CONFIG_PATH.name} is not valid JSON ({e}). Using defaults "
                 "— fix or regenerate the file before saving.")
        return _default_config()


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def sf_conf(cfg: dict) -> dict:
    out = dict(cfg["snowflake"])
    env_map = {"account": "SF_ACCOUNT", "user": "SF_USER",
               "password": "SF_PASSWORD", "role": "SF_ROLE",
               "warehouse": "SF_WAREHOUSE", "database": "SF_DATABASE",
               "schema": "SF_SCHEMA"}
    for k, e in env_map.items():
        if os.getenv(e):
            out[k] = os.getenv(e)
    return out


def my_conf(cfg: dict) -> dict:
    out = dict(cfg["source"])
    env_map = {"host": "MYSQL_HOST", "port": "MYSQL_PORT",
               "user": "MYSQL_USER", "password": "MYSQL_PASSWORD"}
    for k, e in env_map.items():
        if os.getenv(e):
            out[k] = int(os.getenv(e)) if k == "port" else os.getenv(e)
    return out


def get_sf(cfg):
    import snowflake.connector
    return snowflake.connector.connect(**sf_conf(cfg))


def get_mysql(cfg):
    import mysql.connector
    c = my_conf(cfg)
    return mysql.connector.connect(
        host=c["host"], port=int(c["port"]), user=c["user"], password=c["password"])


def status_icon(status):
    return {"success": "✅", "failed": "❌", "skipped": "⏭️",
            "running": "⚡", None: "⏳"}.get(status, "⏳")


def load_type_pill(lt):
    cls = {"full": "pill-full", "incremental": "pill-incr"}.get(lt, "pill-full")
    return f'<span class="pill {cls}">{lt}</span>'


def colorize_log(line):
    low = line.lower()
    if any(x in low for x in ["✅", "done", "success", "complete", " ok"]):
        return f'<span class="log-ok">{line}</span>'
    if any(x in low for x in ["❌", "failed", "error", "exception"]):
        return f'<span class="log-err">{line}</span>'
    if any(x in low for x in ["⚠️", "warn", "skip", "no new"]):
        return f'<span class="log-warn">{line}</span>'
    if any(x in low for x in ["[full", "[incr", "[recon", "batch", "===="]):
        return f'<span class="log-info">{line}</span>'
    return line


def run_subprocess_stream(args):
    """Stream subprocess output into a log box; return (rc, full_output)."""
    log_area = st.empty()
    lines = []
    proc = subprocess.Popen(
        [sys.executable, *args], cwd=str(HERE),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for raw in proc.stdout:
        lines.append(colorize_log(raw.rstrip()))
        html = "\n".join(lines[-60:])
        log_area.markdown(f'<div class="log-box">{html}</div>',
                          unsafe_allow_html=True)
    proc.wait()
    return proc.returncode, "\n".join(lines)


# ─── Sidebar ─────────────────────────────────────────────────────────────────
cfg = load_config()

with st.sidebar:
    st.markdown("## ❄️ Migration Hub")
    st.markdown("---")
    st.markdown('<div class="section-header">Connections</div>',
                unsafe_allow_html=True)
    if st.button("Test Connections", use_container_width=True):
        with st.spinner("Checking…"):
            try:
                con = get_mysql(cfg); cur = con.cursor()
                cur.execute("SELECT VERSION()")
                st.success(f"MySQL {cur.fetchone()[0][:12]}")
                cur.close(); con.close()
            except Exception as e:  # noqa: BLE001
                st.error(f"MySQL ✗ {str(e)[:60]}")
            try:
                con = get_sf(cfg); cur = con.cursor()
                cur.execute("SELECT CURRENT_ACCOUNT(), CURRENT_WAREHOUSE()")
                acct, _wh = cur.fetchone()
                st.success(f"Snowflake: {acct}")
                cur.close(); con.close()
            except Exception as e:  # noqa: BLE001
                st.error(f"Snowflake ✗ {str(e)[:60]}")

    st.markdown("---")
    st.markdown('<div class="section-header">Quick Info</div>',
                unsafe_allow_html=True)
    active_tables = [t for t in cfg.get("tables", []) if t.get("active", True)]
    full_tbls = sum(1 for t in active_tables if t.get("load_type") == "full")
    incr_tbls = sum(1 for t in active_tables if t.get("load_type") == "incremental")
    never_run = sum(1 for t in active_tables if not t.get("last_loaded_at"))
    st.markdown(f"""
        <div class="metric-card">
          <div class="label">Active Tables</div>
          <div class="value">{len(active_tables)}</div>
          <div class="sub">{full_tbls} full · {incr_tbls} incremental</div>
        </div>
        <div class="metric-card">
          <div class="label">Never Run</div>
          <div class="value">{never_run}</div>
          <div class="sub">pending first load</div>
        </div>
    """, unsafe_allow_html=True)
    failed_count = sum(1 for t in active_tables
                       if t.get("last_run_status") == "failed")
    if failed_count:
        st.error(f"⚠️ {failed_count} table(s) failed last run")
    st.markdown("---")
    st.caption(f"Account: `{sf_conf(cfg).get('account', '—')}`")
    st.caption(f"Config: `{CONFIG_PATH.name}`")

# ─── Main area ───────────────────────────────────────────────────────────────
st.markdown("# MySQL → Snowflake")
st.markdown("---")

tab_dash, tab_run, tab_config, tab_hist, tab_counts = st.tabs(
    ["📊 Dashboard", "▶️ Run", "⚙️ Config", "📜 History", "🔢 Counts"])

# ── TAB 1 — DASHBOARD ────────────────────────────────────────────────────────
with tab_dash:
    st.markdown('<div class="section-header">Table Status Overview</div>',
                unsafe_allow_html=True)
    tables = cfg.get("tables", [])
    if not tables:
        st.info("No tables configured. Generate config in the Run tab.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        success_n = sum(1 for t in tables if t.get("last_run_status") == "success")
        failed_n = sum(1 for t in tables if t.get("last_run_status") == "failed")
        pending_n = sum(1 for t in tables if not t.get("last_run_status"))
        skipped_n = sum(1 for t in tables if t.get("last_run_status") == "skipped")
        for col, label, val, color in [
            (c1, "SUCCESS", success_n, "#00c896"),
            (c2, "FAILED", failed_n, "#ff4b6e"),
            (c3, "PENDING", pending_n, "#4a7fa5"),
            (c4, "SKIPPED", skipped_n, "#f59e0b"),
        ]:
            col.markdown(f"""<div class="metric-card" style="border-color:{color}33">
                <div class="label">{label}</div>
                <div class="value" style="color:{color}">{val}</div>
                <div class="sub">tables</div></div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="section-header">Per-Table Status</div>',
                    unsafe_allow_html=True)
        cols = st.columns(2)
        for i, tbl in enumerate(tables):
            status = tbl.get("last_run_status") or "pending"
            wm = tbl.get("last_loaded_at")
            wm_display = f"Last sync: {wm[:19]}" if wm else "Never run"
            active = tbl.get("active", True)
            inactive_badge = (
                "" if active
                else '<span class="pill" style="background:#1a1a1a;color:#555">INACTIVE</span>')
            scd2_badge = (
                '<span class="pill" style="background:#1e1040;color:#a78bfa">SCD2</span>'
                if tbl.get("table_type") == "scd2" else "")
            with cols[i % 2]:
                st.markdown(f"""
                    <div class="table-card {status}">
                      <span class="tstatus {status}">{status_icon(status)} {status.upper()}</span>
                      <div class="tname">{tbl['source_db']}.{tbl['source_table']}</div>
                      <div class="tmeta">→ {tbl['target_table']} &nbsp;·&nbsp;
                        {load_type_pill(tbl.get('load_type', 'full'))} {scd2_badge} {inactive_badge}</div>
                      <div class="tmeta" style="margin-top:6px">🕐 {wm_display}
                        &nbsp;·&nbsp; PK: {tbl.get('primary_key', '—')}</div>
                    </div>""", unsafe_allow_html=True)

# ── TAB 2 — RUN ──────────────────────────────────────────────────────────────
with tab_run:
    st.markdown('<div class="section-header">Pipeline Controls</div>',
                unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    run_clicked = c1.button("▶️ Run Migration", type="primary",
                            use_container_width=True)
    rec_clicked = c2.button("🗑️ Reconcile Deletes", use_container_width=True)
    full_clicked = c3.button("🔁 Force Full Reload", use_container_width=True)

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">Run Single Table</div>',
                unsafe_allow_html=True)
    table_options = [f"{t['source_db']}.{t['source_table']}"
                     for t in cfg.get("tables", []) if t.get("active", True)]
    selected_table = st.selectbox("Select table", ["— all tables —"] + table_options,
                                  label_visibility="collapsed")
    single_clicked = st.button("▶️ Run Selected Table",
                               disabled=(selected_table == "— all tables —"))

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">Live Log</div>',
                unsafe_allow_html=True)
    if "last_log" not in st.session_state:
        st.session_state["last_log"] = "No runs yet. Click ▶️ Run Migration."

    if run_clicked:
        st.info("⚡ Migration started…")
        rc, log = run_subprocess_stream(["orchestrator.py"])
        st.session_state["last_log"] = log
        (st.success if rc == 0 else st.error)(
            f"{'✅ Completed' if rc == 0 else '❌ Failed'} (exit {rc})")
    elif rec_clicked:
        st.info("🗑️ Reconcile started…")
        rc, log = run_subprocess_stream(["orchestrator.py", "--reconcile"])
        st.session_state["last_log"] = log
        (st.success if rc == 0 else st.error)(
            f"{'✅ Done' if rc == 0 else '❌ Failed'} (exit {rc})")
    elif full_clicked:
        st.warning("⚠️ This truncates and reloads ALL active tables.")
        rc, log = run_subprocess_stream(["orchestrator.py", "--full"])
        st.session_state["last_log"] = log
        (st.success if rc == 0 else st.error)(
            f"{'✅ Done' if rc == 0 else '❌ Failed'} (exit {rc})")
    elif single_clicked and selected_table != "— all tables —":
        _db, tbl_name = selected_table.split(".", 1)
        st.info(f"▶️ Running {selected_table}…")
        rc, log = run_subprocess_stream(["orchestrator.py", "--table", tbl_name])
        st.session_state["last_log"] = log
        (st.success if rc == 0 else st.error)(
            f"{'✅ Done' if rc == 0 else '❌ Failed'} (exit {rc})")
    else:
        lines = st.session_state["last_log"].split("\n")
        colored = "\n".join(colorize_log(line) for line in lines[-60:])
        st.markdown(f'<div class="log-box">{colored}</div>',
                    unsafe_allow_html=True)

# ── TAB 3 — CONFIG EDITOR ────────────────────────────────────────────────────
with tab_config:
    st.markdown('<div class="section-header">Generate Config from Schema</div>',
                unsafe_allow_html=True)
    g1, g2 = st.columns([3, 1])
    schema_input = g1.text_input("MySQL schema name", placeholder="e.g. company",
                                 label_visibility="collapsed")
    if g2.button("⚙️ Generate", use_container_width=True) and schema_input:
        rc, _out = run_subprocess_stream(["config_generator.py", schema_input])
        if rc == 0:
            st.success(f"Config generated for schema: {schema_input}")
            st.rerun()
        else:
            st.error("Generation failed — check log above")

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">Table Configuration</div>',
                unsafe_allow_html=True)
    tables = cfg.get("tables", [])
    if not tables:
        st.info("No tables in config.")
    else:
        changed = False
        for i, tbl in enumerate(tables):
            with st.expander(
                f"{'🟢' if tbl.get('active', True) else '⚫'} "
                f"{tbl['source_db']}.{tbl['source_table']} → {tbl['target_table']}"):
                col_a, col_b, col_c = st.columns(3)
                new_load_type = col_a.selectbox(
                    "Load type", ["full", "incremental"],
                    index=["full", "incremental"].index(tbl.get("load_type", "full")),
                    key=f"lt_{i}")
                new_wm_col = col_b.text_input(
                    "Watermark column", value=tbl.get("watermark_col") or "",
                    key=f"wm_{i}")
                new_pk = col_c.text_input(
                    "Primary key", value=tbl.get("primary_key") or "", key=f"pk_{i}")
                col_d, col_e, col_f = st.columns(3)
                new_active = col_d.checkbox("Active", value=tbl.get("active", True),
                                            key=f"act_{i}")
                new_part_num = col_e.number_input(
                    "Parallel partitions", min_value=1, max_value=32,
                    value=int(tbl.get("partition_num", 4)), key=f"pn_{i}")
                new_reconcile = col_f.checkbox(
                    "Reconcile deletes", value=tbl.get("reconcile", False),
                    key=f"rec_{i}")
                if any([
                    new_load_type != tbl.get("load_type"),
                    (new_wm_col or None) != tbl.get("watermark_col"),
                    (new_pk or None) != tbl.get("primary_key"),
                    new_active != tbl.get("active", True),
                    new_part_num != tbl.get("partition_num", 4),
                    new_reconcile != tbl.get("reconcile", False),
                ]):
                    cfg["tables"][i]["load_type"] = new_load_type
                    cfg["tables"][i]["watermark_col"] = new_wm_col or None
                    cfg["tables"][i]["primary_key"] = new_pk or None
                    cfg["tables"][i]["active"] = new_active
                    cfg["tables"][i]["partition_num"] = new_part_num
                    cfg["tables"][i]["reconcile"] = new_reconcile
                    changed = True
        if changed:
            if st.button("💾 Save Changes", type="primary"):
                save_config(cfg)
                st.success("✅ migration_config.json saved")
                st.rerun()
        else:
            st.caption("No unsaved changes.")
        with st.expander("📄 View raw JSON"):
            st.json(cfg)

# ── TAB 4 — HISTORY ──────────────────────────────────────────────────────────
with tab_hist:
    st.markdown('<div class="section-header">Run History (META.RUN_LOG)</div>',
                unsafe_allow_html=True)
    c1, c2 = st.columns([1, 4])
    if c1.button("🔄 Refresh", use_container_width=True):
        st.session_state.pop("_hist", None)
    limit = c2.slider("Rows to show", 20, 500, 100)
    if "_hist" not in st.session_state:
        try:
            con = get_sf(cfg); cur = con.cursor()
            cur.execute(
                "SELECT RUN_START_UTC, BATCH_ID, SOURCE_DB, TARGET_TABLE, "
                "LOAD_TYPE, ENGINE, ROWS_EXTRACTED, ROWS_RAW, WATERMARK_FROM, "
                "WATERMARK_TO, STATUS, ERROR_MESSAGE "
                "FROM MIGRATION_DB.META.RUN_LOG "
                f"ORDER BY RUN_START_UTC DESC LIMIT {int(limit)}")
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            cur.close(); con.close()
            import pandas as pd
            st.session_state["_hist"] = pd.DataFrame(rows, columns=cols)
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not read RUN_LOG: {e}")
            st.session_state["_hist"] = None
    hist = st.session_state.get("_hist")
    if hist is not None and not hist.empty:
        st.dataframe(hist, use_container_width=True, hide_index=True)
        failed_rows = hist[hist["STATUS"] == "failed"]
        if not failed_rows.empty:
            st.markdown('<div class="section-header">❌ Failed Runs</div>',
                        unsafe_allow_html=True)
            for _, row in failed_rows.iterrows():
                with st.expander(f"❌ {row['TARGET_TABLE']} — {row['RUN_START_UTC']}"):
                    st.code(row.get("ERROR_MESSAGE") or "No error message")
    elif hist is not None:
        st.info("No run history yet.")

# ── TAB 5 — COUNTS ───────────────────────────────────────────────────────────
with tab_counts:
    st.markdown('<div class="section-header">RAW vs SILVER Row Counts</div>',
                unsafe_allow_html=True)
    auto_refresh = st.toggle("Auto-refresh every 30s", value=False)
    compute = st.button("🔢 Compute Counts", type="primary")
    if compute or auto_refresh:
        rows_data = []
        try:
            con = get_sf(cfg); cur = con.cursor()
            for tbl in cfg.get("tables", []):
                if not tbl.get("active", True):
                    continue
                raw_schema = f"{tbl['source_db'].upper()}_RAW"
                target = tbl["target_table"]
                is_scd2 = tbl.get("table_type") == "scd2"

                def _count(schema, table, where=""):
                    try:
                        cur.execute(
                            f"SELECT COUNT(*) FROM MIGRATION_DB.{schema}.{table}"
                            f"{(' WHERE ' + where) if where else ''}")
                        return cur.fetchone()[0]
                    except Exception:  # noqa: BLE001
                        return None
                raw_ct = _count(raw_schema, target)
                if is_scd2:
                    sec_schema = f"{tbl['source_db'].upper()}_SCD2"
                    sec_ct = _count(sec_schema, target, '"IS_CURRENT" = TRUE')
                    layer = "SCD2 (current)"
                else:
                    sec_schema = f"{tbl['source_db'].upper()}_SILVER"
                    sec_ct = _count(sec_schema, target)
                    layer = "SILVER"
                wm = tbl.get("last_loaded_at")
                rows_data.append({
                    "Source Table": f"{tbl['source_db']}.{tbl['source_table']}",
                    "Target": target,
                    "Layer": layer,
                    "RAW Count": raw_ct,
                    "Target Count": sec_ct,
                    "Match": "✅" if raw_ct == sec_ct else "⚠️",
                    "Last Sync": wm[:19] if wm else "Never",
                    "Status": tbl.get("last_run_status", "—"),
                })
            cur.close(); con.close()
        except Exception as e:  # noqa: BLE001
            st.error(f"Error: {e}")
        if rows_data:
            import pandas as pd
            total_raw = sum(r["RAW Count"] or 0 for r in rows_data)
            total_slv = sum(r["Target Count"] or 0 for r in rows_data)
            matched = sum(1 for r in rows_data if r["Match"] == "✅")
            mismatched = sum(1 for r in rows_data if r["Match"] == "⚠️")
            m1, m2, m3, m4 = st.columns(4)
            m1.markdown(f"""<div class="metric-card"><div class="label">Total RAW</div>
                <div class="value">{total_raw:,}</div></div>""", unsafe_allow_html=True)
            m2.markdown(f"""<div class="metric-card"><div class="label">Total TARGET</div>
                <div class="value">{total_slv:,}</div></div>""", unsafe_allow_html=True)
            m3.markdown(f"""<div class="metric-card" style="border-color:#00c89633">
                <div class="label">Matched</div>
                <div class="value" style="color:#00c896">{matched}</div></div>""",
                        unsafe_allow_html=True)
            m4.markdown(f"""<div class="metric-card" style="border-color:#ff4b6e33">
                <div class="label">Mismatched</div>
                <div class="value" style="color:#ff4b6e">{mismatched}</div></div>""",
                        unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame(rows_data), use_container_width=True,
                         hide_index=True)
        if auto_refresh:
            time.sleep(30)
            st.rerun()
