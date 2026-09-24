import streamlit as st
import pandas as pd
import numpy as np
import requests
from io import StringIO
from pandas.api.types import (
    is_categorical_dtype, is_datetime64_any_dtype, is_numeric_dtype, is_object_dtype,
)
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

def filter_dataframe(df, scan_mode, max_rel_tight, min_adr, min_avgvol):
    modify = st.checkbox("Add Advanced Filters")
    check_today_bo = False
    if scan_mode == "Post Breakout":
        check_today_bo = st.checkbox("Check Today Breakouts (Chg% > 0 & Vol_Score >= 1)", key="check_today_bo")
    check_tight_flags = False
    if scan_mode == "Anticipation":
        check_tight_flags = st.checkbox(f"Check high Tight flags (ADR >= {min_adr}, AvgVol >= {min_avgvol}, Rel Tight <= {max_rel_tight})", key="check_tight_flags")
    if not modify:
        if check_today_bo:
            if '_chg_percentclose' in df.columns: df = df[df['_chg_percentclose'].fillna(0) > 0]
            if 'Vol_Score' in df.columns: df = df[df['Vol_Score'].fillna(0) >= 1]
        if check_tight_flags:
            if 'Adr' in df.columns: df = df[df['Adr'].fillna(0) >= min_adr]
            if '_avgvol_mln' in df.columns: df = df[df['_avgvol_mln'].fillna(0) >= min_avgvol]
            if '_rel_tightness' in df.columns:
                df['_rel_tightness'] = pd.to_numeric(df['_rel_tightness'], errors='coerce')
                df = df[df['_rel_tightness'].fillna(999) <= max_rel_tight]
        return df
    df = df.copy()
    with st.container():
        tc = '_nr4' if scan_mode == "Anticipation" else '_nr4_previous'
        df2 = ['_chg_percentclose','Adr','Sector_Percentile','_avgvol_mln'] if scan_mode == "Post Breakout" else [tc,'Sector_Percentile','Adr','Tier','_avgvol_mln']
        to_filter = st.multiselect("Filter dataframe on", df.columns, default=df2)
        for column in to_filter:
            cs = df[column]; hn = cs.isna().any()
            if _is_categorical(cs) or cs.dropna().nunique() < 10:
                un = list(cs.dropna().unique()); NL = "(blank / NaN)"
                so = un + ([NL] if hn else [])
                if column == 'Tier':
                    ds = [t for t in ['A','B'] if t in so] or list(so)
                else: ds = list(so)
                ui = st.multiselect(f"Values for {column}", so, default=ds)
                ns = NL in ui; rv = [v for v in ui if v != NL]
                df = df[(cs.isna() | cs.isin(rv)) if ns else (~cs.isna() & cs.isin(rv))]
            elif is_numeric_dtype(cs):
                cl = cs.dropna()
                if cl.empty: st.info(f"Column **{column}** has no numeric values"); continue
                _min = float(cl.min()); _max = float(cl.max())
                if _max <= _min: _max = _min + 0.1
                step = (_max - _min) / 100 if (_max - _min) > 0 else 0.1
                cmb = {'_nr4':5.0,'_nr4_previous':5.0,'_chg_percentclose':20.0,'Adr':15.0,'Sector_Percentile':100.0,'Avg_RS':100.0}
                _max = max(_max, cmb.get(column, _max))
                cr = {'_nr4':(0.0,3.0),'_nr4_previous':(0.0,3.0),'Adr':(2.0,_max),'Sector_Percentile':(60.0,100.0),'_chg_percentclose':(2.0,_max),'Ti65':(1.05,_max),'Avg_RS':(92.0,_max),'_avgvol_mln':(10.0,_max)}
                dr = cr.get(column, (_min, _max))
                dm = max(float(dr[0]), _min); dx = min(float(dr[1]), _max)
                if dm > dx: dm = _min; dx = _max
                ui = st.slider(f"Values for {column}", _min, _max, (dm, dx), step=step)
                kn = st.checkbox(f"Keep rows where **{column}** is blank", value=True, key=f"kn_{column}") if hn else False
                ir = cs.between(*ui)
                df = df[(ir | cs.isna()) if kn else ir]
            else:
                ti = st.text_input(f"Substring or regex in {column}")
                if ti: df = df[cs.astype(str).str.contains(ti, case=False, na=False) | cs.isna()]
    if check_today_bo:
        if '_chg_percentclose' in df.columns: df = df[df['_chg_percentclose'].fillna(0) > 0]
        if 'Vol_Score' in df.columns: df = df[df['Vol_Score'].fillna(0) >= 1]
    if check_tight_flags:
        if 'Adr' in df.columns: df = df[df['Adr'].fillna(0) >= 6.0]
        if '_avgvol_mln' in df.columns: df = df[df['_avgvol_mln'].fillna(0) >= 10.0]
        if '_rel_tightness' in df.columns:
            df['_rel_tightness'] = pd.to_numeric(df['_rel_tightness'], errors='coerce')
            df = df[df['_rel_tightness'].fillna(999) <= 0.6]
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
        st.sidebar.markdown(f"""<div style="font-size:13px;color:#888;padding:2px 0;">Next refresh in <span id="cd-m">{m}</span>m <span id="cd-s">{s:02d}</span>s</div><script>let t={rem};const a=document.getElementById('cd-m'),b=document.getElementById('cd-s');const x=setInterval(function(){{t--;if(t<=0){{clearInterval(x);a.textContent='0';b.textContent='00';window.top.location.reload();}}else{{a.textContent=Math.floor(t/60);b.textContent=(t%60<10?'0':'')+(t%60);}}}},1000);</script>""", unsafe_allow_html=True)
    else:
        if 'last_refresh_ts' in st.session_state: del st.session_state['last_refresh_ts']

    sector_df = load_sector_mapping(SECTOR_FILE)
    scan_mode = st.radio("Select Scanner Mode", ("Anticipation", "Post Breakout"), horizontal=True)
    if scan_mode == "Post Breakout": st.markdown("Automated lifecycle manager for Boom Boom, 1-2-3, and Coiled Spring setups.")
    else: st.markdown("Anticipation scanner for coiled setups as they are breaking out. BEWARE - MAKE SURE VOLUME IS COMING IN")

    st.sidebar.header("Scoring System Config")
    saved_scoring = load_scoring_prefs("NSE")

    ABS_SORT_COLS = {'W_Dist10wMA', '_rel_tightness', '_rel_wk_dist', '_20madist', '_10madist', '_10wmadist'}
    sortable_columns = {
        'Total_Score': 'Total Score', 'W_Dist10wMA': 'Wk Dist 10wMA', '_rel_wk_dist': 'Rel Wk Dist (ADR)',
        '_rel_tightness': 'Rel Tightness', 'Adr': 'ADR', 'Ti65': 'Ti65', 'Avg_RS': 'Avg RS',
        '_avgvol_mln': 'Avg Volume', 'dvol': 'Daily Volume', 'Sector_Percentile': 'Sector %ile',
        '_chg_percentclose': 'Chg %', 'W_TightCloses_10w': 'Wk Tight Closes', '_nr4': 'NR4',
        '_nr4_previous': 'NR4 Previous', '_20madist': '20MA Dist', '_10madist': '10MA Dist',
        'W_PctOf10wkHigh': 'Wk % of 10wHi',
    }
    with st.sidebar.expander("Custom Multi-Level Sort", expanded=False):
        use_custom_sort = st.checkbox("Enable custom sort order", value=False, key="use_custom_sort")
        tier_first = st.checkbox("Always sort Tier A-B-Ignore first", value=True, key="tier_first_sort")
        sort_levels = []
        if use_custom_sort:
            st.caption("For tightness/distance columns, sorting is done by |value|.")
            for i in range(1, 4):
                col = st.selectbox(f"Sort Level {i}", options=['(skip)']+list(sortable_columns.keys()), index=0,
                                   format_func=lambda x: sortable_columns.get(x, '(skip)'), key=f"sl_{i}")
                if col == '(skip)': continue
                is_abs = col in ABS_SORT_COLS
                hig = {'Total_Score','Avg_RS','Adr','Sector_Percentile','_chg_percentclose','W_TightCloses_10w','W_PctOf10wkHigh','dvol','_avgvol_mln','Ti65'}
                di = 1 if col in hig else 0
                d = st.radio(f"Direction{' (by |val|)' if is_abs else ''}", options=['Low to High','High to Low'],
                            index=di, key=f"sd_{i}", horizontal=True)
                sort_levels.append((col, d == 'Low to High'))

    # ── 1. Weekly Setup (Absolute Distance) ──
    st.sidebar.subheader("1. Weekly Setup (10w MA)")
    wk_neg_cutoff = st.sidebar.number_input("Avoid if W_Dist10wMA below this %", value=float(saved_scoring.get('wk_neg_cutoff', -3.0)), step=0.5, key="sc_wk_neg")
    st.sidebar.caption("The golden average. Absolute distance from 10w EMA.")
    wk_defaults = saved_scoring.get('wk_thresholds', [2.0, 4.0, 6.0, 10.0])
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

    abs_comparator = JsCode("""
        function(a, b, na, nb, inv) {
            const x = (a === null || a === undefined || isNaN(a)) ? Infinity : Math.abs(a);
            const y = (b === null || b === undefined || isNaN(b)) ? Infinity : Math.abs(b);
            return x < y ? -1 : x > y ? 1 : 0;
        }
    """)

    # ── 1b. Relative Weekly Distance (NEW — normalized by ADR) ──
    st.sidebar.subheader("1b. Relative Weekly Distance (by ADR)")
    st.sidebar.caption("Volatility-adjusted: how many ADRs away from the 10w EMA. Captures high-tight flags!")
    rwd_defaults = saved_scoring.get('rwd_thresholds', [1.0, 2.0, 3.0, 5.0])
    rwd_raw = [
        st.sidebar.number_input("Rel Wk Dist < this -> 4 pts", value=float(rwd_defaults[0]), step=0.5, key="sc_rwd1"),
        st.sidebar.number_input("Rel Wk Dist < this -> 3 pts", value=float(rwd_defaults[1]), step=0.5, key="sc_rwd2"),
        st.sidebar.number_input("Rel Wk Dist < this -> 2 pts", value=float(rwd_defaults[2]), step=0.5, key="sc_rwd3"),
        st.sidebar.number_input("Rel Wk Dist < this -> 1 pt", value=float(rwd_defaults[3]), step=0.5, key="sc_rwd4"),
    ]
    rwd1, rwd2, rwd3, rwd4 = sorted(rwd_raw)

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

    w_raw = [
        st.sidebar.number_input("Wk Dist < this -> 4 pts", value=float(wk_defaults[0]), step=0.5, key="sc_w1"),
        st.sidebar.number_input("Wk Dist < this -> 3 pts", value=float(wk_defaults[1]), step=0.5, key="sc_w2"),
        st.sidebar.number_input("Wk Dist < this -> 2 pts", value=float(wk_defaults[2]), step=0.5, key="sc_w3"),
        st.sidebar.number_input("Wk Dist < this -> 1 pt", value=float(wk_defaults[3]), step=0.5, key="sc_w4"),
    ]
    w1, w2, w3, w4 = sorted(w_raw)
    wclose_pts = st.sidebar.number_input("Bonus pts if W_TightCloses >= 1", value=int(saved_scoring.get('wclose_pts', 2)), min_value=0, max_value=5, step=1, key="sc_wclose")

    # ── 2. Daily Tightness ──
    st.sidebar.subheader("2. Daily Tightness (Relative to ADR)")
    td = saved_scoring.get('tightness_thresholds', [0.4, 0.6, 0.9, 1.2])
    t_raw = [st.sidebar.number_input(f"Rel Tightness < this -> {x} pts", value=float(td[i]), step=0.1, key=f"sc_t{i+1}") for i, x in enumerate([4,3,2,1])]
    t1, t2, t3, t4 = sorted(t_raw)

    # ── 3. BO Volume ──
    st.sidebar.subheader("3. BO Volume (dvol/avg)")
    vd = saved_scoring.get('vol_thresholds', [3.0, 2.0, 1.5])
    v_raw = [st.sidebar.number_input(f"dvol/avg > this -> {x} pts", value=float(vd[i]), step=0.5, key=f"sc_v{i+1}") for i, x in enumerate([3,2,1])]
    v3, v2, v1 = sorted(v_raw)

    # ── 4. Volume Arriving (NEW — addresses "stuck in trade" problem) ──
    st.sidebar.subheader("4. Volume Arriving (Anti-Stuck)")
    st.sidebar.caption("Rewards stocks up 2-5% with volume STARTING to come in (0.8x-1.3x avg). Penalizes dead volume.")
    va_defaults = saved_scoring.get('vol_arriving', {})
    va_min_chg = st.sidebar.number_input("Min Chg% for Vol Arriving", value=float(va_defaults.get('min_chg', 2.0)), step=0.5, key="sc_va_chg")
    va_max_chg = st.sidebar.number_input("Max Chg% for Vol Arriving", value=float(va_defaults.get('max_chg', 5.0)), step=0.5, key="sc_va_chg2")
    va_min_vol = st.sidebar.number_input("Min Vol Ratio (x avg)", value=float(va_defaults.get('min_vol', 0.8)), step=0.1, key="sc_va_vol1")
    va_max_vol = st.sidebar.number_input("Max Vol Ratio (x avg)", value=float(va_defaults.get('max_vol', 1.3)), step=0.1, key="sc_va_vol2")
    va_bonus = st.sidebar.number_input("Vol Arriving Bonus (pts)", value=int(va_defaults.get('bonus', 2)), min_value=0, max_value=5, step=1, key="sc_va_bonus")
    stuck_max_vol = st.sidebar.number_input("Stuck Risk: Vol below this (x avg)", value=float(va_defaults.get('stuck_max_vol', 0.5)), step=0.1, key="sc_va_stuck")
    stuck_penalty = st.sidebar.number_input("Stuck Risk Penalty (pts)", value=int(va_defaults.get('stuck_penalty', 1)), min_value=0, max_value=3, step=1, key="sc_va_sp")

    # ── 5. TightCloses Bonus ──
    st.sidebar.subheader("5. TightCloses Bonus")
    tclose_pts = st.sidebar.number_input("Points if W_TightCloses >= 1", value=int(saved_scoring.get('tclose_bonus_pts', 2)), min_value=0, max_value=5, step=1, key="sc_tclose")

    # ── 6. 20MADist ──
    st.sidebar.subheader("6. 20MADist")
    m20d = saved_scoring.get('ma20_tiers', [2.0, 4.0, 6.0])
    ma20_neg = st.sidebar.number_input("Avoid if 20MADist below this %", value=float(saved_scoring.get('ma20_neg_cutoff', -6.0)), step=0.5, key="sc_ma20_neg")
    ma20_raw = [st.sidebar.number_input(f"abs(20MADist) < this -> {x} pts", value=float(m20d[i]), step=0.5, key=f"sc_ma20_{i+1}") for i, x in enumerate([3,2,1])]
    ma20_t1, ma20_t2, ma20_t3 = sorted(ma20_raw)

    # ── 7. 10MADist ──
    st.sidebar.subheader("7. 10MADist")
    m10d = saved_scoring.get('ma10_tiers', [4.0, 6.0])
    ma10_neg = st.sidebar.number_input("Avoid if 10MADist below this %", value=float(saved_scoring.get('ma10_neg_cutoff', -6.0)), step=0.5, key="sc_ma10_neg")
    ma10_raw = [st.sidebar.number_input(f"abs(10MADist) < this -> {x} pts", value=float(m10d[i]), step=0.5, key=f"sc_ma10_{i+1}") for i, x in enumerate([2,1])]
    ma10_t1, ma10_t2 = sorted(ma10_raw)

    # ── Quick Filter Config ──
    st.sidebar.subheader("Quick Filter Config")
    filter_min_adr = st.sidebar.number_input("Min ADR for Tight Flags", value=float(saved_scoring.get('filter_min_adr', 4.0)), step=0.5, key="sc_f_adr")
    filter_min_avgvol = st.sidebar.number_input("Min AvgVol (Mln) for Tight Flags", value=float(saved_scoring.get('filter_min_avgvol', 10.0)), step=1.0, key="sc_f_avgvol")

    # ── Tier Thresholds ──
    st.sidebar.subheader("Tier Thresholds")
    tier_a = st.sidebar.number_input("Tier A min score", value=int(saved_scoring.get('tier_a_threshold', 10)), min_value=1, max_value=20, step=1, key="sc_tier_a")
    tier_b = st.sidebar.number_input("Tier B min score", value=int(saved_scoring.get('tier_b_threshold', 7)), min_value=1, max_value=20, step=1, key="sc_tier_b")

    if st.sidebar.button("Save scoring config", key="save_scoring_btn"):
        prefs_to_save = {
            'tightness_thresholds': t_raw, 'wk_thresholds': w_raw, 'rwd_thresholds': rwd_raw,
            'wclose_pts': int(wclose_pts), 'vol_thresholds': v_raw,
            'vol_arriving': {'min_chg': va_min_chg, 'max_chg': va_max_chg, 'min_vol': va_min_vol, 'max_vol': va_max_vol,
                             'bonus': int(va_bonus), 'stuck_max_vol': stuck_max_vol, 'stuck_penalty': int(stuck_penalty)},
            'tclose_bonus_pts': int(tclose_pts), 'ma20_tiers': ma20_raw, 'ma20_neg_cutoff': ma20_neg,
            'ma10_tiers': ma10_raw, 'ma10_neg_cutoff': ma10_neg,
            'tier_a_threshold': int(tier_a), 'tier_b_threshold': int(tier_b),
            'filter_min_adr': float(filter_min_adr), 'filter_min_avgvol': float(filter_min_avgvol),
            'wk_neg_cutoff': wk_neg_cutoff,
        }
        save_scoring_prefs(prefs_to_save, "NSE")
        st.sidebar.success("Saved!")

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

    with tab1:
        if 'gtt_base_df' in st.session_state and st.session_state.gtt_base_df is not None:
            adf = st.session_state.gtt_base_df.copy()
            thresholds_ok = (len(set([t1,t2,t3,t4])) >= 4 and len(set([v1,v2,v3])) >= 3 and
                len(set([w1,w2,w3,w4])) >= 4 and len(set([rwd1,rwd2,rwd3,rwd4])) >= 4 and
                len(set([ma20_t1,ma20_t2,ma20_t3])) >= 3 and len(set([ma10_t1,ma10_t2])) >= 2)

            if not thresholds_ok:
                st.sidebar.error("Scoring Error: Threshold values within a criteria must be unique.")
                for c in ['Tier','Total_Score','Wk_Setup_Score','Wk_TClose_Score','Wk_RelDist_Score','Tight_Score','Vol_Score','Vol_Arriving_Score','Stuck_Risk_Score','TClose_Score','MA20_Score','MA10_Score']:
                    adf[c] = 0 if c not in ['Tier'] else 'Error'
                adf['Tier'] = 'Error'
            else:
                # ── 1a. Absolute Weekly Distance Score ──
                if 'W_Dist10wMA' in adf.columns:
                    wr = adf['W_Dist10wMA'].fillna(999); wa = wr.abs()
                    wbs = pd.cut(wa, bins=[-float('inf'),w1,w2,w3,w4,float('inf')], labels=[4,3,2,1,0]).astype(int)
                    wii = (adf['W_Dist10wMA'].isna() | (adf['W_Dist10wMA'] < wk_neg_cutoff))
                    adf['Wk_Setup_Score'] = np.where(wii, 0, wbs)
                else: adf['Wk_Setup_Score'] = 0

                # ── 1b. Relative Weekly Distance Score (NEW) ──
                if 'W_Dist10wMA' in adf.columns and 'Adr' in adf.columns:
                    adf['_rel_wk_dist'] = (adf['W_Dist10wMA'].fillna(999).abs() / adf['Adr'].replace(0, np.nan)).round(2)
                    rwd_filled = adf['_rel_wk_dist'].fillna(999)
                    adf['Wk_RelDist_Score'] = pd.cut(rwd_filled, bins=[-float('inf'),rwd1,rwd2,rwd3,rwd4,float('inf')], labels=[4,3,2,1,0]).astype(int)
                else:
                    adf['_rel_wk_dist'] = 999; adf['Wk_RelDist_Score'] = 0

                adf['Wk_TClose_Score'] = np.where(adf.get('W_TightCloses_10w', pd.Series(0, index=adf.index)).fillna(0) >= 1, wclose_pts, 0)

                # ── 2. Daily Tightness ──
                tc = '_nr4' if scan_mode == "Anticipation" else '_nr4_previous'
                sa = adf['Adr'].replace(0, np.nan)
                adf['_rel_tightness'] = (adf[tc] / sa).round(2)
                rtf = adf['_rel_tightness'].fillna(999)
                adf['Tight_Score'] = pd.cut(rtf, bins=[-float('inf'),t1,t2,t3,t4,float('inf')], labels=[4,3,2,1,0]).astype(int)

                # ── 3. BO Volume ──
                if scan_mode == "Anticipation": adf['Vol_Score'] = 0
                else:
                    rr = np.where(adf['_avgvol_mln'] > 0, adf['dvol'] / adf['_avgvol_mln'], 0)
                    adf['Vol_Score'] = pd.cut(rr, bins=[-float('inf'),v3,v2,v1,float('inf')], labels=[0,1,2,3]).astype(int)

                # ── 4. Volume Arriving Score (NEW — anti-stuck) ──
                if '_chg_percentclose' in adf.columns and 'dvol' in adf.columns and '_avgvol_mln' in adf.columns:
                    vr = np.where(adf['_avgvol_mln'] > 0, adf['dvol'] / adf['_avgvol_mln'], 0)
                    vol_arriving = ((adf['_chg_percentclose'].fillna(0) >= va_min_chg) & (adf['_chg_percentclose'].fillna(0) <= va_max_chg) & (vr >= va_min_vol) & (vr <= va_max_vol))
                    adf['Vol_Arriving_Score'] = np.where(vol_arriving, va_bonus, 0)
                    stuck_risk = ((adf['_chg_percentclose'].fillna(0) >= va_min_chg) & (adf['_chg_percentclose'].fillna(0) <= va_max_chg) & (vr < stuck_max_vol))
                    adf['Stuck_Risk_Score'] = np.where(stuck_risk, -stuck_penalty, 0)
                else:
                    adf['Vol_Arriving_Score'] = 0; adf['Stuck_Risk_Score'] = 0

                adf['TClose_Score'] = np.where(adf['W_TightCloses_10w'].fillna(0) >= 1, tclose_pts, 0)

                # ── 6. MA20 ──
                m20f = adf['_20madist'].fillna(999); m20a = m20f.abs()
                m20bs = pd.cut(m20a, bins=[-float('inf'),ma20_t1,ma20_t2,ma20_t3,float('inf')], labels=[3,2,1,0]).astype(int)
                m20ii = adf['_20madist'].isna() | (adf['_20madist'] < ma20_neg)
                adf['MA20_Score'] = np.where(m20ii, 0, m20bs)

                # ── 7. MA10 ──
                m10f = adf['_10madist'].fillna(999); m10a = m10f.abs()
                m10bs = pd.cut(m10a, bins=[-float('inf'),ma10_t1,ma10_t2,float('inf')], labels=[2,1,0]).astype(int)
                m10ii = adf['_10madist'].isna() | (adf['_10madist'] < ma10_neg)
                adf['MA10_Score'] = np.where(m10ii, 0, m10bs)

                # ── Total Score (includes all new components) ──
                adf['Total_Score'] = (
                    adf['Wk_Setup_Score'] + adf['Wk_TClose_Score'] + adf['Wk_RelDist_Score'] +
                    adf['Tight_Score'] + adf['Vol_Score'] + adf['Vol_Arriving_Score'] + adf['Stuck_Risk_Score'] +
                    adf['TClose_Score'] + adf['MA20_Score'] + adf['MA10_Score']
                )
                adf['Tier'] = np.select([adf['Total_Score'] >= tier_a, adf['Total_Score'] >= tier_b], ['A','B'], default='Ignore')

            # ── Change tracking ──
            tom = {'A':0,'B':1,'Ignore':2,'Error':3}
            if 'prev_scan_data' in st.session_state and st.session_state.prev_scan_data is not None:
                prev = st.session_state.prev_scan_data; adf['Change'] = ''
                for idx, row in adf.iterrows():
                    sym = row['Symbol']; ct, cs = row['Tier'], row['Total_Score']
                    if sym not in prev: adf.at[idx, 'Change'] = 'New'
                    else:
                        pt, ps = prev[sym]['tier'], prev[sym]['score']
                        cr, pr = tom.get(ct, 9), tom.get(pt, 9)
                        if cr < pr: adf.at[idx, 'Change'] = 'Up'
                        elif cr > pr: adf.at[idx, 'Change'] = 'Down'
                        elif cs > ps: adf.at[idx, 'Change'] = 'Score Up'
                        elif cs < ps: adf.at[idx, 'Change'] = 'Score Down'
                st.session_state.dropped_symbols = set(prev.keys()) - set(adf['Symbol'])
            else:
                adf['Change'] = ''; st.session_state.dropped_symbols = set()
            st.session_state.prev_scan_data = {r['Symbol']: {'tier': r['Tier'], 'score': int(r['Total_Score'])} for _, r in adf.iterrows()}

            cols = list(adf.columns); cols.insert(0, cols.pop(cols.index('Tier'))); adf = adf[cols]
            st.session_state.gtt_scored_df = adf.copy()

            columns_to_show = [
                'Tier','Change','Total_Score',
                'Wk_Setup_Score','Wk_RelDist_Score','Wk_TClose_Score','Tight_Score','Vol_Score','Vol_Arriving_Score','Stuck_Risk_Score','TClose_Score','MA20_Score','MA10_Score',
                'W_Dist10wMA','_rel_wk_dist','W_TightCloses_10w','W_PctOf10wkHigh','W_InsideBars','W_CloseChg_Pct',
                '_nr4_previous','_rel_tightness','_chg_percentclose',
                'dvol','_avgvol_mln','_20madist','_10madist',
                'Symbol','Sector','Industry','Avg_RS','RS_6M','RS_3M','RS_1M',
                'Adr','Ti65','_nr4',
                '_bo_engulfing_cndl','_days_since_bo','_bo_dollar_vol_mln','_circuit','_avg_vol_float_ratio',
                '_period_perf','_10wmadist','_insideday',
            ]
            if 'Sector_Rank' in adf.columns: columns_to_show.insert(columns_to_show.index('Symbol')+1, 'Sector_Rank')
            if 'Sector_Total' in adf.columns: columns_to_show.insert(columns_to_show.index('Sector_Rank')+1, 'Sector_Total')
            if 'Sector_Percentile' in adf.columns: columns_to_show.insert(columns_to_show.index('Sector_Total')+1, 'Sector_Percentile')

            vc = [c for c in columns_to_show if c in adf.columns]
            tso = {'A':0,'B':1,'Ignore':2,'Error':3}
            adf['_tier_sort_key'] = adf['Tier'].map(tso).fillna(9)
            ddf = adf[vc].copy(); ddf['_tier_sort_key'] = adf['_tier_sort_key']
            ABSL = {'W_Dist10wMA','_rel_tightness','_rel_wk_dist','_20madist','_10madist','_10wmadist'}

            if use_custom_sort and len(sort_levels) > 0:
                sbc = []; sa = []; tcd = []
                if tier_first: sbc.append('_tier_sort_key'); sa.append(True)
                for col, asc in sort_levels:
                    if col not in ddf.columns: continue
                    ddf[col] = pd.to_numeric(ddf[col], errors='coerce')
                    if col in ABSL:
                        tc2 = f'_abs_{col}'; ddf[tc2] = ddf[col].abs(); sbc.append(tc2); tcd.append(tc2)
                    else: sbc.append(col)
                    sa.append(asc)
                if sbc:
                    ddf = ddf.sort_values(by=sbc, ascending=sa, na_position='last')
                    ddf = ddf.drop(columns=tcd, errors='ignore')
                else: ddf = ddf.sort_values(by=['_tier_sort_key'], ascending=[True])
            else:
                swc = 'W_Dist10wMA'
                if swc in ddf.columns:
                    ddf[swc] = pd.to_numeric(ddf[swc], errors='coerce')
                    ddf['_ask'] = ddf[swc].abs()
                    ddf = ddf.sort_values(by=['_tier_sort_key','_ask','_rel_tightness'], ascending=[True,True,True], na_position='last')
                    ddf = ddf.drop(columns=['_ask'], errors='ignore')
                else: ddf = ddf.sort_values(by=['_tier_sort_key'], ascending=[True])
            ddf = ddf.drop(columns=['_tier_sort_key'], errors='ignore')
            st.session_state.gtt_display_df = ddf
            st.success(f"Generated {len(ddf)} actionable GTT setups.")
            fdf = filter_dataframe(ddf, scan_mode, t4, filter_min_adr, filter_min_avgvol)

            mtdh = ['RS_6M','RS_3M','RS_1M','Industry','_bo_dollar_vol_mln','_avg_vol_float_ratio','_period_perf','_10wmadist','_insideday','_bo_engulfing_cndl','_days_since_bo','_circuit','W_CloseChg_Pct']
            amc = list(fdf.columns)
            with st.expander("Choose visible columns (saved as your default)"):
                smc = st.multiselect("Columns to show", amc, default=get_persisted_columns('us_main_table', amc, mtdh), key="main_col_sel")
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

            for col in ['Avg_RS','RS_6M','RS_3M','RS_1M']:
                if col not in fdf.columns: continue
                vd = fdf[fdf[col] > 0][col]
                cmn = vd.min() if not vd.empty else 0; cmx = vd.max() if not vd.empty else 100
                dj = JsCode(f"""function(p){{const v=p.value;if(v<=0)return null;const m={cmn},x={cmx};if(x===m)return{{'backgroundColor':'#fff','color':'black'}};const r=(v-m)/(x-m);let a,b,c;if(r<0.5){{const p=r/0.5;a=255;b=Math.round(100+155*p);c=Math.round(100+155*p)}}else{{const p=(r-0.5)/0.5;a=Math.round(255-155*p);b=255;c=Math.round(255-155*p)}}return{{'backgroundColor':'rgb('+a+','+b+','+c+')','color':'black','fontWeight':r>=0.9?'bold':'normal'}}}}""")
                gb.configure_column(col, minWidth=60 if col == 'Avg_RS' else 50, maxWidth=90 if col == 'Avg_RS' else 80, cellStyle=dj)

            th = JsCode("""function(p){return{'backgroundColor':'#fff3cd','color':'#664d03','fontWeight':'bold'};}""")
            if '_rel_tightness' in fdf.columns:
                gb.configure_column('_rel_tightness', minWidth=70, maxWidth=90, headerName='Rel Tight', cellStyle=th, comparator=abs_comparator)

            if '_rel_wk_dist' in fdf.columns:
                gb.configure_column('_rel_wk_dist', minWidth=70, maxWidth=90, headerName='Rel Wk Dist', cellStyle=rel_wk_dist_jscode, comparator=abs_comparator)

            if '_chg_percentclose' in fdf.columns:
                vc2 = fdf[fdf['_chg_percentclose'] > 0]['_chg_percentclose']
                cmn = float(vc2.min()) if not vc2.empty else 0.0; cmx = float(vc2.max()) if not vc2.empty else 10.0
                cj = JsCode(f"""function(p){{const v=p.value;if(!v||v<=0)return null;const m={cmn},x={cmx};if(x===m)return{{'backgroundColor':'#ffe6ff','color':'black'}};const r=Math.min((v-m)/(x-m),1.0);const a=Math.round(255-115*r),b=Math.round(220-220*r),c=Math.round(255-115*r);return{{'backgroundColor':'rgb('+a+','+b+','+c+')','color':r>0.5?'white':'black','fontWeight':r>=0.8?'bold':'normal'}}}}""")
                gb.configure_column('_chg_percentclose', minWidth=80, maxWidth=110, cellStyle=cj, filter='agNumberColumnFilter', filterParams={'filterOptions':['greaterThan','lessThan','equals','inRange'],'defaultOption':'greaterThan','defaultValues':[0]})

            for col in ['Adr','Ti65','_nr4']:
                if col in fdf.columns: gb.configure_column(col, minWidth=55, maxWidth=75)

            if 'dvol' in fdf.columns and '_avgvol_mln' in fdf.columns:
                vr = fdf[(fdf['dvol']>0)&(fdf['_avgvol_mln']>0)].copy()
                if not vr.empty:
                    vr['rr'] = vr['dvol']/vr['_avgvol_mln']; aa = vr[vr['rr']>1.0]['rr']
                    rf = max(float(aa.min()),1.0) if not aa.empty else 1.0; rc = float(aa.max()) if not aa.empty else 3.0
                else: rf, rc = 1.0, 3.0
                rj = JsCode(f"""function(p){{const d=p.data.dvol,a=p.data._avgvol_mln;if(!d||!a||a<=0||d<=0)return null;const r=d/a;if(r<=1.0)return null;const f={rf},c={rc};if(c<=f)return{{'backgroundColor':'#d4edda','color':'black'}};const n=Math.min((r-f)/(c-f),1.0);let a2,b2,c2;if(n<0.5){{const p2=n/0.5;a2=Math.round(248-208*p2);b2=Math.round(255-90*p2);c2=Math.round(248-181*p2)}}else{{const p2=(n-0.5)/0.5;a2=Math.round(40-17*p2);b2=Math.round(165-78*p2);c2=Math.round(67-31*p2)}}return{{'backgroundColor':'rgb('+a2+','+b2+','+c2+')','color':'black','fontWeight':n>=0.8?'bold':'normal'}}}}""")
                gb.configure_column('dvol', minWidth=60, maxWidth=85, cellStyle=rj)
                gb.configure_column('_avgvol_mln', minWidth=60, maxWidth=85, cellStyle=rj)

            for col in ['_bo_dollar_vol_mln','_avg_vol_float_ratio']:
                if col in fdf.columns: gb.configure_column(col, minWidth=70, maxWidth=110)

            md = JsCode("""function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;const a=Math.abs(v);if(v<-6)return{'backgroundColor':'#f8d7da','color':'#721c24','fontWeight':'bold'};if(a<2)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(a<4)return{'backgroundColor':'#8ee68e','color':'black'};if(a<6)return{'backgroundColor':'#d4edda','color':'black'};return null}""")
            if '_20madist' in fdf.columns: gb.configure_column('_20madist', minWidth=70, maxWidth=90, cellStyle=md, comparator=abs_comparator)
            if '_10madist' in fdf.columns: gb.configure_column('_10madist', minWidth=70, maxWidth=90, cellStyle=md, comparator=abs_comparator)
            if 'W_Dist10wMA' in fdf.columns: gb.configure_column('W_Dist10wMA', minWidth=80, maxWidth=110, headerName='Wk 10wMA %', cellStyle=wk_dist_jscode, comparator=abs_comparator)

            sc = JsCode("""function(p){const v=p.value;if(v===null||v===undefined)return null;if(v>=3)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(v>=2)return{'backgroundColor':'#8ee68e','color':'black'};if(v>=1)return{'backgroundColor':'#d4edda','color':'black'};if(v<0)return{'backgroundColor':'#f8d7da','color':'#721c24','fontWeight':'bold'};return null}""")
            for sc_col in ['Wk_Setup_Score','Wk_RelDist_Score','Wk_TClose_Score','Tight_Score','Vol_Score','Vol_Arriving_Score','TClose_Score','MA20_Score','MA10_Score']:
                if sc_col in fdf.columns: gb.configure_column(sc_col, minWidth=45, maxWidth=60, cellStyle=sc)
            if 'Stuck_Risk_Score' in fdf.columns:
                gb.configure_column('Stuck_Risk_Score', minWidth=45, maxWidth=60, headerName='Stuck?',
                    cellStyle=JsCode("""function(p){const v=p.value;if(v===null||v===undefined||v>=0)return null;return{'backgroundColor':'#dc3545','color':'white','fontWeight':'bold'}}"""))

            wp = JsCode("""function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;if(v>=1.0)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(v>=0.95)return{'backgroundColor':'#8ee68e','color':'black'};if(v>=0.85)return{'backgroundColor':'#d4edda','color':'black'};return null}""")
            if 'W_PctOf10wkHigh' in fdf.columns: gb.configure_column('W_PctOf10wkHigh', headerName='Wk % of 10wHi', minWidth=95, maxWidth=120, cellStyle=wp)
            if 'W_CloseChg_Pct' in fdf.columns: gb.configure_column('W_CloseChg_Pct', headerName='Wk CloseChg%', minWidth=90, maxWidth=115)
            if 'W_TightCloses_10w' in fdf.columns: gb.configure_column('W_TightCloses_10w', headerName='Wk Tight 10w/5', minWidth=85, maxWidth=105)
            if 'W_InsideBars' in fdf.columns: gb.configure_column('W_InsideBars', headerName='Wk InsideB/8', minWidth=85, maxWidth=105)

            hs = {'Change':'Chg','_chg_percentclose':'Chg %','_avgvol_mln':'AvgVolcr','_bo_dollar_vol_mln':'BO$Volcr','_avg_vol_float_ratio':'VolFloatR','_bo_engulfing_cndl':'BOEngulf','_days_since_bo':'DaysSinceBO','Sector_Percentile':'SectPctile','_nr4_previous':'NR4Prev','_period_perf':'PeriodPerf','_10wmadist':'10wMADist','_10madist':'10MADist','_20madist':'20MADist','_insideday':'InsideDay','Tight_Score':'Tight','Vol_Score':'Vol','TClose_Score':'TClose','MA20_Score':'MA20','MA10_Score':'MA10','W_Dist10wMA':'Wk 10wMA %','_rel_wk_dist':'Rel Wk Dist','W_PctOf10wkHigh':'Wk % of 10wHi','W_CloseChg_Pct':'Wk CloseChg%','W_TightCloses':'Wk TightCl/5','W_InsideBars':'Wk InsideB/8','Wk_Setup_Score':'WkAbs','Wk_RelDist_Score':'WkRel','Vol_Arriving_Score':'VolArr','Stuck_Risk_Score':'Stuck'}
            for rc, sn in hs.items():
                if rc in fdf.columns: gb.configure_column(rc, headerName=sn)

            tj = JsCode("""function(p){if(!p.value)return null;if(p.value.includes('A'))return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(p.value.includes('B'))return{'backgroundColor':'#ffc107','color':'black','fontWeight':'bold'};if(p.value.includes('Ignore'))return{'backgroundColor':'#dc3545','color':'white','fontWeight':'bold'};return null}""")
            gb.configure_column('Tier', minWidth=70, maxWidth=85, cellStyle=tj, pinned='left')
            gb.configure_column('Total_Score', minWidth=55, maxWidth=70)
            if 'Sector_Rank' in fdf.columns: gb.configure_column('Sector_Rank', hide=True)
            if 'Sector_Total' in fdf.columns: gb.configure_column('Sector_Total', hide=True)
            if 'Sector_Percentile' in fdf.columns: gb.configure_column('Sector_Percentile', minWidth=100, maxWidth=120)
            sr = JsCode("""function(p){const s=p.value;const r=p.data.Sector_Rank;const t=p.data.Sector_Total;if(r&&t&&r>0)return s+' ('+r+'/'+t+')';return s}""")
            gb.configure_column('Symbol', cellRenderer=sr, minWidth=150, maxWidth=180, pinned='left', checkboxSelection=True)
            if 'Sector' in fdf.columns: gb.configure_column('Sector', minWidth=120, maxWidth=150)
            if 'Industry' in fdf.columns: gb.configure_column('Industry', minWidth=120, maxWidth=150)
            for col in hmc: gb.configure_column(col, hide=True)

            go = gb.build()
            safe_df = clean_df_for_json(fdf)
            grid_response = AgGrid(safe_df, gridOptions=go, height=600, width='100%', update_mode=GridUpdateMode.MODEL_CHANGED, data_return_mode=DataReturnMode.FILTERED_AND_SORTED, allow_unsafe_jscode=True)

            # ── Export ──
            st.markdown("---"); st.subheader("Export Scanner Data for Analysis")
            cd1, cd2 = st.columns(2)
            with cd1:
                ef = fdf.copy()
                if 'W_Dist10wMA' in ef.columns and 'Adr' in ef.columns:
                    ef['_rel_wk_dist'] = (ef['W_Dist10wMA'].abs() / ef['Adr'].replace(0, np.nan)).round(2)
                st.download_button("Download Filtered Data (CSV)", ef.to_csv(index=False), f"gtt_filtered_{datetime.now().strftime('%Y%m%d_%H%M')}.csv", "text/csv")
                st.caption(f"{len(ef)} rows, {len(ef.columns)} columns")
            with cd2:
                if 'gtt_scored_df' in st.session_state and st.session_state.gtt_scored_df is not None:
                    efl = st.session_state.gtt_scored_df.copy()
                    if 'W_Dist10wMA' in efl.columns and 'Adr' in efl.columns:
                        efl['_rel_wk_dist'] = (efl['W_Dist10wMA'].abs() / efl['Adr'].replace(0, np.nan)).round(2)
                    st.download_button("Download Full Scored Dataset (CSV)", efl.to_csv(index=False), f"gtt_full_scored_{datetime.now().strftime('%Y%m%d_%H%M')}.csv", "text/csv")
                    st.caption(f"{len(efl)} rows, {len(efl.columns)} columns")

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

            sdf = grid_response['data'] if grid_response and 'data' in grid_response and not grid_response['data'].empty else fdf
            if not sdf.empty and 'Symbol' in sdf.columns and 'Tier' in sdf.columns:
                al = sdf['Symbol'].dropna().unique().tolist(); atv = ",".join([f"nse:{s}" for s in al])
                ta = sdf[sdf['Tier']=='A']['Symbol'].dropna().unique().tolist(); tatv = ",".join([f"nse:{s}" for s in ta])
                tb = sdf[sdf['Tier']=='B']['Symbol'].dropna().unique().tolist(); tbtv = ",".join([f"nse:{s}" for s in tb])
                st.markdown("---"); st.subheader("Copy Symbols to TradingView")
                cc1, cc2, cc3 = st.columns(3)
                with cc1:
                    st.markdown(f"**Tier A only** — `{len(ta)} symbols`")
                    if ta: st.code(tatv, language=None); st.caption(f"Click to copy {len(ta)} symbols.")
                    else: st.info("No Tier A stocks.")
                with cc2:
                    st.markdown(f"**Tier B only** — `{len(tb)} symbols`")
                    if tb: st.code(tbtv, language=None); st.caption(f"Click to copy {len(tb)} symbols.")
                    else: st.info("No Tier B stocks.")
                with cc3:
                    st.markdown(f"**All filtered** — `{len(al)} symbols`")
                    if al:
                        if st.button("Copy All", key="copy_all"): st.code(atv, language=None); st.caption(f"Click to copy {len(al)} symbols.")
                    else: st.info("No symbols in view.")
        else:
            st.info("Click 'Generate GTT Trading Plan' to load data.")

    with tab2:
        if 'gtt_scored_df' in st.session_state and st.session_state.gtt_scored_df is not None:
            sdf = st.session_state.gtt_scored_df.copy()
            if 'Sector' in sdf.columns:
                tab = sdf[sdf['Tier'].isin(['A','B'])].copy()
                if not tab.empty:
                    ss = tab.groupby('Sector').agg(Tier_A_Count=('Tier',lambda x:(x=='A').sum()),Tier_B_Count=('Tier',lambda x:(x=='B').sum()),Total_Count=('Symbol','count'),Avg_RS=('Avg_RS','mean'),Avg_Total_Score=('Total_Score','mean')).round(2).sort_values('Total_Count',ascending=False).reset_index()
                    st.subheader("Sector Concentration (Tier A + B)")
                    sgb = GridOptionsBuilder.from_dataframe(ss); sgb.configure_default_column(resizable=True,filterable=True,sortable=True,minWidth=70,flex=0); sgb.configure_side_bar(); sgb.configure_grid_options(enableBrowserTooltips=True)
                    for c in ss.columns: sgb.configure_column(c, headerTooltip=c)
                    if 'Avg_RS' in ss.columns:
                        vr = ss[ss['Avg_RS']>0]['Avg_RS']; rmin=float(vr.min()) if not vr.empty else 0; rmax=float(vr.max()) if not vr.empty else 100
                        sgb.configure_column('Avg_RS', minWidth=70, maxWidth=100, cellStyle=JsCode(f"""function(p){{const v=p.value;if(v===null||v===undefined||v<=0)return null;const m={rmin},x={rmax};if(x===m)return{{'backgroundColor':'#fff','color':'black'}};const r=(v-m)/(x-m);let a,b,c;if(r<0.5){{const p2=r/0.5;a=255;b=Math.round(100+155*p2);c=Math.round(100+155*p2)}}else{{const p2=(r-0.5)/0.5;a=Math.round(255-155*p2);b=255;c=Math.round(255-155*p2)}}return{{'backgroundColor':'rgb('+a+','+b+','+c+')','color':'black','fontWeight':r>=0.9?'bold':'normal'}}}}"""))
                    if 'Avg_Total_Score' in ss.columns:
                        vs = ss[ss['Avg_Total_Score']>0]['Avg_Total_Score']; smin=float(vs.min()) if not vs.empty else 0; smax=float(vs.max()) if not vs.empty else 14
                        sgb.configure_column('Avg_Total_Score', minWidth=90, maxWidth=120, headerName='Avg Score', cellStyle=JsCode(f"""function(p){{const v=p.value;if(v===null||v===undefined)return null;const m={smin},x={smax};if(x===m)return{{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'}};const r=(v-m)/(x-m);let a,b,c;if(r<0.5){{const p2=r/0.5;a=255;b=Math.round(100+155*p2);c=Math.round(100+155*p2)}}else{{const p2=(r-0.5)/0.5;a=Math.round(255-155*p2);b=255;c=Math.round(255-155*p2)}}return{{'backgroundColor':'rgb('+a+','+b+','+c+')','color':'black','fontWeight':r>=0.9?'bold':'normal'}}}}"""))
                    sgb.configure_column('Sector', minWidth=140, maxWidth=200, pinned='left')
                    AgGrid(ss, gridOptions=sgb.build(), height=400, width='100%', update_mode=GridUpdateMode.MODEL_CHANGED, data_return_mode=DataReturnMode.FILTERED_AND_SORTED, allow_unsafe_jscode=True)

                    st.subheader("Top Setups by Sector")
                    for sec in ss.head(10)['Sector'].tolist():
                        ss2 = tab[tab['Sector']==sec].sort_values('Total_Score',ascending=False).head(15)
                        dc = [c for c in ['Symbol','Tier','Change','Total_Score','Wk_Setup_Score','Wk_RelDist_Score','Wk_TClose_Score','Tight_Score','Vol_Score','Vol_Arriving_Score','Stuck_Risk_Score','TClose_Score','MA20_Score','MA10_Score','Last','_chg_percentclose','Avg_RS','RS_6M','RS_3M','RS_1M','Adr','Ti65','_nr4','_nr4_previous','_rel_tightness','dvol','_avgvol_mln','_20madist','_10madist','W_Dist10wMA','_rel_wk_dist','W_TightCloses_10w','W_InsideBars','W_PctOf10wkHigh','W_CloseChg_Pct','Sector','Industry','Sector_Rank','Sector_Total','Sector_Percentile'] if c in ss2.columns]
                        sd = ss2[dc].copy()
                        with st.expander(f"{sec} ({len(ss2)} stocks)"):
                            gb = GridOptionsBuilder.from_dataframe(sd); gb.configure_default_column(resizable=True,filterable=True,sortable=True,minWidth=70,flex=0); gb.configure_side_bar(); gb.configure_grid_options(enableBrowserTooltips=True)
                            for c in sd.columns: gb.configure_column(c, headerTooltip=c)
                            for col in ['Avg_RS','RS_6M','RS_3M','RS_1M']:
                                if col not in sd.columns: continue
                                vd = sd[sd[col]>0][col]; cmn=vd.min() if not vd.empty else 0; cmx=vd.max() if not vd.empty else 100
                                dj=JsCode(f"""function(p){{const v=p.value;if(v<=0)return null;const m={cmn},x={cmx};if(x===m)return{{'backgroundColor':'#fff','color':'black'}};const r=(v-m)/(x-m);let a,b,c;if(r<0.5){{const p2=r/0.5;a=255;b=Math.round(100+155*p2);c=Math.round(100+155*p2)}}else{{const p2=(r-0.5)/0.5;a=Math.round(255-155*p2);b=255;c=Math.round(255-155*p2)}}return{{'backgroundColor':'rgb('+a+','+b+','+c+')','color':'black','fontWeight':r>=0.9?'bold':'normal'}}}}""")
                                gb.configure_column(col, minWidth=60 if col=='Avg_RS' else 50, maxWidth=90 if col=='Avg_RS' else 80, cellStyle=dj)
                            if '_rel_tightness' in sd.columns: gb.configure_column('_rel_tightness', minWidth=70, maxWidth=90, headerName='Rel Tight', cellStyle=th, comparator=abs_comparator)
                            if '_rel_wk_dist' in sd.columns: gb.configure_column('_rel_wk_dist', minWidth=70, maxWidth=90, headerName='Rel Wk Dist', cellStyle=rel_wk_dist_jscode, comparator=abs_comparator)
                            if '_chg_percentclose' in sd.columns:
                                vc2=sd[sd['_chg_percentclose']>0]['_chg_percentclose']; cmn=float(vc2.min()) if not vc2.empty else 0; cmx=float(vc2.max()) if not vc2.empty else 10
                                gb.configure_column('_chg_percentclose', minWidth=80, maxWidth=110, cellStyle=JsCode(f"""function(p){{const v=p.value;if(!v||v<=0)return null;const m={cmn},x={cmx};if(x===m)return{{'backgroundColor':'#ffe6ff','color':'black'}};const r=Math.min((v-m)/(x-m),1.0);const a=Math.round(255-115*r),b=Math.round(220-220*r),c=Math.round(255-115*r);return{{'backgroundColor':'rgb('+a+','+b+','+c+')','color':r>0.5?'white':'black','fontWeight':r>=0.8?'bold':'normal'}}}}"""), filter='agNumberColumnFilter', filterParams={'filterOptions':['greaterThan','lessThan','equals','inRange'],'defaultOption':'greaterThan','defaultValues':[0]})
                            for col in ['Adr','Ti65','_nr4']:
                                if col in sd.columns: gb.configure_column(col, minWidth=55, maxWidth=75)
                            if 'dvol' in sd.columns and '_avgvol_mln' in sd.columns:
                                vr=sd[(sd['dvol']>0)&(sd['_avgvol_mln']>0)].copy()
                                if not vr.empty:
                                    vr['rr']=vr['dvol']/vr['_avgvol_mln']; aa=vr[vr['rr']>1.0]['rr']
                                    rf=max(float(aa.min()),1.0) if not aa.empty else 1.0; rc=float(aa.max()) if not aa.empty else 3.0
                                else: rf,rc=1.0,3.0
                                rj=JsCode(f"""function(p){{const d=p.data.dvol,a=p.data._avgvol_mln;if(!d||!a||a<=0||d<=0)return null;const r=d/a;if(r<=1.0)return null;const f={rf},c={rc};if(c<=f)return{{'backgroundColor':'#d4edda','color':'black'}};const n=Math.min((r-f)/(c-f),1.0);let a2,b2,c2;if(n<0.5){{const p2=n/0.5;a2=Math.round(248-208*p2);b2=Math.round(255-90*p2);c2=Math.round(248-181*p2)}}else{{const p2=(n-0.5)/0.5;a2=Math.round(40-17*p2);b2=Math.round(165-78*p2);c2=Math.round(67-31*p2)}}return{{'backgroundColor':'rgb('+a2+','+b2+','+c2+')','color':'black','fontWeight':n>=0.8?'bold':'normal'}}}}""")
                                gb.configure_column('dvol', minWidth=60, maxWidth=85, cellStyle=rj)
                                gb.configure_column('_avgvol_mln', minWidth=60, maxWidth=85, cellStyle=rj)
                            md2=JsCode("""function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;const a=Math.abs(v);if(v<-6)return{'backgroundColor':'#f8d7da','color':'#721c24','fontWeight':'bold'};if(a<2)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(a<4)return{'backgroundColor':'#8ee68e','color':'black'};if(a<6)return{'backgroundColor':'#d4edda','color':'black'};return null}""")
                            if '_20madist' in sd.columns: gb.configure_column('_20madist', minWidth=70, maxWidth=90, cellStyle=md2, comparator=abs_comparator)
                            if '_10madist' in sd.columns: gb.configure_column('_10madist', minWidth=70, maxWidth=90, cellStyle=md2, comparator=abs_comparator)
                            if 'W_Dist10wMA' in sd.columns: gb.configure_column('W_Dist10wMA', minWidth=80, maxWidth=110, headerName='Wk 10wMA %', cellStyle=wk_dist_jscode, comparator=abs_comparator)
                            sc2=JsCode("""function(p){const v=p.value;if(v===null||v===undefined)return null;if(v>=3)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(v>=2)return{'backgroundColor':'#8ee68e','color':'black'};if(v>=1)return{'backgroundColor':'#d4edda','color':'black'};if(v<0)return{'backgroundColor':'#f8d7da','color':'#721c24','fontWeight':'bold'};return null}""")
                            for sc_col in ['Wk_Setup_Score','Wk_RelDist_Score','Wk_TClose_Score','Tight_Score','Vol_Score','Vol_Arriving_Score','TClose_Score','MA20_Score','MA10_Score']:
                                if sc_col in sd.columns: gb.configure_column(sc_col, minWidth=45, maxWidth=60, cellStyle=sc2)
                            if 'Stuck_Risk_Score' in sd.columns: gb.configure_column('Stuck_Risk_Score', minWidth=45, maxWidth=60, headerName='Stuck?', cellStyle=JsCode("""function(p){const v=p.value;if(v===null||v===undefined||v>=0)return null;return{'backgroundColor':'#dc3545','color':'white','fontWeight':'bold'}}"""))
                            hs2={'Change':'Chg','_chg_percentclose':'Chg %','_avgvol_mln':'AvgVol','_nr4_previous':'NR4Prev','_10madist':'10MADist','_20madist':'20MADist','Tight_Score':'Tight','Vol_Score':'Vol','TClose_Score':'TClose','MA20_Score':'MA20','MA10_Score':'MA10','W_Dist10wMA':'Wk 10wMA%','_rel_wk_dist':'RelWkDist'}
                            for rc,sn in hs2.items():
                                if rc in sd.columns: gb.configure_column(rc, headerName=sn)
                            if 'Tier' in sd.columns: gb.configure_column('Tier', minWidth=70, maxWidth=85, cellStyle=tj, pinned='left')
                            if 'Change' in sd.columns:
                                cc=JsCode("""function(p){if(!p.value)return null;if(p.value==='New')return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(p.value==='Up')return{'backgroundColor':'#17a2b8','color':'white','fontWeight':'bold'};if(p.value==='Score Up')return{'backgroundColor':'#d4edda','color':'#155724'};if(p.value==='Down')return{'backgroundColor':'#ffc107','color':'#856404','fontWeight':'bold'};if(p.value==='Score Down')return{'backgroundColor':'#fff3cd','color':'#856404'};return null}""")
                                gb.configure_column('Change', minWidth=50, maxWidth=60, cellStyle=cc, headerName='Chg')
                            if 'Total_Score' in sd.columns: gb.configure_column('Total_Score', minWidth=55, maxWidth=70)
                            if 'Symbol' in sd.columns: gb.configure_column('Symbol', cellRenderer=sr, minWidth=150, maxWidth=180, pinned='left')
                            if 'Sector_Rank' in sd.columns: gb.configure_column('Sector_Rank', hide=True)
                            if 'Sector_Total' in sd.columns: gb.configure_column('Sector_Total', hide=True)
                            if 'Sector_Percentile' in sd.columns: gb.configure_column('Sector_Percentile', minWidth=100, maxWidth=120)
                            if 'Sector' in sd.columns: gb.configure_column('Sector', minWidth=120, maxWidth=150)
                            if 'Industry' in sd.columns: gb.configure_column('Industry', minWidth=120, maxWidth=150)
                            gb.configure_grid_options(getRowStyle=JsCode("""function(p){if(!p.data)return null;const c=p.data.Change;if(c==='New')return{'backgroundColor':'#e8f5e9'};if(c==='Up')return{'backgroundColor':'#e1f5fe'};return null}"""))
                            AgGrid(clean_df_for_json(sd), gridOptions=gb.build(), height=400, width='100%', update_mode=GridUpdateMode.MODEL_CHANGED, data_return_mode=DataReturnMode.FILTERED_AND_SORTED, allow_unsafe_jscode=True)
                else: st.info("No Tier A or B stocks found.")
            else: st.info("No sector data available.")
        else: st.info("Generate data first.")

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
            mdf['Status'] = mdf['Total_Score'].apply(lambda x: 'Active in Scanner' if pd.notna(x) and x > 0 else 'Dropped from Scanner')
            ct3 = ['Symbol','Saved_On','Status','Tier','Change','Total_Score','_nr4_previous','_rel_tightness','_rel_wk_dist','_chg_percentclose','Adr','Ti65','_nr4','Avg_RS','Sector','Sector_Percentile','_avgvol_mln','_20madist','_10madist','W_TightCloses_10w','W_PctOf10wkHigh','Last']
            ac3 = [c for c in ct3 if c in mdf.columns]; mdf = mdf[ac3]
            for c in mdf.columns:
                if c not in ['Symbol','Saved_On','Status','Tier','Change']: mdf[c] = mdf[c].fillna('N/A')
            mdf['Tier'] = mdf.get('Tier', pd.Series('Dropped', index=mdf.index)).fillna('Dropped') if 'Tier' in mdf.columns else 'Dropped'
            mdf['Change'] = mdf.get('Change', pd.Series('', index=mdf.index)).fillna('') if 'Change' in mdf.columns else ''
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
            gb3 = GridOptionsBuilder.from_dataframe(mdf); gb3.configure_default_column(resizable=True,filterable=True,sortable=True,minWidth=70,flex=0); gb3.configure_side_bar(); gb3.configure_grid_options(enableBrowserTooltips=True)
            if 'Symbol' in mdf.columns: gb3.configure_column('Symbol', minWidth=90, maxWidth=130, pinned='left')
            gb3.configure_column('Status', minWidth=120, maxWidth=150, cellStyle=JsCode("""function(p){if(!p.value)return null;if(p.value.includes('Active'))return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(p.value.includes('Dropped'))return{'backgroundColor':'#dc3545','color':'white','fontWeight':'bold'};return null}"""))
            sc3 = JsCode("""function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;if(v>=3)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(v>=2)return{'backgroundColor':'#8ee68e','color':'black'};if(v>=1)return{'backgroundColor':'#d4edda','color':'black'};return null}""")
            ts3 = JsCode("""function(p){const v=p.value;if(!v)return null;if(v.includes('A'))return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(v.includes('B'))return{'backgroundColor':'#ffc107','color':'black','fontWeight':'bold'};return{'backgroundColor':'#f8d7da','color':'#721c24'}}""")
            if 'Total_Score' in mdf.columns: gb3.configure_column('Total_Score', minWidth=55, maxWidth=70, cellStyle=sc3)
            if 'Tier' in mdf.columns: gb3.configure_column('Tier', minWidth=55, maxWidth=75, cellStyle=ts3)
            if '_nr4_previous' in mdf.columns: gb3.configure_column('_nr4_previous', minWidth=55, maxWidth=75, cellStyle=JsCode("""function(p){return{'backgroundColor':'#fff3cd','color':'#664d03','fontWeight':'bold'}}"""))
            if '_rel_tightness' in mdf.columns: gb3.configure_column('_rel_tightness', minWidth=70, maxWidth=90, headerName='Rel Tight', cellStyle=JsCode("""function(p){return{'backgroundColor':'#fff3cd','color':'#664d03','fontWeight':'bold'}}"""), comparator=abs_comparator)
            if '_rel_wk_dist' in mdf.columns: gb3.configure_column('_rel_wk_dist', minWidth=70, maxWidth=90, headerName='Rel Wk Dist', cellStyle=rel_wk_dist_jscode, comparator=abs_comparator)
            if '_chg_percentclose' in mdf.columns: gb3.configure_column('_chg_percentclose', minWidth=80, maxWidth=110)
            if 'Adr' in mdf.columns: gb3.configure_column('Adr', minWidth=55, maxWidth=75)
            if 'Ti65' in mdf.columns: gb3.configure_column('Ti65', minWidth=55, maxWidth=75)
            if 'Avg_RS' in mdf.columns: gb3.configure_column('Avg_RS', minWidth=55, maxWidth=75)
            hs3 = {'Change':'Chg','_chg_percentclose':'Chg %','_avgvol_mln':'AvgVolcr','Sector_Percentile':'SectPctile','_nr4_previous':'NR4Prev','_10madist':'10MADist','_20madist':'20MADist','W_PctOf10wkHigh':'Wk % of 10wHi','W_TightCloses_10w':'Wk TightCl/5','_rel_wk_dist':'RelWkDist'}
            for rc, sn in hs3.items():
                if rc in mdf.columns: gb3.configure_column(rc, headerName=sn)
            AgGrid(clean_df_for_json(mdf), gridOptions=gb3.build(), update_mode=GridUpdateMode.MODEL_CHANGED, fit_columns_on_grid_load=False, height=600, theme='streamlit', key='nse_saved_breakouts_grid', allow_unsafe_jscode=True)
        elif not saved_breakouts:
            st.info("No saved breakouts yet. Select rows in the main scanner or add a ticker manually above.")
        else:
            st.warning("Click 'Generate GTT Trading Plan' first to pull live data for your saved tickers.")

if __name__ == "__main__":
    main()