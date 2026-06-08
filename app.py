"""app.py — MySQL → Snowflake Migration Control Panel (Tiger Analytics branded).

Start with:  streamlit run app.py

Contrast strategy: config.toml pins base=light, so Streamlit native widgets render
with dark text on white backgrounds. Custom HTML cards use explicit dark surfaces
with light text — guaranteed contrast regardless of theme detection.
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
GUIDE_PATH = HERE / "USER_GUIDE.md"
_LOGO_DIR = HERE / "assets" / "logos"
_FAVICON = _LOGO_DIR / "ta_favicon.png"

# ── Brand & semantic color tokens (FIXED — not theme-adaptive) ────────────────
TA_ORANGE = "#F15A22"
TA_ORANGE_DARK = "#C94A18"
TA_NAVY = "#0F1B2D"        # card / surface background
TA_NAVY_LIGHT = "#162032"  # slightly lighter surface

TXT_PRIMARY = "#F0F4F8"    # near-white on dark
TXT_SECONDARY = "#A8B8CC"  # muted on dark
TXT_LABEL = "#7E96B0"      # uppercase labels on dark

ST_SUCCESS = "#34D058"
ST_FAILED = "#F85149"
ST_SKIPPED = "#F0A742"
ST_PENDING = "#58A6FF"
ST_SCD2 = "#C084FC"

BORDER = "#263245"
SURFACE_LIGHT = "#FFFFFF"
TEXT_ON_WHITE = "#0F1B2D"

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="MySQL → Snowflake | Tiger Analytics",
    page_icon=str(_FAVICON) if _FAVICON.exists() else "❄️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Sans+Pro:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
html, body, [class*="css"] {{ font-family: 'Source Sans Pro', 'Segoe UI', Arial, sans-serif; }}
#MainMenu, footer, header {{ visibility: hidden; }}
.block-container {{ padding-top: 1rem; padding-bottom: 2rem; }}

/* Sidebar */
section[data-testid="stSidebar"] {{ background: {TA_NAVY} !important; border-right: 3px solid {TA_ORANGE}; }}
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] div,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] .stMarkdown {{ color: {TXT_PRIMARY} !important; }}
section[data-testid="stSidebar"] .stCaption {{ color: {TXT_SECONDARY} !important; }}

/* Metric cards */
.metric-card {{ background: {TA_NAVY}; border: 1px solid {BORDER}; border-radius: 10px; padding: 18px 22px; margin-bottom: 12px; }}
.metric-card .label {{ font-size: .72rem; letter-spacing: 2px; text-transform: uppercase; color: {TXT_LABEL}; margin-bottom: 4px; font-weight: 600; }}
.metric-card .value {{ font-family: 'JetBrains Mono', monospace; font-size: 2rem; font-weight: 700; color: {TXT_PRIMARY}; line-height: 1.1; }}
.metric-card .sub {{ font-size: .72rem; color: {TXT_SECONDARY}; margin-top: 4px; }}

/* Table status cards */
.table-card {{ background: {TA_NAVY}; border: 1px solid {BORDER}; border-radius: 10px; padding: 16px 20px; margin-bottom: 10px; position: relative; overflow: hidden; }}
.table-card::before {{ content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; }}
.table-card.success::before {{ background: {ST_SUCCESS}; }}
.table-card.failed::before {{ background: {ST_FAILED}; }}
.table-card.skipped::before {{ background: {ST_SKIPPED}; }}
.table-card.pending::before {{ background: {ST_PENDING}; }}
.table-card .tname {{ font-weight: 700; font-size: .95rem; color: {TXT_PRIMARY}; padding-right: 90px; }}
.table-card .tmeta {{ font-family: 'JetBrains Mono', monospace; font-size: .72rem; color: {TXT_SECONDARY}; margin-top: 4px; line-height: 1.6; }}
.table-card .tstatus {{ font-size: .68rem; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; padding: 3px 9px; border-radius: 4px; position: absolute; top: 16px; right: 20px; }}
.tstatus.success {{ background: #0d2e15; color: {ST_SUCCESS}; border: 1px solid {ST_SUCCESS}44; }}
.tstatus.failed {{ background: #2e0d0d; color: {ST_FAILED}; border: 1px solid {ST_FAILED}44; }}
.tstatus.skipped {{ background: #2e1e0d; color: {ST_SKIPPED}; border: 1px solid {ST_SKIPPED}44; }}
.tstatus.pending {{ background: #0d1e2e; color: {ST_PENDING}; border: 1px solid {ST_PENDING}44; }}

/* Log terminal */
.log-box {{ background: #0A0F17; border: 1px solid {BORDER}; border-radius: 8px; padding: 16px; font-family: 'JetBrains Mono', monospace; font-size: .75rem; color: #8BAFC8; height: 340px; overflow-y: auto; white-space: pre-wrap; line-height: 1.7; }}
.log-ok {{ color: {ST_SUCCESS}; }}
.log-err {{ color: {ST_FAILED}; }}
.log-warn {{ color: {ST_SKIPPED}; }}
.log-info {{ color: {ST_PENDING}; }}

/* Section header */
.section-header {{ font-size: .7rem; letter-spacing: 2px; text-transform: uppercase; color: {TA_ORANGE}; font-weight: 700; margin-bottom: 12px; padding-bottom: 6px; border-bottom: 2px solid {TA_ORANGE}33; }}

/* Pills */
.pill {{ display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: .67rem; font-weight: 700; letter-spacing: .5px; text-transform: uppercase; margin-right: 4px; }}
.pill-full {{ background: #0d1e2e; color: {ST_PENDING}; border: 1px solid {ST_PENDING}55; }}
.pill-incr {{ background: #0d2e15; color: {ST_SUCCESS}; border: 1px solid {ST_SUCCESS}55; }}
.pill-scd2 {{ background: #1e0d2e; color: {ST_SCD2}; border: 1px solid {ST_SCD2}55; }}

/* Connection dot */
.dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }}

/* Primary button */
[data-testid="baseButton-primary"] > button,
.stButton > button[kind="primary"] {{ background: {TA_ORANGE} !important; color: #fff !important; border: none !important; font-weight: 700 !important; letter-spacing: .3px; }}
[data-testid="baseButton-primary"] > button:hover {{ background: {TA_ORANGE_DARK} !important; }}

/* Namespace info box */
.ns-box {{ background: {TA_NAVY}; border: 1px solid {BORDER}; border-radius: 6px; padding: 10px 14px; font-family: 'JetBrains Mono', monospace; font-size: .73rem; line-height: 1.8; margin-top: 8px; }}
.ns-box .ns-label {{ color: {TXT_LABEL}; }}
.ns-box .ns-value {{ color: {TXT_PRIMARY}; }}
</style>
""", unsafe_allow_html=True)


