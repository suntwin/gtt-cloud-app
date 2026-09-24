import streamlit as st
import pandas as pd
import numpy as np
import requests
from io import StringIO
from pandas.api.types import is_categorical_dtype, is_numeric_dtype, is_object_dtype
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode, GridUpdateMode, DataReturnMode
import os, json, time
from datetime import datetime

st.set_page_config(page_title="GTT Trade Generator (NSE)", page_icon="⚡", layout="wide")

from supabase import create_client, Client

SUPABASE_URL = "https://uroqarbpyrloymijbqaa.supabase.co"
SUPABASE_KEY = "sb_publishable_bPnWVx9S7zI0_FdK8RCbRg_Gfc2Vqzt"

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    st.error(f"Database connection failed: {e}")
    supabase = None

gtt_endpoints = {
    "1M": "https://api.marketinout.com/run/screen?key=dbf1d7c7f45c4fac",
    "3M": "https://api.marketinout.com/run/screen?key=29d147cbc8f1466b",
    "6M": "https://api.marketinout.com/run/screen?key=c53af41692ff4949"
}
weekly_endpoint = "https://api.marketinout.com/run/screen?key=64e86ed22d834681"

weekly_metric_columns = [
    'Wema10', 'Dist_wema10_pct', 'Weeklyclose_chg_pct', 'Tightcloses_10w_of5',
    'Insidebar_thiswk', 'Insidebars_of8', 'Weeklycontraction', 'Pricevs2yrlow_ratio',
    'Pctof10wkhigh', 'Weeklyvolratio', 'Weeklyrsi'
]
gtt_columns = [
    'Symbol', 'Last', 'Timestamp', '_chg_percentclose', 'dvol', '_avgvol_mln', '_bo_engulfing_cndl',
    '_circuit', '_days_since_bo', '_bo_dollar_vol_mln', '_rvol_to_float', 'Adr', 'Ti65',
    '_avg_vol_float_ratio', '_20madist', '_10wmadist', '_10madist',
    '_nr4', '_nr4_previous', '_rs', '_period_perf', '_insideday'
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
SECTOR_FILE = os.path.join(PROJECT_DIR, "TradingView", "Symbols_NSE.csv")

def load_column_prefs(table_key):
    if not supabase: return None
    try:
        r = supabase.table("column_prefs").select("visible_columns").eq("table_key", table_key).eq("user_id", "nse_user").execute()
        return r.data[0]['visible_columns'] if r.data else None
    except: return None

def save_column_prefs(table_key, cols):
    if not supabase: return
    try:
        ex = supabase.table("column_prefs").select("id").eq("table_key", table_key).eq("user_id", "nse_user").execute()
        if ex.data:
            supabase.table("column_prefs").update({"visible_columns": cols}).eq("table_key", table_key).eq("user_id", "nse_user").execute()
        else:
            supabase.table("column_prefs").insert({"user_id": "nse_user", "table_key": table_key, "visible_columns": cols}).execute()
    except Exception as e: st.warning(f"Could not save column preferences: {e}")

def get_persisted_columns(table_key, all_cols, default_hidden):
    dv = [c for c in all_cols if c not in default_hidden]
    s = load_column_prefs(table_key)
    if s is None: return dv
    s = [c for c in s if c in all_cols]
    return s if s else dv

def load_scoring_prefs(scanner_type):
    lf = os.path.join(BASE_DIR, f"{scanner_type.lower()}_scoring_prefs.json")
    if not supabase:
        if os.path.exists(lf):
            try:
                with open(lf, 'r') as f: return json.load(f)
            except: return {}
        return {}
    try:
        r = supabase.table("scoring_prefs").select("config").eq("user_id", "nse_user").eq("scanner_type", scanner_type).execute()
        return r.data[0]['config'] if r.data else {}
    except:
        if os.path.exists(lf):
            try:
                with open(lf, 'r') as f: return json.load(f)
            except: pass
        return {}

def save_scoring_prefs(prefs, scanner_type):
    lf = os.path.join(BASE_DIR, f"{scanner_type.lower()}_scoring_prefs.json")
    if not supabase:
        try:
            with open(lf, 'w') as f: json.dump(prefs, f, indent=2)
        except: pass
        return
    try:
        ex = supabase.table("scoring_prefs").select("id").eq("user_id", "nse_user").eq("scanner_type", scanner_type).execute()
        if ex.data:
            supabase.table("scoring_prefs").update({"config": prefs}).eq("user_id", "nse_user").eq("scanner_type", scanner_type).execute()
        else:
            supabase.table("scoring_prefs").insert({"user_id": "nse_user", "scanner_type": scanner_type, "config": prefs}).execute()
    except Exception as e:
        try:
            with open(lf, 'w') as f: json.dump(prefs, f, indent=2)
        except: pass

@st.cache_data(ttl=3600)
def load_sector_mapping(fp):
    if not os.path.exists(fp): return None
    df = pd.read_csv(fp)
    c = ['Symbol', 'Sector', 'Industry']
    df = df[[x for x in c if x in df.columns]]
    df['Symbol'] = df['Symbol'].astype(str).str.upper()
    return df

def get_file_age_days(fp):
    if not os.path.exists(fp): return None
    return int((datetime.now().timestamp() - os.path.getmtime(fp)) / 86400)

@st.cache_data(ttl=300)
def fetch_gtt_scan(url, name):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        if r.status_code == 200 and r.text.strip():
            df = pd.read_csv(StringIO(r.text), sep='|', header=None)
            if len(df.columns) < len(gtt_columns):
                df.columns = gtt_columns[:len(df.columns)]
            else:
                df.columns = gtt_columns + [f'Extra_{i}' for i in range(len(gtt_columns), len(df.columns))]
            df['Symbol'] = df['Symbol'].str.upper().str.replace('.NS', '', regex=False)
            for c in ['Last','_days_since_bo','_nr4','_rs','Adr','Ti65','dvol','_avgvol_mln','_bo_dollar_vol_mln','_bo_engulfing_cndl','_avg_vol_float_ratio','_insideday']:
                if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
            for c in ['_20madist','_10wmadist','_10madist','_nr4_previous']:
                if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
            return df
        return None
    except Exception as e:
        st.error(f"Error fetching {name}: {e}")
        return None

@st.cache_data(ttl=300)
def fetch_weekly_scan(url):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        if r.status_code == 200 and r.text.strip():
            n = len(weekly_metric_columns)
            rows = []
            for line in r.text.strip().split('\n'):
                line = line.strip()
                if not line: continue
                f = line.split('|')
                if len(f) < n + 2: continue
                rows.append([f[0], f[1]] + f[-n:])
            if not rows: return None
            df = pd.DataFrame(rows, columns=['Symbol', 'Last'] + weekly_metric_columns)
            df['Symbol'] = df['Symbol'].astype(str).str.upper().str.replace('.NS', '', regex=False).str.strip()
            for c in ['Last'] + weekly_metric_columns:
                df[c] = pd.to_numeric(df[c], errors='coerce')
            return df
        return None
    except Exception as e:
        st.error(f"Error fetching Weekly scan: {e}")
        return None

def _is_categorical(s):
    try:
        from pandas.api.types import is_categorical_dtype
        return is_categorical_dtype(s)
    except: return isinstance(s.dtype, pd.CategoricalDtype)

def clean_df_for_json(df):
    df = df.copy()
    df = df.replace([np.inf, -np.inf], np.nan)
    for c in df.columns:
        if df[c].isna().any():
            df[c] = df[c].astype(object)
            df.loc[df[c].isna(), c] = None
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].apply(lambda x: x.item() if hasattr(x, 'item') and x is not None else x)
    return df