# ── Config helpers ────────────────────────────────────────────────────────────
def _default_config() -> dict:
    return {
        "source": {"host": os.getenv("MYSQL_HOST", "localhost"),
                   "port": int(os.getenv("MYSQL_PORT", "3306")),
                   "user": os.getenv("MYSQL_USER", "root"),
                   "password": os.getenv("MYSQL_PASSWORD", "")},
        "snowflake": {"account": os.getenv("SF_ACCOUNT", ""),
                      "user": os.getenv("SF_USER", ""),
                      "password": os.getenv("SF_PASSWORD", ""),
                      "warehouse": os.getenv("SF_WAREHOUSE", "COMPUTE_WH"),
                      "database": "MIGRATION_DB", "schema": "META"},
        "export_dir": str(HERE / "export"),
        "tables": [],
    }


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            d = json.load(f)
        if d:
            return d
    except Exception:
        pass
    return _default_config()


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def sf_conf(cfg: dict) -> dict:
    out = dict(cfg["snowflake"])
    for k, e in {"account": "SF_ACCOUNT", "user": "SF_USER", "password": "SF_PASSWORD",
                 "warehouse": "SF_WAREHOUSE", "database": "SF_DATABASE",
                 "schema": "SF_SCHEMA"}.items():
        if os.getenv(e):
            out[k] = os.getenv(e)
    return out


def my_conf(cfg: dict) -> dict:
    out = dict(cfg["source"])
    for k, e in {"host": "MYSQL_HOST", "port": "MYSQL_PORT",
                 "user": "MYSQL_USER", "password": "MYSQL_PASSWORD"}.items():
        if os.getenv(e):
            out[k] = int(os.getenv(e)) if k == "port" else os.getenv(e)
    return out


# ── Connections ───────────────────────────────────────────────────────────────
def get_sf(cfg):
    import snowflake.connector
    return snowflake.connector.connect(**sf_conf(cfg))


def get_mysql(cfg):
    import mysql.connector
    c = my_conf(cfg)
    return mysql.connector.connect(
        host=c["host"], port=int(c["port"]), user=c["user"], password=c["password"])


def check_connections(cfg) -> dict:
    out = {}
    try:
        con = get_mysql(cfg); cur = con.cursor()
        cur.execute("SELECT VERSION()")
        ver = cur.fetchone()[0]; cur.close(); con.close()
        out["MySQL"] = (True, str(ver))
    except Exception as e:  # noqa: BLE001
        out["MySQL"] = (False, str(e)[:80])
    try:
        con = get_sf(cfg); cur = con.cursor()
        cur.execute("SELECT CURRENT_ACCOUNT(), CURRENT_WAREHOUSE()")
        acct, wh = cur.fetchone(); cur.close(); con.close()
        out["Snowflake"] = (True, f"{acct} / {wh}")
    except Exception as e:  # noqa: BLE001
        out["Snowflake"] = (False, str(e)[:80])
    return out


# ── Namespace helpers (mirror loader.py) ──────────────────────────────────────
def raw_schema(db):
    return db.strip().upper() + "_RAW"


def silver_schema(db):
    return db.strip().upper() + "_SILVER"


def scd2_schema(db):
    return db.strip().upper() + "_SCD2"


def raw_fqn(tbl):
    return f"MIGRATION_DB.{raw_schema(tbl['source_db'])}.{tbl['target_table']}"


def target_fqn(tbl):
    s = (scd2_schema(tbl['source_db']) if tbl.get("table_type") == "scd2"
         else silver_schema(tbl['source_db']))
    return f"MIGRATION_DB.{s}.{tbl['target_table']}"


def target_layer(tbl):
    return "SCD2" if tbl.get("table_type") == "scd2" else "SILVER"