# ════════════════════════════════════════════════════════════════════
# filter_dataframe — NEW: Two filter modes, no scoring dependency
# ════════════════════════════════════════════════════════════════════
def filter_dataframe(df, scan_mode, vol_bo_min_chg, vol_bo_min_vol, vol_bo_min_wktight,
                     vol_bo_min_adr, vol_bo_max_adr, vol_bo_max_rwd,
                     coil_min_wktight, coil_max_reltight, coil_max_rwd,
                     coil_min_chg, coil_max_chg):
    modify = st.checkbox("Add Advanced Filters")

    # ── Mode-specific checkbox ──
    find_vol_breakouts = False
    find_coiled = False

    if scan_mode == "Post Breakout":
        find_vol_breakouts = st.checkbox(
            f"Find Volume Breakouts (Up {vol_bo_min_chg}%+, Vol {vol_bo_min_vol}x+, WkTight ≥{vol_bo_min_wktight})",
            key="find_vol_bo")
    else:
        find_coiled = st.checkbox(
            f"Find Coiled Setups (WkTight ≥{coil_min_wktight}, RelTight ≤{coil_max_reltight}, on 10w EMA)",
            key="find_coiled")

    if not modify:
        if find_vol_breakouts:
            if '_chg_percentclose' in df.columns:
                df = df[df['_chg_percentclose'].fillna(0) >= vol_bo_min_chg]
            if 'dvol' in df.columns and '_avgvol_mln' in df.columns:
                vr = df['dvol'] / df['_avgvol_mln'].replace(0, np.nan)
                df = df[vr.fillna(0) >= vol_bo_min_vol]
            if 'W_TightCloses_10w' in df.columns:
                df = df[df['W_TightCloses_10w'].fillna(0) >= vol_bo_min_wktight]
            if 'Adr' in df.columns:
                df = df[(df['Adr'].fillna(0) >= vol_bo_min_adr) & (df['Adr'].fillna(0) <= vol_bo_max_adr)]
            if '_rel_wk_dist' in df.columns:
                df = df[df['_rel_wk_dist'].fillna(999) <= vol_bo_max_rwd]
            if 'dvol' in df.columns and '_avgvol_mln' in df.columns:
                df['_vol_ratio'] = (df['dvol'] / df['_avgvol_mln'].replace(0, np.nan)).round(2)
                df = df.sort_values(by='_vol_ratio', ascending=False, na_position='last')

        if find_coiled:
            if 'W_TightCloses_10w' in df.columns:
                df = df[df['W_TightCloses_10w'].fillna(0) >= coil_min_wktight]
            if '_rel_tightness' in df.columns:
                df = df[df['_rel_tightness'].fillna(999) <= coil_max_reltight]
            if '_rel_wk_dist' in df.columns:
                df = df[df['_rel_wk_dist'].fillna(999) <= coil_max_rwd]
            if '_chg_percentclose' in df.columns:
                df = df[(df['_chg_percentclose'].fillna(0) >= coil_min_chg) & (df['_chg_percentclose'].fillna(0) <= coil_max_chg)]
            if '_rel_tightness' in df.columns:
                df = df.sort_values(by='_rel_tightness', ascending=True, na_position='last')

        return df

    # ── Advanced Filters (per-column) ──
    df = df.copy()
    with st.container():
        tc = '_nr4' if scan_mode == "Anticipation" else '_nr4_previous'
        df2 = ['_chg_percentclose','Adr','Sector_Percentile','_avgvol_mln'] if scan_mode == "Post Breakout" else [tc,'Sector_Percentile','Adr','_avgvol_mln']
        to_filter = st.multiselect("Filter dataframe on", df.columns, default=df2)
        for column in to_filter:
            cs = df[column]; hn = cs.isna().any()
            if _is_categorical(cs) or cs.dropna().nunique() < 10:
                un = list(cs.dropna().unique()); NL = "(blank / NaN)"
                so = un + ([NL] if hn else [])
                ds = list(so)
                ui = st.multiselect(f"Values for {column}", so, default=ds)
                ns = NL in ui; rv = [v for v in ui if v != NL]
                df = df[(cs.isna() | cs.isin(rv)) if ns else (~cs.isna() & cs.isin(rv))]
            elif is_numeric_dtype(cs):
                cl = cs.dropna()
                if cl.empty: st.info(f"Column **{column}** has no numeric values"); continue
                _min = float(cl.min()); _max = float(cl.max())
                if _max <= _min: _max = _min + 0.1
                step = (_max - _min) / 100 if (_max - _min) > 0 else 0.1
                ui = st.slider(f"Values for {column}", _min, _max, (_min, _max), step=step)
                kn = st.checkbox(f"Keep rows where **{column}** is blank", value=True, key=f"kn_{column}") if hn else False
                ir = cs.between(*ui)
                df = df[(ir | cs.isna()) if kn else ir]
            else:
                ti = st.text_input(f"Substring or regex in {column}")
                if ti: df = df[cs.astype(str).str.contains(ti, case=False, na=False) | cs.isna()]

    # ── Apply mode-specific filter after advanced filters ──
    if find_vol_breakouts:
        if '_chg_percentclose' in df.columns: df = df[df['_chg_percentclose'].fillna(0) >= vol_bo_min_chg]
        if 'dvol' in df.columns and '_avgvol_mln' in df.columns:
            vr = df['dvol'] / df['_avgvol_mln'].replace(0, np.nan)
            df = df[vr.fillna(0) >= vol_bo_min_vol]
        if 'W_TightCloses_10w' in df.columns: df = df[df['W_TightCloses_10w'].fillna(0) >= vol_bo_min_wktight]
        if 'Adr' in df.columns: df = df[(df['Adr'].fillna(0) >= vol_bo_min_adr) & (df['Adr'].fillna(0) <= vol_bo_max_adr)]
        if '_rel_wk_dist' in df.columns: df = df[df['_rel_wk_dist'].fillna(999) <= vol_bo_max_rwd]
        if 'dvol' in df.columns and '_avgvol_mln' in df.columns:
            df['_vol_ratio'] = (df['dvol'] / df['_avgvol_mln'].replace(0, np.nan)).round(2)
            df = df.sort_values(by='_vol_ratio', ascending=False, na_position='last')

    if find_coiled:
        if 'W_TightCloses_10w' in df.columns: df = df[df['W_TightCloses_10w'].fillna(0) >= coil_min_wktight]
        if '_rel_tightness' in df.columns: df = df[df['_rel_tightness'].fillna(999) <= coil_max_reltight]
        if '_rel_wk_dist' in df.columns: df = df[df['_rel_wk_dist'].fillna(999) <= coil_max_rwd]
        if '_chg_percentclose' in df.columns:
            df = df[(df['_chg_percentclose'].fillna(0) >= coil_min_chg) & (df['_chg_percentclose'].fillna(0) <= coil_max_chg)]
        if '_rel_tightness' in df.columns:
            df = df.sort_values(by='_rel_tightness', ascending=True, na_position='last')

    return df


def main():
    st.markdown("""
    <style>
        html, body, [class*="css"], [class*="st-"] { font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif !important; }
        .main .block-container { padding-top: 2rem; padding-bottom: 2rem; max-width: 95% !important; }
        h1, h2, h3, h4 { font-weight: 700 !important; letter-spacing: -0.5px !important; margin-bottom: 0.5rem !important; margin-top: 1.5rem !important; }
        .stDataFrame { font-size: 14px !important; }
        details summary span:first-child { display: none !important; }
    </style>
    """, unsafe_allow_html=True)
    st.title("GTT Trade Generator (NSE)")

    fa = get_file_age_days(SECTOR_FILE)
    if fa is not None:
        if fa == 0: st.sidebar.success("Sector data loaded today.")
        elif fa <= 3: st.sidebar.info(f"Sector data loaded {fa} days ago.")
        else: st.sidebar.warning(f"Sector data loaded {fa} days ago. Update recommended!")
    else: st.sidebar.error("Symbols_NSE.csv not found!")

    st.sidebar.markdown("---")
    auto_refresh = st.sidebar.checkbox("Auto-refresh every 10 min", value=False, key="auto_refresh_toggle")
    refresh_clicked = st.sidebar.button("Refresh Now", key="manual_refresh_btn")
    if auto_refresh:
        ARI = 600
        if 'last_refresh_ts' not in st.session_state: st.session_state.last_refresh_ts = time.time()
        el = time.time() - st.session_state.last_refresh_ts
        if el >= ARI or refresh_clicked:
            st.session_state.last_refresh_ts = time.time(); st.cache_data.clear()
        el = time.time() - st.session_state.last_refresh_ts
        rem = max(0, int(ARI - el))
        lrd = datetime.fromtimestamp(st.session_state.last_refresh_ts)
        st.sidebar.caption(f"Last refreshed: {lrd.strftime('%H:%M:%S')}")
        m, s = divmod(rem, 60)
        st.sidebar.markdown(f"""<div style="font-size:13px;color:#888;">Next refresh in <span id="cd-m">{m}</span>m <span id="cd-s">{s:02d}</span>s</div><script>let t={rem};const a=document.getElementById('cd-m'),b=document.getElementById('cd-s');const x=setInterval(function(){{t--;if(t<=0){{clearInterval(x);window.top.location.reload();}}else{{a.textContent=Math.floor(t/60);b.textContent=(t%60<10?'0':'')+(t%60);}}}},1000);</script>""", unsafe_allow_html=True)
    else:
        if 'last_refresh_ts' in st.session_state: del st.session_state['last_refresh_ts']

    sector_df = load_sector_mapping(SECTOR_FILE)
    scan_mode = st.radio("Select Scanner Mode", ("Anticipation", "Post Breakout"), horizontal=True)
    if scan_mode == "Post Breakout":
        st.markdown("Find stocks breaking out from tight setups with volume confirmation.")
    else:
        st.markdown("Find coiled setups sitting on the 10w EMA. Watch for volume to arrive.")

    saved_prefs = load_scoring_prefs("NSE")

    # ════════════════════════════════════════════════════════════════════
    # FILTER SETTINGS (replaces scoring config)
    # ════════════════════════════════════════════════════════════════════
    st.sidebar.header("Filter Settings")

    vol_prefs = saved_prefs.get('vol_breakout', {})
    coil_prefs = saved_prefs.get('coiled', {})

    if scan_mode == "Post Breakout":
        st.sidebar.subheader("Volume Breakout Filters")
        vol_bo_min_chg = st.sidebar.number_input("Min Chg% (up on day)", value=float(vol_prefs.get('min_chg', 2.0)), step=0.5, key="vb_chg")
        vol_bo_min_vol = st.sidebar.number_input("Min Vol Ratio (x avg)", value=float(vol_prefs.get('min_vol', 1.5)), step=0.1, key="vb_vol")
        vol_bo_min_wktight = st.sidebar.number_input("Min W_TightCloses", value=int(vol_prefs.get('min_wktight', 1)), min_value=0, max_value=5, step=1, key="vb_wt")
        vol_bo_min_adr = st.sidebar.number_input("Min ADR", value=float(vol_prefs.get('min_adr', 3.0)), step=0.5, key="vb_adr1")
        vol_bo_max_adr = st.sidebar.number_input("Max ADR", value=float(vol_prefs.get('max_adr', 6.0)), step=0.5, key="vb_adr2")
        vol_bo_max_rwd = st.sidebar.number_input("Max Rel Wk Dist (ADRs from 10w EMA)", value=float(vol_prefs.get('max_rwd', 3.0)), step=0.5, key="vb_rwd")
        st.sidebar.caption(f"Colors: Vol 1-2.5x = light green, 2.5-3.5x = green, 3.5x+ = dark green bold")
    else:
        vol_bo_min_chg = 2.0; vol_bo_min_vol = 1.5; vol_bo_min_wktight = 1
        vol_bo_min_adr = 3.0; vol_bo_max_adr = 6.0; vol_bo_max_rwd = 3.0

    if scan_mode == "Anticipation":
        st.sidebar.subheader("Coiled Setup Filters")
        coil_min_wktight = st.sidebar.number_input("Min W_TightCloses", value=int(coil_prefs.get('min_wktight', 2)), min_value=0, max_value=5, step=1, key="cl_wt")
        coil_max_reltight = st.sidebar.number_input("Max Rel Tightness", value=float(coil_prefs.get('max_reltight', 0.8)), step=0.1, key="cl_rt")
        coil_max_rwd = st.sidebar.number_input("Max Rel Wk Dist (on 10w EMA)", value=float(coil_prefs.get('max_rwd', 1.0)), step=0.5, key="cl_rwd")
        coil_min_chg = st.sidebar.number_input("Min Chg% (not yet broken out)", value=float(coil_prefs.get('min_chg', -1.0)), step=0.5, key="cl_chg1")
        coil_max_chg = st.sidebar.number_input("Max Chg%", value=float(coil_prefs.get('max_chg', 3.0)), step=0.5, key="cl_chg2")
    else:
        coil_min_wktight = 2; coil_max_reltight = 0.8; coil_max_rwd = 1.0
        coil_min_chg = -1.0; coil_max_chg = 3.0

    # ── Save Settings ──
    if st.sidebar.button("Save filter settings", key="save_prefs"):
        prefs_to_save = {
            'vol_breakout': {'min_chg': vol_bo_min_chg, 'min_vol': vol_bo_min_vol, 'min_wktight': vol_bo_min_wktight,
                             'min_adr': vol_bo_min_adr, 'max_adr': vol_bo_max_adr, 'max_rwd': vol_bo_max_rwd},
            'coiled': {'min_wktight': coil_min_wktight, 'max_reltight': coil_max_reltight, 'max_rwd': coil_max_rwd,
                       'min_chg': coil_min_chg, 'max_chg': coil_max_chg},
        }
        save_scoring_prefs(prefs_to_save, "NSE")
        st.sidebar.success("Saved!")

    # ── Custom Sort ──
    ABS_SORT_COLS = {'W_Dist10wMA', '_rel_tightness', '_rel_wk_dist', '_vol_ratio', '_20madist', '_10madist'}
    sortable_columns = {
        '_vol_ratio': 'Vol Ratio', 'W_Dist10wMA': 'Wk Dist 10wMA', '_rel_wk_dist': 'Rel Wk Dist',
        '_rel_tightness': 'Rel Tightness', '_chg_percentclose': 'Chg %', 'Adr': 'ADR',
        'Avg_RS': 'Avg RS', 'W_TightCloses_10w': 'Wk Tight Closes', '_nr4': 'NR4',
        '_nr4_previous': 'NR4 Previous', '_20madist': '20MA Dist', '_10madist': '10MA Dist',
        'W_PctOf10wkHigh': 'Wk % of 10wHi', 'Sector_Percentile': 'Sector %ile',
    }
    with st.sidebar.expander("Custom Multi-Level Sort", expanded=False):
        use_custom_sort = st.checkbox("Enable custom sort", value=False, key="use_custom_sort")
        sort_levels = []
        if use_custom_sort:
            st.caption("For tightness/distance columns, sorting is by |value|.")
            for i in range(1, 4):
                col = st.selectbox(f"Sort Level {i}", options=['(skip)']+list(sortable_columns.keys()), index=0,
                                   format_func=lambda x: sortable_columns.get(x, '(skip)'), key=f"sl_{i}")
                if col == '(skip)': continue
                is_abs = col in ABS_SORT_COLS
                hig = {'_vol_ratio','Avg_RS','Adr','_chg_percentclose','W_TightCloses_10w','W_PctOf10wkHigh'}
                di = 1 if col in hig else 0
                d = st.radio(f"Direction{' (by |val|)' if is_abs else ''}", options=['Low to High','High to Low'],
                            index=di, key=f"sd_{i}", horizontal=True)
                sort_levels.append((col, d == 'Low to High'))

    st.subheader("Strategy & Risk Parameters")
    c1, c2, c3 = st.columns(3)
    with c1: account_equity = st.number_input("Total Account Equity ($)", min_value=10000, value=100000, step=10000)
    with c2: risk_pct = st.number_input("Max Risk Per Trade (%)", min_value=0.1, value=1.0, step=0.1)
    with c3: nr4_threshold = st.number_input("Max Tightness Range (NR4 %)", min_value=1.0, max_value=50.0, value=8.0, step=0.5)

    manual_fetch = st.button("Generate GTT Trading Plan", type="primary")
    auto_fetch = auto_refresh and ('gtt_base_df' in st.session_state)
    should_fetch = manual_fetch or auto_fetch or refresh_clicked

    if should_fetch:
        with st.spinner("Fetching and merging multi-timeframe scans..."):
            df_1m = fetch_gtt_scan(gtt_endpoints["1M"], "1M")
            df_3m = fetch_gtt_scan(gtt_endpoints["3M"], "3M")
            df_6m = fetch_gtt_scan(gtt_endpoints["6M"], "6M")
            if df_1m is not None and not df_1m.empty:
                d1r = df_1m.rename(columns={'_rs': 'RS_1M'})
                d3r = df_3m.rename(columns={'_rs': 'RS_3M'}) if df_3m is not None and not df_3m.empty else None
                d6r = df_6m.rename(columns={'_rs': 'RS_6M'}) if df_6m is not None and not df_6m.empty else None
                nrc = [c for c in d1r.columns if c not in ['Symbol','RS_1M','RS_3M','RS_6M']]
                bdf = d1r.copy()
                if d3r is not None:
                    bdf = bdf.merge(d3r, on='Symbol', how='outer', suffixes=('', '_3m'))
                    for c in nrc:
                        c3m = f'{c}_3m'
                        if c3m in bdf.columns: bdf[c] = bdf[c].fillna(bdf[c3m]); bdf.drop(c3m, axis=1, inplace=True)
                else: bdf['RS_3M'] = 0
                if d6r is not None:
                    bdf = bdf.merge(d6r, on='Symbol', how='outer', suffixes=('', '_6m'))
                    for c in nrc:
                        c6m = f'{c}_6m'
                        if c6m in bdf.columns: bdf[c] = bdf[c].fillna(bdf[c6m]); bdf.drop(c6m, axis=1, inplace=True)
                else: bdf['RS_6M'] = 0
                bdf['RS_1M'] = bdf['RS_1M'].fillna(0); bdf['RS_3M'] = bdf['RS_3M'].fillna(0); bdf['RS_6M'] = bdf['RS_6M'].fillna(0)
                if sector_df is not None:
                    bdf = bdf.merge(sector_df, on='Symbol', how='left')
                    bdf['Sector'] = bdf['Sector'].fillna('Unknown'); bdf['Industry'] = bdf['Industry'].fillna('Unknown')
                adf = bdf
                if not adf.empty:
                    adf['Avg_RS'] = adf[['RS_6M','RS_3M','RS_1M']].replace(0, np.nan).mean(axis=1).fillna(0).round(2)
                    if 'Sector' in adf.columns:
                        vm = adf['Sector'] != 'Unknown'
                        adf['Sector_Rank'] = 0; adf['Sector_Total'] = 0; adf['Sector_Percentile'] = 0.0
                        sc = adf[vm].groupby('Sector')['Symbol'].count()
                        adf.loc[vm, 'Sector_Rank'] = adf[vm].groupby('Sector')['Avg_RS'].rank(ascending=False, method='min').astype(int)
                        adf.loc[vm, 'Sector_Total'] = adf.loc[vm, 'Sector'].map(sc).astype(int)
                        adf.loc[vm, 'Sector_Percentile'] = ((adf.loc[vm,'Sector_Total'] - adf.loc[vm,'Sector_Rank'] + 1) / adf.loc[vm,'Sector_Total'] * 100).round(1)
                    for c in ['RS_1M','RS_3M','RS_6M','Adr','Ti65','_nr4','dvol','_avgvol_mln','_bo_dollar_vol_mln','_avg_vol_float_ratio']:
                        if c in adf.columns: adf[c] = adf[c].round(2)
                wdf = fetch_weekly_scan(weekly_endpoint)
                if wdf is not None and not wdf.empty:
                    wf = wdf.copy()
                    if sector_df is not None:
                        wf = wf.merge(sector_df, on='Symbol', how='left')
                        wf['Sector'] = wf['Sector'].fillna('Unknown'); wf['Industry'] = wf['Industry'].fillna('Unknown')
                    st.session_state.weekly_full_df = wf
                    ws = wdf[['Symbol','Pctof10wkhigh','Weeklyclose_chg_pct','Tightcloses_10w_of5','Insidebars_of8','Dist_wema10_pct']].rename(columns={
                        'Pctof10wkhigh':'W_PctOf10wkHigh','Weeklyclose_chg_pct':'W_CloseChg_Pct','Tightcloses_10w_of5':'W_TightCloses_10w',
                        'Insidebars_of8':'W_InsideBars','Dist_wema10_pct':'W_Dist10wMA'})
                    adf = adf.merge(ws, on='Symbol', how='left')
                else:
                    st.session_state.weekly_full_df = None; st.warning("Weekly scan unavailable.")
                st.session_state.gtt_base_df = adf
            else:
                st.error("Failed to retrieve base 1M scan data."); st.session_state.gtt_base_df = None

    tab1, tab2, tab3 = st.tabs(["GTT Scanner", "Market Themes & Leaders", "Saved Breakouts"])

    # ════════════════════════════════════════════════════════════════════
    # TAB 1: SCANNER (NO SCORING — just computed columns + filters)
    # ════════════════════════════════════════════════════════════════════
    with tab1:
        if 'gtt_base_df' in st.session_state and st.session_state.gtt_base_df is not None:
            adf = st.session_state.gtt_base_df.copy()

            # ── Compute derived columns (no scoring) ──
            if 'Adr' in adf.columns:
                safe_adr = adf['Adr'].replace(0, np.nan)
                tc = '_nr4' if scan_mode == "Anticipation" else '_nr4_previous'
                if tc in adf.columns:
                    adf['_rel_tightness'] = (adf[tc] / safe_adr).round(2)
                if 'W_Dist10wMA' in adf.columns:
                    adf['_rel_wk_dist'] = (adf['W_Dist10wMA'].fillna(999).abs() / safe_adr).round(2)
            if 'dvol' in adf.columns and '_avgvol_mln' in adf.columns:
                adf['_vol_ratio'] = (adf['dvol'] / adf['_avgvol_mln'].replace(0, np.nan)).round(2)

            st.session_state.gtt_scored_df = adf.copy()

            columns_to_show = [
                '_vol_ratio', '_chg_percentclose', 'W_Dist10wMA', '_rel_wk_dist',
                'W_TightCloses_10w', 'W_PctOf10wkHigh', 'W_InsideBars', 'W_CloseChg_Pct',
                '_nr4_previous', '_rel_tightness',
                'dvol', '_avgvol_mln', '_20madist', '_10madist',
                'Symbol', 'Sector', 'Industry', 'Avg_RS', 'RS_6M', 'RS_3M', 'RS_1M',
                'Adr', 'Ti65', '_nr4', 'Last',
                '_bo_engulfing_cndl', '_days_since_bo', '_bo_dollar_vol_mln', '_circuit',
                '_avg_vol_float_ratio', '_period_perf', '_10wmadist', '_insideday', 'Timestamp',
            ]
            if 'Sector_Rank' in adf.columns: columns_to_show.insert(columns_to_show.index('Symbol')+1, 'Sector_Rank')
            if 'Sector_Total' in adf.columns: columns_to_show.insert(columns_to_show.index('Sector_Rank')+1, 'Sector_Total')
            if 'Sector_Percentile' in adf.columns: columns_to_show.insert(columns_to_show.index('Sector_Total')+1, 'Sector_Percentile')

            vc = [c for c in columns_to_show if c in adf.columns]
            ddf = adf[vc].copy()

            # ── Default sort ──
            if use_custom_sort and len(sort_levels) > 0:
                sbc = []; sa = []; tcd = []
                for col, asc in sort_levels:
                    if col not in ddf.columns: continue
                    ddf[col] = pd.to_numeric(ddf[col], errors='coerce')
                    if col in ABS_SORT_COLS:
                        tc2 = f'_abs_{col}'; ddf[tc2] = ddf[col].abs(); sbc.append(tc2); tcd.append(tc2)
                    else: sbc.append(col)
                    sa.append(asc)
                if sbc:
                    ddf = ddf.sort_values(by=sbc, ascending=sa, na_position='last')
                    ddf = ddf.drop(columns=tcd, errors='ignore')
            else:
                # Default: breakout mode → vol_ratio desc, anticipation mode → rel_tightness asc
                if scan_mode == "Post Breakout" and '_vol_ratio' in ddf.columns:
                    ddf = ddf.sort_values(by='_vol_ratio', ascending=False, na_position='last')
                elif '_rel_tightness' in ddf.columns:
                    ddf = ddf.sort_values(by='_rel_tightness', ascending=True, na_position='last')

            st.session_state.gtt_display_df = ddf
            st.success(f"Loaded {len(ddf)} stocks. Use the checkboxes to filter.")

            fdf = filter_dataframe(ddf, scan_mode, vol_bo_min_chg, vol_bo_min_vol, vol_bo_min_wktight,
                                   vol_bo_min_adr, vol_bo_max_adr, vol_bo_max_rwd,
                                   coil_min_wktight, coil_max_reltight, coil_max_rwd,
                                   coil_min_chg, coil_max_chg)

            st.caption(f"**{len(fdf)} stocks** after filtering.")

            mtdh = ['RS_6M','RS_3M','RS_1M','Industry','_bo_dollar_vol_mln','_avg_vol_float_ratio','_period_perf',
                    '_10wmadist','_insideday','_bo_engulfing_cndl','_days_since_bo','_circuit','W_CloseChg_Pct','Timestamp']
            amc = list(fdf.columns)
            with st.expander("Choose visible columns (saved as your default)"):
                smc = st.multiselect("Columns to show", amc, default=get_persisted_columns('nse_main_table', amc, mtdh), key="main_col_sel")
                if st.button("Save as default", key="save_cols"): save_column_prefs('nse_main_table', smc); st.success("Saved.")
            hmc = [c for c in amc if c not in smc]
            cp = {c: i for i, c in enumerate(columns_to_show) if c in fdf.columns}
            smc = sorted(smc, key=lambda c: cp.get(c, 9999)); hmc = sorted(hmc, key=lambda c: cp.get(c, 9999))
            fdf = fdf[smc + hmc]

            gb = GridOptionsBuilder.from_dataframe(fdf)
            gb.configure_default_column(resizable=True, filterable=True, sortable=True, minWidth=70, flex=0)
            gb.configure_side_bar(); gb.configure_grid_options(enableBrowserTooltips=True)
            gb.configure_selection(selection_mode='multiple', use_checkbox=True)
            for col in fdf.columns: gb.configure_column(col, headerTooltip=col)

            abs_comparator = JsCode("""
                function(a, b, na, nb, inv) {
                    const x = (a === null || a === undefined || isNaN(a)) ? Infinity : Math.abs(a);
                    const y = (b === null || b === undefined || isNaN(b)) ? Infinity : Math.abs(b);
                    return x < y ? -1 : x > y ? 1 : 0;
                }
            """)

            # ── Volume Ratio Heatmap (NEW: 2.5x / 3.5x thresholds) ──
            if '_vol_ratio' in fdf.columns:
                vol_jscode = JsCode("""
                    function(params) {
                        const v = params.value;
                        if (v === null || v === undefined || isNaN(v) || v <= 0) return null;
                        if (v >= 3.5) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                        if (v >= 2.5) return { 'backgroundColor': '#8ee68e', 'color': 'black', 'fontWeight': 'bold' };
                        if (v >= 1.5) return { 'backgroundColor': '#d4edda', 'color': 'black' };
                        if (v >= 1.0) return { 'backgroundColor': '#f8f9fa', 'color': 'black' };
                        if (v < 0.5) return { 'backgroundColor': '#f8d7da', 'color': '#721c24' };
                        return null;
                    }
                """)
                gb.configure_column('_vol_ratio', minWidth=60, maxWidth=85, headerName='Vol Ratio', cellStyle=vol_jscode)

            # ── Also style dvol and _avgvol_mln with the same vol ratio coloring ──
            if 'dvol' in fdf.columns and '_avgvol_mln' in fdf.columns:
                gb.configure_column('dvol', minWidth=60, maxWidth=85, headerName='dvol', cellStyle=vol_jscode)
                gb.configure_column('_avgvol_mln', minWidth=60, maxWidth=85, headerName='AvgVol', cellStyle=vol_jscode)

            # ── W_Dist10wMA Heatmap ──
            wk_neg_cutoff = float(saved_prefs.get('wk_neg_cutoff', -3.0))
            wk_dist_jscode = JsCode(f"""
                function(params) {{
                    const val = params.value;
                    if (val === null || val === undefined || isNaN(val)) return null;
                    if (val < {wk_neg_cutoff}) return {{ 'backgroundColor': '#f8d7da', 'color': '#721c24', 'fontWeight': 'bold' }};
                    if (val < 0) return {{ 'backgroundColor': '#fff3cd', 'color': '#664d03' }};
                    if (val < 2) return {{ 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' }};
                    if (val < 4) return {{ 'backgroundColor': '#8ee68e', 'color': 'black' }};
                    if (val < 6) return {{ 'backgroundColor': '#d4edda', 'color': 'black' }};
                    if (val < 10) return {{ 'backgroundColor': '#fff3cd', 'color': '#664d03' }};
                    return null;
                }}
            """)
            if 'W_Dist10wMA' in fdf.columns:
                gb.configure_column('W_Dist10wMA', minWidth=80, maxWidth=110, headerName='Wk 10wMA %', cellStyle=wk_dist_jscode, comparator=abs_comparator)

            # ── _rel_wk_dist Heatmap ──
            rel_wk_dist_jscode = JsCode("""
                function(params) {
                    const v = params.value;
                    if (v === null || v === undefined || isNaN(v)) return null;
                    if (v < 1.0) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                    if (v < 2.0) return { 'backgroundColor': '#8ee68e', 'color': 'black' };
                    if (v < 3.0) return { 'backgroundColor': '#d4edda', 'color': 'black' };
                    if (v < 5.0) return { 'backgroundColor': '#fff3cd', 'color': '#664d03' };
                    return { 'backgroundColor': '#f8d7da', 'color': '#721c24' };
                }
            """)
            if '_rel_wk_dist' in fdf.columns:
                gb.configure_column('_rel_wk_dist', minWidth=70, maxWidth=90, headerName='Rel Wk Dist', cellStyle=rel_wk_dist_jscode, comparator=abs_comparator)

            # ── _rel_tightness Heatmap ──
            th = JsCode("""function(p){return{'backgroundColor':'#fff3cd','color':'#664d03','fontWeight':'bold'};}""")
            if '_rel_tightness' in fdf.columns:
                gb.configure_column('_rel_tightness', minWidth=70, maxWidth=90, headerName='Rel Tight', cellStyle=th, comparator=abs_comparator)

            # ── Chg% Heatmap ──
            if '_chg_percentclose' in fdf.columns:
                vc2 = fdf[fdf['_chg_percentclose'] > 0]['_chg_percentclose']
                cmn = float(vc2.min()) if not vc2.empty else 0.0; cmx = float(vc2.max()) if not vc2.empty else 10.0
                cj = JsCode(f"""function(p){{const v=p.value;if(!v||v<=0)return null;const m={cmn},x={cmx};if(x===m)return{{'backgroundColor':'#ffe6ff','color':'black'}};const r=Math.min((v-m)/(x-m),1.0);const a=Math.round(255-115*r),b=Math.round(220-220*r),c=Math.round(255-115*r);return{{'backgroundColor':'rgb('+a+','+b+','+c+')','color':r>0.5?'white':'black','fontWeight':r>=0.8?'bold':'normal'}}}}""")
                gb.configure_column('_chg_percentclose', minWidth=80, maxWidth=110, cellStyle=cj, filter='agNumberColumnFilter',
                    filterParams={'filterOptions':['greaterThan','lessThan','equals','inRange'],'defaultOption':'greaterThan','defaultValues':[0]})

            # ── RS columns ──
            for col in ['Avg_RS','RS_6M','RS_3M','RS_1M']:
                if col not in fdf.columns: continue
                vd = fdf[fdf[col] > 0][col]
                cmn = vd.min() if not vd.empty else 0; cmx = vd.max() if not vd.empty else 100
                dj = JsCode(f"""function(p){{const v=p.value;if(v<=0)return null;const m={cmn},x={cmx};if(x===m)return{{'backgroundColor':'#fff','color':'black'}};const r=(v-m)/(x-m);let a,b,c;if(r<0.5){{const p2=r/0.5;a=255;b=Math.round(100+155*p2);c=Math.round(100+155*p2)}}else{{const p2=(r-0.5)/0.5;a=Math.round(255-155*p2);b=255;c=Math.round(255-155*p2)}}return{{'backgroundColor':'rgb('+a+','+b+','+c+')','color':'black','fontWeight':r>=0.9?'bold':'normal'}}}}""")
                gb.configure_column(col, minWidth=60 if col == 'Avg_RS' else 50, maxWidth=90 if col == 'Avg_RS' else 80, cellStyle=dj)

            # ── Other columns ──
            for col in ['Adr','Ti65','_nr4']:
                if col in fdf.columns: gb.configure_column(col, minWidth=55, maxWidth=75)

            # ── MA Distance heatmaps ──
            md = JsCode("""function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;const a=Math.abs(v);if(v<-6)return{'backgroundColor':'#f8d7da','color':'#721c24','fontWeight':'bold'};if(a<2)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(a<4)return{'backgroundColor':'#8ee68e','color':'black'};if(a<6)return{'backgroundColor':'#d4edda','color':'black'};return null}""")
            if '_20madist' in fdf.columns: gb.configure_column('_20madist', minWidth=70, maxWidth=90, cellStyle=md, comparator=abs_comparator)
            if '_10madist' in fdf.columns: gb.configure_column('_10madist', minWidth=70, maxWidth=90, cellStyle=md, comparator=abs_comparator)

            for col in ['_bo_dollar_vol_mln','_avg_vol_float_ratio']:
                if col in fdf.columns: gb.configure_column(col, minWidth=70, maxWidth=110)

            # ── Weekly columns ──
            wp = JsCode("""function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;if(v>=1.0)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(v>=0.95)return{'backgroundColor':'#8ee68e','color':'black'};if(v>=0.85)return{'backgroundColor':'#d4edda','color':'black'};return null}""")
            if 'W_PctOf10wkHigh' in fdf.columns: gb.configure_column('W_PctOf10wkHigh', headerName='Wk % of 10wHi', minWidth=95, maxWidth=120, cellStyle=wp)
            if 'W_CloseChg_Pct' in fdf.columns: gb.configure_column('W_CloseChg_Pct', headerName='Wk CloseChg%', minWidth=90, maxWidth=115)
            if 'W_TightCloses_10w' in fdf.columns: gb.configure_column('W_TightCloses_10w', headerName='Wk Tight 10w/5', minWidth=85, maxWidth=105)
            if 'W_InsideBars' in fdf.columns: gb.configure_column('W_InsideBars', headerName='Wk InsideB/8', minWidth=85, maxWidth=105)

            # ── Header shortening ──
            hs = {'_chg_percentclose':'Chg %','_avgvol_mln':'AvgVolcr','_bo_dollar_vol_mln':'BO$Volcr','_avg_vol_float_ratio':'VolFloatR',
                  '_bo_engulfing_cndl':'BOEngulf','_days_since_bo':'DaysSinceBO','Sector_Percentile':'SectPctile',
                  '_nr4_previous':'NR4Prev','_period_perf':'PeriodPerf','_10wmadist':'10wMADist','_10madist':'10MADist',
                  '_20madist':'20MADist','_insideday':'InsideDay','W_Dist10wMA':'Wk 10wMA %','_rel_wk_dist':'Rel Wk Dist',
                  'W_PctOf10wkHigh':'Wk % of 10wHi','W_CloseChg_Pct':'Wk CloseChg%','W_TightCloses':'Wk TightCl/5',
                  'W_InsideBars':'Wk InsideB/8','_vol_ratio':'Vol Ratio','_rel_tightness':'Rel Tight'}
            for rc, sn in hs.items():
                if rc in fdf.columns: gb.configure_column(rc, headerName=sn)

            # ── Symbol renderer ──
            sr = JsCode("""function(p){const s=p.value;const r=p.data.Sector_Rank;const t=p.data.Sector_Total;if(r&&t&&r>0)return s+' ('+r+'/'+t+')';return s}""")
            gb.configure_column('Symbol', cellRenderer=sr, minWidth=150, maxWidth=180, pinned='left', checkboxSelection=True)
            if 'Sector' in fdf.columns: gb.configure_column('Sector', minWidth=120, maxWidth=150)
            if 'Industry' in fdf.columns: gb.configure_column('Industry', minWidth=120, maxWidth=150)
            if 'Sector_Rank' in fdf.columns: gb.configure_column('Sector_Rank', hide=True)
            if 'Sector_Total' in fdf.columns: gb.configure_column('Sector_Total', hide=True)
            if 'Sector_Percentile' in fdf.columns: gb.configure_column('Sector_Percentile', minWidth=100, maxWidth=120)
            for col in hmc: gb.configure_column(col, hide=True)

            go = gb.build()
            safe_df = clean_df_for_json(fdf)
            grid_response = AgGrid(safe_df, gridOptions=go, height=600, width='100%', update_mode=GridUpdateMode.MODEL_CHANGED, data_return_mode=DataReturnMode.FILTERED_AND_SORTED, allow_unsafe_jscode=True)

            # ── Export ──
            st.markdown("---"); st.subheader("Export Scanner Data for Analysis")
            cd1, cd2 = st.columns(2)
            with cd1:
                ef = fdf.copy()
                st.download_button("Download Filtered Data (CSV)", ef.to_csv(index=False), f"gtt_filtered_{datetime.now().strftime('%Y%m%d_%H%M')}.csv", "text/csv")
                st.caption(f"{len(ef)} rows, {len(ef.columns)} columns")
            with cd2:
                if 'gtt_scored_df' in st.session_state and st.session_state.gtt_scored_df is not None:
                    efl = st.session_state.gtt_scored_df.copy()
                    st.download_button("Download Full Dataset (CSV)", efl.to_csv(index=False), f"gtt_full_{datetime.now().strftime('%Y%m%d_%H%M')}.csv", "text/csv")
                    st.caption(f"{len(efl)} rows, {len(efl.columns)} columns")

            # ── Selected rows → Saved Breakouts ──
            sr2 = grid_response['selected_rows']
            if sr2 is not None and len(sr2) > 0:
                ss = sr2['Symbol'].tolist() if isinstance(sr2, pd.DataFrame) else [r.get('Symbol') for r in sr2 if r]
                st.markdown(f"**{len(ss)} symbols selected.**")
                if st.button("Add selected to Saved Breakouts"):
                    try:
                        r = supabase.table("saved_breakouts").select("symbol").eq("user_id", "nse_user").execute()
                        es = [b['symbol'] for b in r.data]
                    except: es = []
                    na = 0
                    for s in ss:
                        if s not in es:
                            try: supabase.table("saved_breakouts").insert({"symbol":s,"saved_date":datetime.now().strftime("%Y-%m-%d %H:%M"),"user_id":"nse_user"}).execute(); na += 1
                            except Exception as e: st.error(f"Failed to save {s}: {e}")
                    if na > 0: st.success(f"Added {na} new ticker(s)!")
                    else: st.info("All selected tickers already saved.")

            # ── Copy to TradingView (grouped by volume strength) ──
            sdf = grid_response['data'] if grid_response and 'data' in grid_response and not grid_response['data'].empty else fdf
            if not sdf.empty and 'Symbol' in sdf.columns:
                al = sdf['Symbol'].dropna().unique().tolist(); atv = ",".join([f"nse:{s}" for s in al])
                st.markdown("---"); st.subheader("Copy Symbols to TradingView")
                cc1, cc2, cc3 = st.columns(3)
                if '_vol_ratio' in sdf.columns:
                    strong = sdf[sdf['_vol_ratio'].fillna(0) >= 3.5]['Symbol'].dropna().unique().tolist()
                    moderate = sdf[(sdf['_vol_ratio'].fillna(0) >= 1.5) & (sdf['_vol_ratio'].fillna(0) < 3.5)]['Symbol'].dropna().unique().tolist()
                    stv = ",".join([f"nse:{s}" for s in strong])
                    mtv = ",".join([f"nse:{s}" for s in moderate])
                else:
                    strong = []; moderate = al
                    stv = ""; mtv = atv
                with cc1:
                    st.markdown(f"**Strong Breakouts (Vol ≥3.5x)** — `{len(strong)} symbols`")
                    if strong: st.code(stv, language=None); st.caption(f"Click to copy {len(strong)} symbols.")
                    else: st.info("No strong breakouts.")
                with cc2:
                    st.markdown(f"**Moderate (Vol 1.5-3.5x)** — `{len(moderate)} symbols`")
                    if moderate: st.code(mtv, language=None); st.caption(f"Click to copy {len(moderate)} symbols.")
                    else: st.info("No moderate breakouts.")
                with cc3:
                    st.markdown(f"**All filtered** — `{len(al)} symbols`")
                    if al:
                        if st.button("Copy All", key="copy_all"): st.code(atv, language=None); st.caption(f"Click to copy {len(al)} symbols.")
                    else: st.info("No symbols in view.")
        else:
            st.info("Click 'Generate GTT Trading Plan' to load data.")

    # ════════════════════════════════════════════════════════════════════
    # TAB 2: MARKET THEMES & LEADERS
    # ════════════════════════════════════════════════════════════════════
    with tab2:
        if 'gtt_scored_df' in st.session_state and st.session_state.gtt_scored_df is not None:
            sdf = st.session_state.gtt_scored_df.copy()
            if 'Sector' in sdf.columns and '_vol_ratio' in sdf.columns:
                # Show top stocks by volume per sector
                top_stocks = sdf[sdf['_vol_ratio'].fillna(0) >= 1.0].copy()
                if not top_stocks.empty:
                    ss = top_stocks.groupby('Sector').agg(
                        Count=('Symbol','count'),
                        Avg_Vol_Ratio=('_vol_ratio','mean'),
                        Avg_RS=('Avg_RS','mean'),
                        Avg_Adr=('Adr','mean'),
                    ).round(2).sort_values('Count', ascending=False).reset_index()

                    st.subheader("Sector Concentration (stocks with Vol ≥ 1.0x)")
                    sgb = GridOptionsBuilder.from_dataframe(ss); sgb.configure_default_column(resizable=True,filterable=True,sortable=True,minWidth=70,flex=0)
                    sgb.configure_side_bar(); sgb.configure_grid_options(enableBrowserTooltips=True)
                    for c in ss.columns: sgb.configure_column(c, headerTooltip=c)
                    if 'Avg_Vol_Ratio' in ss.columns:
                        sgb.configure_column('Avg_Vol_Ratio', minWidth=90, maxWidth=120, headerName='Avg Vol Ratio',
                            cellStyle=JsCode("""function(p){const v=p.value;if(v===null||v===undefined||isNaN(v)||v<=0)return null;if(v>=3.5)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(v>=2.5)return{'backgroundColor':'#8ee68e','color':'black','fontWeight':'bold'};if(v>=1.5)return{'backgroundColor':'#d4edda','color':'black'};return null}"""))
                    sgb.configure_column('Sector', minWidth=140, maxWidth=200, pinned='left')
                    AgGrid(ss, gridOptions=sgb.build(), height=400, width='100%', update_mode=GridUpdateMode.MODEL_CHANGED, data_return_mode=DataReturnMode.FILTERED_AND_SORTED, allow_unsafe_jscode=True)

                    st.subheader("Top Setups by Sector")
                    for sec in ss.head(10)['Sector'].tolist():
                        ss2 = top_stocks[top_stocks['Sector']==sec].sort_values('_vol_ratio', ascending=False, na_position='last').head(15)
                        dc = [c for c in ['Symbol','Last','_chg_percentclose','_vol_ratio','W_Dist10wMA','_rel_wk_dist','W_TightCloses_10w','_rel_tightness','Adr','Ti65','_nr4','_nr4_previous','dvol','_avgvol_mln','_20madist','_10madist','Avg_RS','RS_6M','RS_3M','RS_1M','Sector','Industry','Sector_Rank','Sector_Total','Sector_Percentile'] if c in ss2.columns]
                        sd = ss2[dc].copy()
                        with st.expander(f"{sec} ({len(ss2)} stocks)"):
                            gb = GridOptionsBuilder.from_dataframe(sd); gb.configure_default_column(resizable=True,filterable=True,sortable=True,minWidth=70,flex=0)
                            gb.configure_side_bar(); gb.configure_grid_options(enableBrowserTooltips=True)
                            for c in sd.columns: gb.configure_column(c, headerTooltip=c)
                            if '_vol_ratio' in sd.columns: gb.configure_column('_vol_ratio', minWidth=60, maxWidth=85, headerName='Vol Ratio', cellStyle=vol_jscode if '_vol_ratio' in fdf.columns else None)
                            if 'dvol' in sd.columns and '_avgvol_mln' in sd.columns:
                                gb.configure_column('dvol', minWidth=60, maxWidth=85, cellStyle=vol_jscode if 'dvol' in fdf.columns else None)
                                gb.configure_column('_avgvol_mln', minWidth=60, maxWidth=85, cellStyle=vol_jscode if '_avgvol_mln' in fdf.columns else None)
                            if '_rel_tightness' in sd.columns: gb.configure_column('_rel_tightness', minWidth=70, maxWidth=90, headerName='Rel Tight', cellStyle=th, comparator=abs_comparator)
                            if '_rel_wk_dist' in sd.columns: gb.configure_column('_rel_wk_dist', minWidth=70, maxWidth=90, headerName='Rel Wk Dist', cellStyle=rel_wk_dist_jscode, comparator=abs_comparator)
                            if '_chg_percentclose' in sd.columns:
                                vc2=sd[sd['_chg_percentclose']>0]['_chg_percentclose']; cmn=float(vc2.min()) if not vc2.empty else 0; cmx=float(vc2.max()) if not vc2.empty else 10
                                gb.configure_column('_chg_percentclose', minWidth=80, maxWidth=110, cellStyle=JsCode(f"""function(p){{const v=p.value;if(!v||v<=0)return null;const m={cmn},x={cmx};if(x===m)return{{'backgroundColor':'#ffe6ff','color':'black'}};const r=Math.min((v-m)/(x-m),1.0);const a=Math.round(255-115*r),b=Math.round(220-220*r),c=Math.round(255-115*r);return{{'backgroundColor':'rgb('+a+','+b+','+c+')','color':r>0.5?'white':'black','fontWeight':r>=0.8?'bold':'normal'}}}}"""), filter='agNumberColumnFilter', filterParams={'filterOptions':['greaterThan','lessThan','equals','inRange'],'defaultOption':'greaterThan','defaultValues':[0]})
                            for col in ['Adr','Ti65','_nr4']:
                                if col in sd.columns: gb.configure_column(col, minWidth=55, maxWidth=75)
                            md2=JsCode("""function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;const a=Math.abs(v);if(v<-6)return{'backgroundColor':'#f8d7da','color':'#721c24','fontWeight':'bold'};if(a<2)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(a<4)return{'backgroundColor':'#8ee68e','color':'black'};if(a<6)return{'backgroundColor':'#d4edda','color':'black'};return null}""")
                            if '_20madist' in sd.columns: gb.configure_column('_20madist', minWidth=70, maxWidth=90, cellStyle=md2, comparator=abs_comparator)
                            if '_10madist' in sd.columns: gb.configure_column('_10madist', minWidth=70, maxWidth=90, cellStyle=md2, comparator=abs_comparator)
                            if 'W_Dist10wMA' in sd.columns: gb.configure_column('W_Dist10wMA', minWidth=80, maxWidth=110, headerName='Wk 10wMA %', cellStyle=wk_dist_jscode, comparator=abs_comparator)
                            hs2={'_chg_percentclose':'Chg %','_avgvol_mln':'AvgVol','_nr4_previous':'NR4Prev','_10madist':'10MADist','_20madist':'20MADist','W_Dist10wMA':'Wk 10wMA%','_rel_wk_dist':'RelWkDist','_vol_ratio':'VolRatio'}
                            for rc,sn in hs2.items():
                                if rc in sd.columns: gb.configure_column(rc, headerName=sn)
                            if 'Symbol' in sd.columns: gb.configure_column('Symbol', cellRenderer=sr, minWidth=150, maxWidth=180, pinned='left')
                            if 'Sector_Rank' in sd.columns: gb.configure_column('Sector_Rank', hide=True)
                            if 'Sector_Total' in sd.columns: gb.configure_column('Sector_Total', hide=True)
                            if 'Sector_Percentile' in sd.columns: gb.configure_column('Sector_Percentile', minWidth=100, maxWidth=120)
                            if 'Sector' in sd.columns: gb.configure_column('Sector', minWidth=120, maxWidth=150)
                            if 'Industry' in sd.columns: gb.configure_column('Industry', minWidth=120, maxWidth=150)
                            AgGrid(clean_df_for_json(sd), gridOptions=gb.build(), height=400, width='100%', update_mode=GridUpdateMode.MODEL_CHANGED, data_return_mode=DataReturnMode.FILTERED_AND_SORTED, allow_unsafe_jscode=True)
                else:
                    st.info("No stocks with volume above average found.")
            else:
                st.info("No sector data available.")
        else:
            st.info("Generate data first.")

    # ════════════════════════════════════════════════════════════════════
    # TAB 3: SAVED BREAKOUTS
    # ════════════════════════════════════════════════════════════════════
    with tab3:
        st.subheader("Saved Exceptional Breakouts")
        st.caption("Track multi-day bases and retests. Stored securely in your Supabase cloud database.")
        try:
            r = supabase.table("saved_breakouts").select("*").eq("user_id", "nse_user").execute()
            saved_breakouts = r.data
        except Exception as e:
            saved_breakouts = []; st.error(f"Database error: {e}")
        c1, c2 = st.columns([3, 1])
        with c1: new_ticker = st.text_input("Enter Ticker to Track manually:", key="save_ticker_input").upper().strip()
        with c2:
            st.write(""); st.write("")
            if st.button("Save Ticker", key="save_ticker_btn") and new_ticker:
                if not any(b['symbol'] == new_ticker for b in saved_breakouts):
                    try:
                        supabase.table("saved_breakouts").insert({"symbol": new_ticker, "saved_date": datetime.now().strftime("%Y-%m-%d %H:%M"), "user_id": "nse_user"}).execute()
                        st.success(f"Saved {new_ticker}!"); st.rerun()
                    except Exception as e: st.error(f"Failed: {e}")
                else: st.warning("Already saved.")
        st.markdown("---")
        if 'gtt_scored_df' in st.session_state and st.session_state.gtt_scored_df is not None and saved_breakouts:
            ldf = st.session_state.gtt_scored_df.copy()
            sdf2 = pd.DataFrame(saved_breakouts).rename(columns={'symbol':'Symbol','saved_date':'Saved_On'})
            mdf = sdf2[['Symbol','Saved_On']].merge(ldf, on='Symbol', how='left')
            mdf['Status'] = mdf['_vol_ratio'].apply(lambda x: 'Active (Vol ≥1x)' if pd.notna(x) and x >= 1.0 else ('In Scanner' if pd.notna(x) else 'Dropped'))
            ct3 = ['Symbol','Saved_On','Status','_vol_ratio','_chg_percentclose','W_Dist10wMA','_rel_wk_dist','_rel_tightness','Adr','Ti65','_nr4','Avg_RS','Sector','Sector_Percentile','_avgvol_mln','_20madist','_10madist','W_TightCloses_10w','W_PctOf10wkHigh','Last']
            ac3 = [c for c in ct3 if c in mdf.columns]; mdf = mdf[ac3]
            for c in mdf.columns:
                if c not in ['Symbol','Saved_On','Status']: mdf[c] = mdf[c].fillna('N/A')
            st.markdown("---"); st.subheader("Copy Saved Symbols to TradingView")
            als = mdf['Symbol'].dropna().unique().tolist(); atv = ",".join([f"nse:{s}" for s in als])
            cc1, cc2 = st.columns([1, 2])
            with cc1:
                if st.button("Copy All Saved Symbols"): st.code(atv, language=None); st.caption(f"Click to copy {len(als)} symbols.")
            st.markdown("---"); st.markdown("#### Manage Watchlist")
            ctd = st.multiselect("Select tickers to remove:", mdf['Symbol'].tolist(), key="remove_saved")
            if st.button("Remove Selected", key="remove_saved_btn"):
                try:
                    for s in ctd: supabase.table("saved_breakouts").delete().eq("symbol", s).eq("user_id", "nse_user").execute()
                    st.success("Removed!"); st.rerun()
                except Exception as e: st.error(f"Failed: {e}")
            st.markdown("#### Live Tracked Data")
            gb3 = GridOptionsBuilder.from_dataframe(mdf); gb3.configure_default_column(resizable=True,filterable=True,sortable=True,minWidth=70,flex=0)
            gb3.configure_side_bar(); gb3.configure_grid_options(enableBrowserTooltips=True)
            if 'Symbol' in mdf.columns: gb3.configure_column('Symbol', minWidth=90, maxWidth=130, pinned='left')
            gb3.configure_column('Status', minWidth=120, maxWidth=150, cellStyle=JsCode("""function(p){if(!p.value)return null;if(p.value.includes('Active'))return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(p.value.includes('Dropped'))return{'backgroundColor':'#dc3545','color':'white','fontWeight':'bold'};return{'backgroundColor':'#fff3cd','color':'#664d03'}}"""))
            if '_vol_ratio' in mdf.columns: gb3.configure_column('_vol_ratio', minWidth=60, maxWidth=85, headerName='Vol Ratio', cellStyle=vol_jscode if '_vol_ratio' in fdf.columns else None)
            if '_rel_tightness' in mdf.columns: gb3.configure_column('_rel_tightness', minWidth=70, maxWidth=90, headerName='Rel Tight', cellStyle=JsCode("""function(p){return{'backgroundColor':'#fff3cd','color':'#664d03','fontWeight':'bold'}}"""), comparator=abs_comparator)
            if '_rel_wk_dist' in mdf.columns: gb3.configure_column('_rel_wk_dist', minWidth=70, maxWidth=90, headerName='Rel Wk Dist', cellStyle=rel_wk_dist_jscode if '_rel_wk_dist' in fdf.columns else None, comparator=abs_comparator)
            if '_chg_percentclose' in mdf.columns: gb3.configure_column('_chg_percentclose', minWidth=80, maxWidth=110)
            if 'Adr' in mdf.columns: gb3.configure_column('Adr', minWidth=55, maxWidth=75)
            if 'Ti65' in mdf.columns: gb3.configure_column('Ti65', minWidth=55, maxWidth=75)
            if 'Avg_RS' in mdf.columns: gb3.configure_column('Avg_RS', minWidth=55, maxWidth=75)
            hs3 = {'_chg_percentclose':'Chg %','_avgvol_mln':'AvgVolcr','Sector_Percentile':'SectPctile','_nr4_previous':'NR4Prev','_10madist':'10MADist','_20madist':'20MADist','W_PctOf10wkHigh':'Wk % of 10wHi','W_TightCloses_10w':'Wk TightCl/5','_rel_wk_dist':'RelWkDist','_vol_ratio':'VolRatio'}
            for rc, sn in hs3.items():
                if rc in mdf.columns: gb3.configure_column(rc, headerName=sn)
            AgGrid(clean_df_for_json(mdf), gridOptions=gb3.build(), update_mode=GridUpdateMode.MODEL_CHANGED, fit_columns_on_grid_load=False, height=600, theme='streamlit', key='nse_saved_breakouts_grid', allow_unsafe_jscode=True)
        elif not saved_breakouts:
            st.info("No saved breakouts yet. Select rows in the main scanner or add a ticker manually above.")
        else:
            st.warning("Click 'Generate GTT Trading Plan' first to pull live data for your saved tickers.")

if __name__ == "__main__":
    main()