# ── UI helpers ────────────────────────────────────────────────────────────────
def status_icon(s):
    return {"success": "✅", "failed": "❌", "skipped": "⏭️", None: "⏳"}.get(s, "⏳")


def load_type_pill(tbl):
    if tbl.get("table_type") == "scd2":
        return '<span class="pill pill-scd2">SCD2</span>'
    lt = tbl.get("load_type", "full")
    cls = "pill-incr" if lt == "incremental" else "pill-full"
    return f'<span class="pill {cls}">{lt}</span>'


def colorize_log(line: str) -> str:
    low = line.lower()
    if any(x in low for x in ["✅", "done", "success", "complete"]):
        return f'<span class="log-ok">{line}</span>'
    if any(x in low for x in ["❌", "failed", "error", "exception", "traceback"]):
        return f'<span class="log-err">{line}</span>'
    if any(x in low for x in ["⚠️", "warn", "skip", "no new", "drift"]):
        return f'<span class="log-warn">{line}</span>'
    if any(x in low for x in ["[full", "[incr", "batch", "====", "migration"]):
        return f'<span class="log-info">{line}</span>'
    return line


def run_subprocess_stream(args: list[str]):
    log_area = st.empty()
    lines = []
    proc = subprocess.Popen(
        [sys.executable, *args], cwd=str(HERE),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for raw in proc.stdout:
        lines.append(colorize_log(raw.rstrip()))
        log_area.markdown(
            f'<div class="log-box">{"<br>".join(lines[-60:])}</div>',
            unsafe_allow_html=True)
    proc.wait()
    return proc.returncode, "\n".join(lines)


def _logo_b64(dark=True) -> str:
    p = _LOGO_DIR / ("ta_logo_dark.svg" if dark else "ta_logo_light.svg")
    if p.exists():
        import base64
        return base64.b64encode(p.read_bytes()).decode()
    return ""


def render_header():
    # Self-contained navy banner — white text is readable regardless of the
    # app's (light or dark) main background. Logo lives in the sidebar.
    st.markdown(
        f'<div style="background:{TA_NAVY};border-left:6px solid {TA_ORANGE};'
        f'border-radius:8px;padding:16px 22px;margin-bottom:18px;">'
        f'<div style="font-size:1.5rem;font-weight:700;color:#FFFFFF;">'
        f'MySQL &#8594; Snowflake Migration</div>'
        f'<div style="font-size:.82rem;color:{TXT_SECONDARY};margin-top:2px;">'
        f'Tiger Analytics &middot; Data Engineering Platform</div></div>',
        unsafe_allow_html=True)


def render_footer():
    st.markdown("---")
    st.markdown(
        f'<p style="text-align:center;color:{TXT_SECONDARY};font-size:0.8rem;">'
        f'Powered by <span style="color:{TA_ORANGE};font-weight:700;">Tiger Analytics</span>'
        , unsafe_allow_html=True)

# ── Load config ───────────────────────────────────────────────────────────────
cfg = load_config()


def _add_table_form():
    """Body of the manual 'add table' form. Appends to config + reruns."""
    st.caption("Add a single table entry to migration_config.json")
    c1, c2 = st.columns(2)
    source_db = c1.text_input("Source schema (MySQL db)", key="add_db")
    source_table = c2.text_input("Source table", key="add_tbl")
    target_table = st.text_input("Target table (Snowflake)", key="add_tgt",
                                 help="Defaults to UPPER(source table) if blank")
    c3, c4 = st.columns(2)
    primary_key = c3.text_input("Primary key", key="add_pk")
    watermark_col = c4.text_input("Watermark column (optional)", key="add_wm")
    c5, c6 = st.columns(2)
    load_type = c5.selectbox("Load type", ["full", "incremental"], key="add_lt")
    ttype = c6.selectbox("Target type", ["standard", "scd2"], key="add_tt")
    track = ""
    if ttype == "scd2":
        track = st.text_input("SCD2 tracked columns (comma, blank = all)", key="add_track")
    c7, c8 = st.columns(2)
    reconcile = c7.checkbox("Reconcile deletes", key="add_rec")
    active = c8.checkbox("Active", value=True, key="add_act")

    if st.button("➕ Add table", type="primary", key="add_submit"):
        if not (source_db.strip() and source_table.strip()):
            st.error("Source schema and source table are required.")
            return
        tgt = (target_table.strip() or source_table.strip()).upper()
        pk_u = primary_key.strip().upper() or None
        wm_u = watermark_col.strip().upper() or None
        entry = {
            "source_db": source_db.strip(), "source_table": source_table.strip(),
            "target_table": tgt, "primary_key": pk_u,
            "load_type": load_type, "watermark_col": wm_u,
            "last_loaded_at": None,
            "partition_col": pk_u, "partition_num": 4,
            "reconcile": reconcile, "active": active, "last_run_status": None,
        }
        if ttype == "scd2":
            entry["table_type"] = "scd2"
            entry["scd2"] = {"track_columns":
                             [c.strip().upper() for c in track.split(",") if c.strip()]}
        live = load_config()
        # Replace if same (source_db, source_table) already exists, else append.
        live.setdefault("tables", [])
        live["tables"] = [t for t in live["tables"]
                          if not (t.get("source_db") == entry["source_db"]
                                  and t.get("source_table") == entry["source_table"])]
        live["tables"].append(entry)
        save_config(live)
        st.success(f"Added {entry['source_db']}.{entry['source_table']}")
        st.rerun()


# Wrap as a modal dialog when supported (Streamlit ≥1.31), else inline fallback.
if hasattr(st, "dialog"):
    add_table_dialog = st.dialog("➕ Add Table Manually")(_add_table_form)
else:
    add_table_dialog = None


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    b64 = _logo_b64(dark=True)  # light logo for dark sidebar
    if b64:
        st.markdown(
            f'<img src="data:image/svg+xml;base64,{b64}" '
            f'style="width:80%;max-width:160px;margin:10px auto 18px;display:block">',
            unsafe_allow_html=True)
    else:
        st.markdown(f"<h3 style='color:{TXT_PRIMARY}'>❄️ Migration Hub</h3>",
                    unsafe_allow_html=True)
    st.markdown(f"<hr style='border-color:{BORDER}'>", unsafe_allow_html=True)

    if "_conn" not in st.session_state:
        with st.spinner("Checking connections…"):
            st.session_state["_conn"] = check_connections(cfg)
    if st.button("🔌 Re-check Connections", use_container_width=True):
        st.session_state["_conn"] = check_connections(cfg)
    for name, (ok, detail) in st.session_state["_conn"].items():
        color = ST_SUCCESS if ok else ST_FAILED
        st.markdown(
            f'<div style="margin:5px 0;font-size:.82rem;color:{TXT_PRIMARY}">'
            f'<span class="dot" style="background:{color}"></span>'
            f'<b>{name}</b>: <span style="color:{TXT_SECONDARY}">{detail}</span></div>',
            unsafe_allow_html=True)

    st.markdown(f"<hr style='border-color:{BORDER}'>", unsafe_allow_html=True)
    active_tbls = [t for t in cfg.get("tables", []) if t.get("active", True)]
    full_n = sum(1 for t in active_tbls if t.get("load_type") == "full")
    incr_n = sum(1 for t in active_tbls if t.get("load_type") == "incremental")
    scd2_n = sum(1 for t in active_tbls if t.get("table_type") == "scd2")
    never_n = sum(1 for t in active_tbls if not t.get("last_loaded_at"))
    failed_n = sum(1 for t in active_tbls if t.get("last_run_status") == "failed")
    st.markdown(f"""
        <div class="metric-card">
          <div class="label">Active Tables</div>
          <div class="value">{len(active_tbls)}</div>
          <div class="sub">{full_n} full &nbsp;·&nbsp; {incr_n} incremental &nbsp;·&nbsp; {scd2_n} scd2</div>
        </div>
        <div class="metric-card">
          <div class="label">Awaiting First Load</div>
          <div class="value">{never_n}</div>
          <div class="sub">last_loaded_at = null</div>
        </div>
    """, unsafe_allow_html=True)
    if failed_n:
        st.markdown(
            f'<div style="background:#2e0d0d;border:1px solid {ST_FAILED}44;border-radius:6px;'
            f'padding:8px 12px;font-size:.82rem;color:{ST_FAILED};margin-top:8px">'
            f'⚠️ {failed_n} table(s) failed last run</div>', unsafe_allow_html=True)

    st.markdown(f"<hr style='border-color:{BORDER}'>", unsafe_allow_html=True)
    dbs = sorted({t["source_db"].upper() for t in cfg.get("tables", [])})
    if dbs:
        st.markdown(
            f'<div style="font-size:.68rem;letter-spacing:2px;text-transform:uppercase;'
            f'color:{TXT_LABEL};font-weight:700;margin-bottom:8px">Namespace Map</div>',
            unsafe_allow_html=True)
        for db in dbs[:6]:
            st.markdown(
                f'<div style="font-family:monospace;font-size:.7rem;'
                f'color:{TXT_SECONDARY};margin:3px 0">'
                f'<span style="color:{TA_ORANGE}">{db}</span>'
                f' → {db}_RAW / {db}_SILVER</div>', unsafe_allow_html=True)

    st.markdown(f"<hr style='border-color:{BORDER}'>", unsafe_allow_html=True)
    st.markdown(
        f'<div style="font-size:.7rem;color:{TXT_LABEL};line-height:1.8">'
        f'Account: <code style="color:{TXT_SECONDARY}">{sf_conf(cfg).get("account","—")}</code><br>'
        f'Stage: <code style="color:{TXT_SECONDARY};font-size:.65rem">MIGRATION_DB.META</code>'
        f'</div>', unsafe_allow_html=True)

    st.markdown(f"<hr style='border-color:{BORDER}'>", unsafe_allow_html=True)
    if st.button("📖 User Guide", use_container_width=True):
        st.session_state["_view"] = "guide"
        st.rerun()

# ── Main ──────────────────────────────────────────────────────────────────────
if st.session_state.get("_view") == "guide":
    render_header()
    if st.button("⬅ Back to App"):
        st.session_state["_view"] = "app"
        st.rerun()
    try:
        st.markdown(GUIDE_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not load USER_GUIDE.md: {e}")
    render_footer()
    st.stop()

render_header()

tab_dash, tab_run, tab_cfg_ed, tab_hist, tab_counts = st.tabs([
    "📊 Dashboard", "▶️ Run", "⚙️ Config", "📜 History", "🔢 Counts"])

# ── TAB 1 — DASHBOARD ─────────────────────────────────────────────────────────
with tab_dash:
    tables = cfg.get("tables", [])
    st.markdown('<div class="section-header">Run Summary</div>', unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    for col, label, val, color in [
        (c1, "SUCCESS", sum(1 for t in tables if t.get("last_run_status") == "success"), ST_SUCCESS),
        (c2, "FAILED", sum(1 for t in tables if t.get("last_run_status") == "failed"), ST_FAILED),
        (c3, "PENDING", sum(1 for t in tables if not t.get("last_run_status")), ST_PENDING),
        (c4, "SKIPPED", sum(1 for t in tables if t.get("last_run_status") == "skipped"), ST_SKIPPED),
    ]:
        col.markdown(f"""<div class="metric-card" style="border-left:4px solid {color}">
            <div class="label">{label}</div>
            <div class="value" style="color:{color}">{val}</div>
            <div class="sub">tables</div></div>""", unsafe_allow_html=True)

    if not tables:
        st.info("No tables configured yet. Use **Config** tab or **Run → Generate Config**.")
    else:
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="section-header">Per-Table Status</div>',
                    unsafe_allow_html=True)
        cols = st.columns(2)
        for i, tbl in enumerate(tables):
            status = tbl.get("last_run_status") or "pending"
            wm = tbl.get("last_loaded_at")
            inactive = ("" if tbl.get("active", True)
                        else '<span class="pill" style="background:#1a1a1a;color:#666;border:1px solid #333">INACTIVE</span>')
            reconcile = ('<span class="pill" style="background:#0d1e2e;color:#58A6FF;border:1px solid #58A6FF44">RECONCILE</span>'
                         if tbl.get("reconcile") else "")
            with cols[i % 2]:
                st.markdown(f"""
                    <div class="table-card {status}">
                      <span class="tstatus {status}">{status_icon(status)} {status.upper()}</span>
                      <div class="tname">{tbl['source_db']}.{tbl['source_table']}</div>
                      <div class="tmeta">→ {raw_fqn(tbl)}</div>
                      <div class="tmeta" style="margin-top:6px">
                        {load_type_pill(tbl)} {inactive} {reconcile}</div>
                      <div class="tmeta" style="margin-top:6px;color:{TXT_LABEL}">
                        🕐 {"Last sync: " + str(wm)[:19] if wm else "Never run"}
                        &nbsp;·&nbsp; PK: {tbl.get("primary_key", "—")}
                        &nbsp;·&nbsp; Parts: {tbl.get("partition_num", 4)}</div>
                    </div>
                """, unsafe_allow_html=True)

# ── TAB 2 — RUN ───────────────────────────────────────────────────────────────
with tab_run:
    st.markdown('<div class="section-header">Pipeline Controls</div>',
                unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    run_clicked = c1.button("▶️ Run Migration", type="primary", use_container_width=True)
    rec_clicked = c2.button("🗑️ Reconcile Deletes", use_container_width=True)
    full_clicked = c3.button("🔁 Force Full Reload", use_container_width=True)

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">Run Single Table</div>',
                unsafe_allow_html=True)
    g1, g2, g3 = st.columns([4, 1, 1])
    table_options = [f"{t['source_db']}.{t['source_table']}"
                     for t in cfg.get("tables", []) if t.get("active", True)]
    selected = g1.selectbox("Table", ["— all tables —"] + table_options,
                            label_visibility="collapsed")
    single_clicked = g2.button("▶️ Run", use_container_width=True,
                               disabled=(selected == "— all tables —"))
    single_rec_clicked = g3.button("🗑️ Reconcile", use_container_width=True,
                                   disabled=(selected == "— all tables —"),
                                   help="Reconcile deletes for just this table")

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">Live Log</div>', unsafe_allow_html=True)
    if "last_log" not in st.session_state:
        st.session_state["last_log"] = "No runs yet. Click ▶️ Run Migration to start."

    if run_clicked:
        st.info("⚡ Migration starting…")
        rc, log = run_subprocess_stream(["orchestrator.py"])
        st.session_state["last_log"] = log
        (st.success if rc == 0 else st.error)(
            f"{'✅ Completed' if rc == 0 else '❌ Failed'} (exit {rc})")
        st.rerun()
    elif rec_clicked:
        st.info("🗑️ Running reconcile pass…")
        rc, log = run_subprocess_stream(["orchestrator.py", "--reconcile"])
        st.session_state["last_log"] = log
        (st.success if rc == 0 else st.error)(f"exit {rc}")
        st.rerun()
    elif full_clicked:
        st.warning("⚠️ This truncates and reloads ALL tables.")
        if st.button("Confirm Full Reload", type="primary"):
            rc, log = run_subprocess_stream(["orchestrator.py", "--full"])
            st.session_state["last_log"] = log
            st.rerun()
    elif single_clicked and selected != "— all tables —":
        _, tbl_name = selected.split(".", 1)
        st.info(f"▶️ Running {selected}…")
        rc, log = run_subprocess_stream(["orchestrator.py", "--table", tbl_name])
        st.session_state["last_log"] = log
        st.rerun()
    elif single_rec_clicked and selected != "— all tables —":
        _, tbl_name = selected.split(".", 1)
        st.info(f"🗑️ Reconciling deletes for {selected}…")
        rc, log = run_subprocess_stream(
            ["orchestrator.py", "--reconcile", "--table", tbl_name])
        st.session_state["last_log"] = log
        (st.success if rc == 0 else st.error)(f"exit {rc}")
        st.rerun()
    else:
        lines = st.session_state["last_log"].split("\n")
        colored = "<br>".join(colorize_log(line) for line in lines[-60:])
        st.markdown(f'<div class="log-box">{colored}</div>', unsafe_allow_html=True)

# ── TAB 3 — CONFIG EDITOR ─────────────────────────────────────────────────────
with tab_cfg_ed:
    # ── Build configuration: generate from schema OR add a table manually ──
    st.markdown('<div class="section-header">Build Configuration</div>',
                unsafe_allow_html=True)
    g1, g2, g3 = st.columns([3, 1, 1])
    schema_input = g1.text_input("MySQL schema", placeholder="e.g. sales_db",
                                 label_visibility="collapsed", key="gen_schema")
    if g2.button("⚙️ Generate", use_container_width=True):
        if schema_input.strip():
            rc, _ = run_subprocess_stream(["config_generator.py", schema_input.strip()])
            if rc == 0:
                st.success(f"✅ Config updated for schema: {schema_input}")
                st.rerun()
            else:
                st.error("Generation failed — check log in Run tab")
        else:
            st.warning("Enter a MySQL schema name first.")
    if g3.button("➕ Add table", use_container_width=True):
        if add_table_dialog is not None:
            add_table_dialog()
        else:
            st.session_state["_show_add"] = True
    # Inline fallback for older Streamlit without st.dialog
    if add_table_dialog is None and st.session_state.get("_show_add"):
        with st.expander("➕ Add Table Manually", expanded=True):
            _add_table_form()

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">Table Configuration</div>',
                unsafe_allow_html=True)
    tables = cfg.get("tables", [])
    if not tables:
        st.info("No tables yet. Use **Generate** (from a MySQL schema) or "
                "**➕ Add table** above.")
    else:
        changed = False
        for i, tbl in enumerate(tables):
            is_scd2 = tbl.get("table_type") == "scd2"
            label = (f"{'🟢' if tbl.get('active', True) else '⚫'} "
                     f"{tbl['source_db']}.{tbl['source_table']} → {tbl['target_table']} "
                     f"[{tbl.get('load_type', 'full')}]"
                     f"{' · SCD2' if is_scd2 else ''}")
            with st.expander(label, expanded=False):
                col_a, col_b, col_c = st.columns(3)
                new_load = col_a.selectbox(
                    "Load type", ["full", "incremental"],
                    index=["full", "incremental"].index(tbl.get("load_type", "full")),
                    key=f"lt_{i}")
                new_wm = col_b.text_input("Watermark column",
                                          value=tbl.get("watermark_col") or "", key=f"wm_{i}")
                new_pk = col_c.text_input("Primary key",
                                          value=tbl.get("primary_key") or "", key=f"pk_{i}")
                col_d, col_e, col_f, col_g = st.columns(4)
                new_active = col_d.checkbox("Active", value=tbl.get("active", True),
                                            key=f"act_{i}")
                new_reconcile = col_e.checkbox("Reconcile deletes",
                                               value=tbl.get("reconcile", False), key=f"rec_{i}")
                new_parts = col_f.number_input("Partitions", min_value=1, max_value=32,
                                               value=int(tbl.get("partition_num", 4)), key=f"pn_{i}")
                new_rpf = col_g.number_input("Rows/file (0=off)", min_value=0, step=100000,
                                             value=int(tbl.get("rows_per_file", 1000000) or 0),
                                             key=f"rf_{i}")
                new_ttype = st.selectbox(
                    "Target type", ["standard", "scd2"],
                    index=1 if is_scd2 else 0, key=f"tt_{i}",
                    help="standard → SILVER (Type 1) · scd2 → versioned dimension")
                if new_ttype == "scd2":
                    existing_track = ", ".join(tbl.get("scd2", {}).get("track_columns", []))
                    new_track_raw = st.text_input(
                        "SCD2 tracked columns (comma-separated, blank = all)",
                        value=existing_track, key=f"track_{i}")
                    new_track = [c.strip() for c in new_track_raw.split(",") if c.strip()]
                else:
                    new_track = None

                wm_val = tbl.get("last_loaded_at")
                status = tbl.get("last_run_status", "pending")
                sc = (ST_SUCCESS if status == "success"
                      else ST_FAILED if status == "failed" else ST_SKIPPED)
                st.markdown(f"""
                    <div class="ns-box">
                      <span class="ns-label">RAW : </span>
                      <span class="ns-value">{raw_fqn(tbl)}</span><br>
                      <span class="ns-label">DEST: </span>
                      <span class="ns-value">{target_fqn(tbl)}</span><br>
                      <span class="ns-label">Watermark: </span>
                      <span class="ns-value">{wm_val or "Never run"}</span>
                      &nbsp;·&nbsp; <span style="color:{sc}">{status_icon(status)} {status}</span>
                    </div>
                """, unsafe_allow_html=True)

                updates = {
                    "load_type": new_load,
                    "watermark_col": (new_wm.strip().upper() or None),
                    "primary_key": (new_pk.strip().upper() or None),
                    "active": new_active,
                    "reconcile": new_reconcile,
                    "partition_num": int(new_parts),
                    "rows_per_file": int(new_rpf) or None,
                }
                for k, v in updates.items():
                    if v != tbl.get(k):
                        cfg["tables"][i][k] = v
                        changed = True
                # Target type (standard ⇄ scd2)
                if new_ttype == "scd2":
                    track_u = [c.upper() for c in new_track] if new_track else []
                    if tbl.get("table_type") != "scd2":
                        cfg["tables"][i]["table_type"] = "scd2"
                        changed = True
                    if new_track is not None and track_u != tbl.get("scd2", {}).get("track_columns", []):
                        cfg["tables"][i].setdefault("scd2", {})["track_columns"] = track_u
                        changed = True
                else:
                    if tbl.get("table_type") == "scd2":
                        cfg["tables"][i].pop("table_type", None)
                        cfg["tables"][i].pop("scd2", None)
                        changed = True

        if changed:
            if st.button("💾 Save Changes", type="primary"):
                save_config(cfg)
                st.success("✅ migration_config.json saved")
                time.sleep(0.4)
                st.rerun()
        else:
            st.caption("No unsaved changes.")
        with st.expander("📄 Raw JSON"):
            st.json(cfg)

# ── TAB 4 — HISTORY ───────────────────────────────────────────────────────────
with tab_hist:
    st.markdown('<div class="section-header">Run History — MIGRATION_DB.META.RUN_LOG</div>',
                unsafe_allow_html=True)
    c1, c2 = st.columns([1, 4])
    if c1.button("🔄 Refresh", use_container_width=True):
        st.session_state.pop("_hist", None)
    limit = c2.slider("Rows to show", 20, 500, 100)
    if "_hist" not in st.session_state:
        try:
            con = get_sf(cfg); cur = con.cursor()
            cur.execute(f"""
                SELECT INSERTED_AT, BATCH_ID, SOURCE_DB, SOURCE_TABLE, TARGET_TABLE,
                       LOAD_TYPE, ENGINE, ROWS_EXTRACTED, ROWS_RAW, ROWS_SILVER,
                       DURATION_SEC, STATUS, FAILED_STEP, ERROR_MESSAGE,
                       WATERMARK_FROM, WATERMARK_TO, RUN_START_UTC, RUN_END_UTC
                FROM MIGRATION_DB.META.RUN_LOG
                ORDER BY INSERTED_AT DESC LIMIT {int(limit)}""")
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
        def _style(val):
            return {"success": f"color:{ST_SUCCESS};font-weight:700",
                    "failed": f"color:{ST_FAILED};font-weight:700",
                    "skipped": f"color:{ST_SKIPPED};font-weight:700"}.get(str(val).lower(), "")
        st.dataframe(hist.style.map(_style, subset=["STATUS"]),
                     use_container_width=True, hide_index=True)
        failed = hist[hist["STATUS"] == "failed"]
        if not failed.empty:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown('<div class="section-header">❌ Error Detail</div>',
                        unsafe_allow_html=True)
            for _, row in failed.iterrows():
                step = row.get("FAILED_STEP") or "?"
                with st.expander(f"❌ {row.get('SOURCE_DB', '?')}.{row.get('TARGET_TABLE', '?')} "
                                 f"— step: {step} — {str(row.get('INSERTED_AT', ''))[:19]}"):
                    st.code(row.get("ERROR_MESSAGE") or "No error message", language="text")
    elif hist is not None:
        st.info("No run history yet. Run the migration first.")
    else:
        st.warning("Could not load history — check Snowflake connection.")

# ── TAB 5 — COUNTS ────────────────────────────────────────────────────────────
with tab_counts:
    st.markdown('<div class="section-header">RAW vs SILVER / SCD2 Row Counts</div>',
                unsafe_allow_html=True)
    st.caption("RAW = MIGRATION_DB.<DB>_RAW · SILVER = <DB>_SILVER · "
               "SCD2 current = <DB>_SCD2 WHERE IS_CURRENT=TRUE")
    c1, c2, c3 = st.columns([1, 1, 2])
    compute = c1.button("🔢 Compute Counts", type="primary", use_container_width=True)
    validate = c2.button("🔍 Validate vs MySQL", use_container_width=True,
                         help="Compare MySQL row counts against Snowflake RAW (live)")
    auto_refresh = c3.toggle("Auto-refresh every 30s", value=False)
    if compute or auto_refresh:
        rows_data = []
        try:
            con = get_sf(cfg); cur = con.cursor()
            for tbl in cfg.get("tables", []):
                if not tbl.get("active", True):
                    continue
                is_scd2 = tbl.get("table_type") == "scd2"

                def _count(fqn, where=""):
                    try:
                        q = f"SELECT COUNT(*) FROM {fqn}"
                        if where:
                            q += f" WHERE {where}"
                        cur.execute(q)
                        return cur.fetchone()[0]
                    except Exception:  # noqa: BLE001
                        return None
                raw_ct = _count(raw_fqn(tbl))
                tgt_ct = (_count(target_fqn(tbl), '"IS_CURRENT" = TRUE') if is_scd2
                          else _count(target_fqn(tbl)))
                wm = tbl.get("last_loaded_at")
                rows_data.append({
                    "Source Table": f"{tbl['source_db']}.{tbl['source_table']}",
                    "RAW Count": raw_ct,
                    "Target Layer": "SCD2 (current)" if is_scd2 else "SILVER",
                    "Target Count": tgt_ct,
                    "Match": ("✅" if raw_ct == tgt_ct
                              else "⚠️" if (raw_ct is not None and tgt_ct is not None)
                              else "—"),
                    "Last Sync": str(wm)[:19] if wm else "Never",
                    "Status": tbl.get("last_run_status", "—"),
                })
            cur.close(); con.close()
        except Exception as e:  # noqa: BLE001
            st.error(f"Snowflake error: {e}")
        if rows_data:
            import pandas as pd
            total_raw = sum(r["RAW Count"] or 0 for r in rows_data)
            total_tgt = sum(r["Target Count"] or 0 for r in rows_data)
            matched = sum(1 for r in rows_data if r["Match"] == "✅")
            mismatch = sum(1 for r in rows_data if r["Match"] == "⚠️")
            m1, m2, m3, m4 = st.columns(4)
            m1.markdown(f"""<div class="metric-card"><div class="label">Total RAW Rows</div>
                <div class="value">{total_raw:,}</div></div>""", unsafe_allow_html=True)
            m2.markdown(f"""<div class="metric-card"><div class="label">SILVER / SCD2 Rows</div>
                <div class="value">{total_tgt:,}</div></div>""", unsafe_allow_html=True)
            m3.markdown(f"""<div class="metric-card" style="border-left:4px solid {ST_SUCCESS}">
                <div class="label">Matched</div>
                <div class="value" style="color:{ST_SUCCESS}">{matched}</div>
                <div class="sub">RAW = target</div></div>""", unsafe_allow_html=True)
            m4.markdown(f"""<div class="metric-card" style="border-left:4px solid {ST_FAILED}">
                <div class="label">Mismatched</div>
                <div class="value" style="color:{ST_FAILED}">{mismatch}</div>
                <div class="sub">RAW ≠ target</div></div>""", unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame(rows_data), use_container_width=True, hide_index=True)
        if auto_refresh:
            time.sleep(30)
            st.rerun()

    if validate:
        st.markdown('<div class="section-header">Source ↔ Snowflake Parity</div>',
                    unsafe_allow_html=True)
        st.caption("Source (MySQL) vs RAW live rows (excluding soft-deleted). "
                   "Target shown for reference (dedupe/versioning differs).")
        vrows = []
        try:
            scon = get_sf(cfg); scur = scon.cursor()
            mcon = get_mysql(cfg)

            def _sfc(fqn, where=""):
                try:
                    q = f"SELECT COUNT(*) FROM {fqn}"
                    if where:
                        q += f" WHERE {where}"
                    scur.execute(q)
                    return scur.fetchone()[0]
                except Exception:  # noqa: BLE001
                    return None
            for tbl in cfg.get("tables", []):
                if not tbl.get("active", True):
                    continue
                is_scd2 = tbl.get("table_type") == "scd2"
                mcur = mcon.cursor()
                mcur.execute(
                    f"SELECT COUNT(*) FROM `{tbl['source_db']}`.`{tbl['source_table']}`")
                src = mcur.fetchone()[0]
                mcur.close()
                raw_live = _sfc(raw_fqn(tbl), 'COALESCE("_IS_DELETED", FALSE) = FALSE')
                tgt = (_sfc(target_fqn(tbl), '"IS_CURRENT" = TRUE') if is_scd2
                       else _sfc(target_fqn(tbl)))
                parity = ("✅" if src == raw_live
                          else "⚠️" if (src is not None and raw_live is not None) else "—")
                vrows.append({
                    "Source Table": f"{tbl['source_db']}.{tbl['source_table']}",
                    "Source (MySQL)": src,
                    "RAW (live)": raw_live,
                    "Δ": (src - raw_live) if (src is not None and raw_live is not None) else None,
                    "Parity": parity,
                    "Target": tgt,
                    "Target Layer": "SCD2" if is_scd2 else "SILVER",
                })
            scur.close(); scon.close(); mcon.close()
        except Exception as e:  # noqa: BLE001
            st.error(f"Validation error: {e}")
        if vrows:
            import pandas as pd
            in_sync = sum(1 for r in vrows if r["Parity"] == "✅")
            out_sync = sum(1 for r in vrows if r["Parity"] == "⚠️")
            v1, v2 = st.columns(2)
            v1.markdown(f"""<div class="metric-card" style="border-left:4px solid {ST_SUCCESS}">
                <div class="label">In Sync</div>
                <div class="value" style="color:{ST_SUCCESS}">{in_sync}</div>
                <div class="sub">source = RAW live</div></div>""", unsafe_allow_html=True)
            v2.markdown(f"""<div class="metric-card" style="border-left:4px solid {ST_FAILED}">
                <div class="label">Out of Sync</div>
                <div class="value" style="color:{ST_FAILED}">{out_sync}</div>
                <div class="sub">needs re-run / reconcile</div></div>""", unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame(vrows), use_container_width=True, hide_index=True)

render_footer()
