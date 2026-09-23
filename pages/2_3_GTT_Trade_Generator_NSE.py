import streamlit as st
import pandas as pd
import numpy as np
import requests
from io import StringIO
from pandas.api.types import (
    is_categorical_dtype,
    is_datetime64_any_dtype,
    is_numeric_dtype,
    is_object_dtype,
)
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode, GridUpdateMode, DataReturnMode
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
import os
import json
import time
from datetime import datetime

# st.set_page_config MUST be the first Streamlit command
st.set_page_config(page_title="GTT Trade Generator (NSE)", page_icon="⚡", layout="wide")

# ── Supabase Integration ──
from supabase import create_client, Client

SUPABASE_URL = "https://uroqarbpyrloymijbqaa.supabase.co"
SUPABASE_KEY = "sb_publishable_bPnWVx9S7zI0_FdK8RCbRg_Gfc2Vqzt"

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    st.error(f"Database connection failed: {e}")
    supabase = None

# --- 1. CONFIGURATION & ENDPOINTS ---
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
SCORING_PREFS_FILE = os.path.join(BASE_DIR, "gtt_us_scoring_prefs.json")


# ════════════════════════════════════════════════════════════════════
# 2. DATABASE & HELPER FUNCTIONS
# ════════════════════════════════════════════════════════════════════
def load_column_prefs(table_key):
    if not supabase: return None
    try:
        response = supabase.table("column_prefs").select("visible_columns").eq("table_key", table_key).eq("user_id", "nse_user").execute()
        if response.data:
            return response.data[0]['visible_columns']
        return None
    except Exception:
        return None


def save_column_prefs(table_key, cols):
    if not supabase: return
    try:
        existing = supabase.table("column_prefs").select("id").eq("table_key", table_key).eq("user_id", "nse_user").execute()
        if existing.data:
            supabase.table("column_prefs").update({"visible_columns": cols}).eq("table_key", table_key).eq("user_id", "nse_user").execute()
        else:
            supabase.table("column_prefs").insert({"user_id": "us_user", "table_key": table_key, "visible_columns": cols}).execute()
    except Exception as e:
        st.warning(f"Could not save column preferences to cloud: {e}")


def get_persisted_columns(table_key, all_cols, default_hidden_cols):
    default_visible = [c for c in all_cols if c not in default_hidden_cols]
    saved = load_column_prefs(table_key)
    if saved is None:
        return default_visible
    saved = [c for c in saved if c in all_cols]
    return saved if saved else default_visible


def load_scoring_prefs(scanner_type: str):
    """Loads scoring prefs from Supabase, falls back to local JSON."""
    local_file = os.path.join(BASE_DIR, f"{scanner_type.lower()}_scoring_prefs.json")

    if not supabase:
        if os.path.exists(local_file):
            try:
                with open(local_file, 'r') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    try:
        response = (supabase.table("scoring_prefs")
                    .select("config")
                    .eq("user_id", "nse_user")
                    .eq("scanner_type", scanner_type)
                    .execute())
        if response.data:
            return response.data[0]['config']
        return {}
    except Exception as e:
        # Fallback to local file if DB error occurs
        if os.path.exists(local_file):
            try:
                with open(local_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}


def save_scoring_prefs(prefs, scanner_type: str):
    """Saves scoring prefs to Supabase, falls back to local JSON."""
    local_file = os.path.join(BASE_DIR, f"{scanner_type.lower()}_scoring_prefs.json")

    if not supabase:
        try:
            with open(local_file, 'w') as f:
                json.dump(prefs, f, indent=2)
        except Exception as e:
            st.warning(f"Could not save scoring preferences locally: {e}")
        return

    try:
        existing = (supabase.table("scoring_prefs")
                    .select("id")
                    .eq("user_id", "nse_user")
                    .eq("scanner_type", scanner_type)
                    .execute())

        if existing.data:
            (supabase.table("scoring_prefs")
             .update({"config": prefs})
             .eq("user_id", "nse_user")
             .eq("scanner_type", scanner_type)
             .execute())
        else:
            supabase.table("scoring_prefs").insert({
                "user_id": "nse_user",
                "scanner_type": scanner_type,
                "config": prefs
            }).execute()
    except Exception as e:
        st.warning(f"Could not save scoring config to cloud: {e}")
        # Try saving locally as a fallback
        try:
            with open(local_file, 'w') as f:
                json.dump(prefs, f, indent=2)
        except Exception:
            pass


@st.cache_data(ttl=3600)
def load_sector_mapping(file_path):
    if not os.path.exists(file_path):
        return None
    df = pd.read_csv(file_path)
    cols_to_keep = ['Symbol', 'Sector', 'Industry']
    df = df[[c for c in cols_to_keep if c in df.columns]]
    df['Symbol'] = df['Symbol'].astype(str).str.upper()
    return df


def get_file_age_days(file_path):
    if not os.path.exists(file_path):
        return None
    mod_time = os.path.getmtime(file_path)
    age_days = int((datetime.now().timestamp() - mod_time) / (24 * 3600))
    return age_days


@st.cache_data(ttl=300)
def fetch_gtt_scan(url, name):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200 and response.text.strip():
            df = pd.read_csv(StringIO(response.text), sep='|', header=None)

            if len(df.columns) < len(gtt_columns):
                df.columns = gtt_columns[:len(df.columns)]
            else:
                df.columns = gtt_columns + [f'Extra_{i}' for i in range(len(gtt_columns), len(df.columns))]

            df['Symbol'] = df['Symbol'].str.upper().str.replace('.NS', '', regex=False)

            numeric_cols_fillna = [
                'Last', '_days_since_bo', '_nr4', '_rs', 'Adr', 'Ti65', 'dvol',
                '_avgvol_mln', '_bo_dollar_vol_mln', '_bo_engulfing_cndl', '_avg_vol_float_ratio',
                '_insideday'
            ]
            for col in numeric_cols_fillna:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

            numeric_cols_keep_nan = ['_20madist', '_10wmadist', '_10madist', '_nr4_previous']
            for col in numeric_cols_keep_nan:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            return df
        return None
    except Exception as e:
        st.error(f"Error fetching {name} scan: {str(e)}")
        return None


@st.cache_data(ttl=300)
def fetch_weekly_scan(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200 and response.text.strip():
            n_metrics = len(weekly_metric_columns)
            rows = []
            for line in response.text.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                fields = line.split('|')
                if len(fields) < n_metrics + 2:
                    continue
                symbol = fields[0]
                last = fields[1]
                metrics = fields[-n_metrics:]
                rows.append([symbol, last] + metrics)

            if not rows:
                return None

            df = pd.DataFrame(rows, columns=['Symbol', 'Last'] + weekly_metric_columns)
            df['Symbol'] = df['Symbol'].astype(str).str.upper().str.replace('.NS', '', regex=False).str.strip()

            numeric_cols = ['Last'] + weekly_metric_columns
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            return df
        return None
    except Exception as e:
        st.error(f"Error fetching Weekly scan: {str(e)}")
        return None


def _is_categorical(series):
    try:
        from pandas.api.types import is_categorical_dtype
        return is_categorical_dtype(series)
    except (ImportError, AttributeError, TypeError):
        return isinstance(series.dtype, pd.CategoricalDtype)


def clean_df_for_json(df):
    df = df.copy()
    df = df.replace([np.inf, -np.inf], np.nan)
    for col in df.columns:
        if df[col].isna().any():
            df[col] = df[col].astype(object)
            df.loc[df[col].isna(), col] = None
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].apply(
                lambda x: x.item() if hasattr(x, 'item') and x is not None else x
            )
    return df


def filter_dataframe(df: pd.DataFrame, scan_mode: str,max_rel_tight: float,min_adr: float, min_avgvol: float) -> pd.DataFrame:
    modify = st.checkbox("Add Advanced Filters")

    check_today_bo = False
    if scan_mode == "Post Breakout":
        check_today_bo = st.checkbox("Check Today Breakouts (Chg% > 0 & Vol_Score >= 1, sorted by Tightness)",
                                     key="check_today_bo")

    check_tight_flags = False

    if scan_mode == "Anticipation":
        check_tight_flags = st.checkbox(
            f"Check high Tight flags (ADR >= {min_adr}, AvgVol >= {min_avgvol}, Rel Tight <= {max_rel_tight})",
            key="check_tight_flags")

    if not modify:
        if check_today_bo:
            if '_chg_percentclose' in df.columns:
                df = df[df['_chg_percentclose'].fillna(0) > 0]
            if 'Vol_Score' in df.columns:
                df = df[df['Vol_Score'].fillna(0) >= 1]
            if '_rel_tightness' in df.columns:
                df['_rel_tightness'] = pd.to_numeric(df['_rel_tightness'], errors='coerce')
                df = df.sort_values(by='_rel_tightness', ascending=True, na_position='last')

        if check_tight_flags:
            if 'Adr' in df.columns:
                df = df[df['Adr'].fillna(0) >= min_adr]
            if '_avgvol_mln' in df.columns:
                df = df[df['_avgvol_mln'].fillna(0) >= min_avgvol]
            if '_rel_tightness' in df.columns:
                df['_rel_tightness'] = pd.to_numeric(df['_rel_tightness'], errors='coerce')
                df = df[df['_rel_tightness'].fillna(999) <= max_rel_tight]
                df = df.sort_values(by='_rel_tightness', ascending=True, na_position='last')

        return df

    df = df.copy()
    with st.container():
        tightness_col = '_nr4' if scan_mode == "Anticipation" else '_nr4_previous'

        if scan_mode == "Post Breakout":
            default_filt = ['_chg_percentclose', 'Adr', 'Sector_Percentile', '_avgvol_mln']
        else:
            default_filt = [tightness_col, 'Sector_Percentile', 'Adr', 'Tier', '_avgvol_mln']

        to_filter_columns = st.multiselect("Filter dataframe on", df.columns, default=default_filt)

        for column in to_filter_columns:
            col_series = df[column]
            has_nans = col_series.isna().any()

            if _is_categorical(col_series) or col_series.dropna().nunique() < 10:
                unique_non_nan = list(col_series.dropna().unique())
                NAN_LABEL = "(blank / NaN)"
                select_options = unique_non_nan + ([NAN_LABEL] if has_nans else [])

                if column == 'Tier':
                    # FIX: Only set defaults that actually exist in select_options
                    default_selection = [t for t in ['A', 'B'] if t in select_options]
                    if not default_selection:
                        default_selection = list(select_options)
                else:
                    default_selection = list(select_options)

                user_cat_input = st.multiselect(
                    f"Values for {column}", select_options, default=default_selection
                )

                nan_selected = NAN_LABEL in user_cat_input
                real_vals = [v for v in user_cat_input if v != NAN_LABEL]

                if nan_selected:
                    mask = col_series.isna() | col_series.isin(real_vals)
                else:
                    mask = ~col_series.isna() & col_series.isin(real_vals)
                df = df[mask]

            elif is_numeric_dtype(col_series):
                clean = col_series.dropna()
                if clean.empty:
                    st.info(f"Column **{column}** has no numeric values")
                    continue

                _min = float(clean.min())
                _max = float(clean.max())

                # FIX: Ensure _max is strictly greater than _min to prevent slider errors
                if _max <= _min:
                    _max = _min + 0.1

                step = (_max - _min) / 100 if (_max - _min) > 0 else 0.1

                custom_max_bounds = {
                    '_nr4': 5.0, '_nr4_previous': 5.0,
                    '_chg_percentclose': 20.0, 'Adr': 15.0,
                    'Sector_Percentile': 100.0, 'Avg_RS': 100.0
                }
                _max = max(_max, custom_max_bounds.get(column, _max))

                custom_ranges = {
                    '_nr4': (0.0, 3.0),
                    '_nr4_previous': (0.0, 3.0),
                    'Adr': (2.0, _max),
                    'Sector_Percentile': (60.0, 100.0),
                    '_chg_percentclose': (2.0, _max),
                    'Ti65': (1.05, _max),
                    'Avg_RS': (92.0, _max),
                    '_avgvol_mln': (10.0, _max)
                }

                default_range = custom_ranges.get(column, (_min, _max))
                default_min = max(float(default_range[0]), _min)
                default_max = min(float(default_range[1]), _max)

                if default_min > default_max:
                    default_min = _min
                    default_max = _max

                user_num_input = st.slider(
                    f"Values for {column}", _min, _max, (default_min, default_max), step=step
                )

                if has_nans:
                    keep_nans = st.checkbox(
                        f"Keep rows where **{column}** is blank",
                        value=True,
                        key=f"keep_nan_{column}"
                    )
                else:
                    keep_nans = False

                in_range = col_series.between(*user_num_input)
                if keep_nans:
                    mask = in_range | col_series.isna()
                else:
                    mask = in_range
                df = df[mask]

            else:
                user_text_input = st.text_input(f"Substring or regex in {column}")
                if user_text_input:
                    text_mask = col_series.astype(str).str.contains(
                        user_text_input, case=False, na=False
                    )
                    df = df[text_mask | col_series.isna()]

    if check_today_bo:
        if '_chg_percentclose' in df.columns:
            df = df[df['_chg_percentclose'].fillna(0) > 0]
        if 'Vol_Score' in df.columns:
            df = df[df['Vol_Score'].fillna(0) >= 1]

        if '_rel_tightness' in df.columns:
            df['_rel_tightness'] = pd.to_numeric(df['_rel_tightness'], errors='coerce')
            df = df.sort_values(by='_rel_tightness', ascending=True, na_position='last')

    if check_tight_flags:
        if 'Adr' in df.columns:
            df = df[df['Adr'].fillna(0) >= 6.0]
        if '_avgvol_mln' in df.columns:
            df = df[df['_avgvol_mln'].fillna(0) >= 10.0]
        if '_rel_tightness' in df.columns:
            df['_rel_tightness'] = pd.to_numeric(df['_rel_tightness'], errors='coerce')
            df = df[df['_rel_tightness'].fillna(999) <= 0.6]
            df = df.sort_values(by='_rel_tightness', ascending=True, na_position='last')

    return df


# --- 3. MAIN APPLICATION ---
def main():
    custom_css = """
    <style>
        html, body, [class*="css"], [class*="st-"] {
            font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif !important;
        }
        .main .block-container {
            padding-top: 2rem;
            padding-bottom: 2rem;
            max-width: 95% !important;
        }
        h1, h2, h3, h4 {
            font-weight: 700 !important;
            letter-spacing: -0.5px !important;
            margin-bottom: 0.5rem !important;
            margin-top: 1.5rem !important;
        }
        .stDataFrame {
            font-size: 14px !important;
        }
    </style>
    """
    st.markdown(custom_css, unsafe_allow_html=True)
    st.title("GTT Trade Generator (NSE)")

    file_age = get_file_age_days(SECTOR_FILE)
    if file_age is not None:
        if file_age == 0:
            st.sidebar.success("Sector data loaded today.")
        elif file_age <= 3:
            st.sidebar.info(f"Sector data loaded {file_age} days ago.")
        else:
            st.sidebar.warning(f"Sector data loaded {file_age} days ago. Update recommended!")
    else:
        st.sidebar.error("Symbols_NSE.csv not found!")

    st.sidebar.markdown("---")
    auto_refresh = st.sidebar.checkbox("Auto-refresh every 10 min", value=False, key="auto_refresh_toggle")
    refresh_clicked = st.sidebar.button("Refresh Now", key="manual_refresh_btn")

    if auto_refresh:
        AUTO_REFRESH_INTERVAL = 600
        if 'last_refresh_ts' not in st.session_state:
            st.session_state.last_refresh_ts = time.time()
        elapsed = time.time() - st.session_state.last_refresh_ts
        if elapsed >= AUTO_REFRESH_INTERVAL or refresh_clicked:
            st.session_state.last_refresh_ts = time.time()
            st.cache_data.clear()
        elapsed = time.time() - st.session_state.last_refresh_ts
        remaining = max(0, int(AUTO_REFRESH_INTERVAL - elapsed))
        last_refresh_dt = datetime.fromtimestamp(st.session_state.last_refresh_ts)
        st.sidebar.caption(f"Last refreshed: {last_refresh_dt.strftime('%H:%M:%S')}")
        mins, secs = divmod(remaining, 60)
        countdown_html = f"""
        <div style="font-size:13px;color:#888;padding:2px 0;font-family:'Source Sans Pro',sans-serif;">
            Next refresh in <span id="cd-m">{mins}</span>m <span id="cd-s">{secs:02d}</span>s
        </div>
        <script>
            let totalSeconds={remaining};
            const minEl=document.getElementById('cd-m'); const secEl=document.getElementById('cd-s');
            const timer=setInterval(function(){{
                totalSeconds--;
                if(totalSeconds<=0){{
                    clearInterval(timer); minEl.textContent='0'; secEl.textContent='00';
                    const buttons=window.top.document.querySelectorAll('button');
                    for(const btn of buttons){{
                        if(btn.textContent.includes('Refresh Now')){{btn.click();return;}}
                    }}
                    window.top.location.reload();
                }} else {{
                    const m=Math.floor(totalSeconds/60); const s=totalSeconds%60;
                    minEl.textContent=m; secEl.textContent=(s<10?'0':'')+s;
                }}
            }},1000);
        </script>
        """
        st.sidebar.markdown(countdown_html, unsafe_allow_html=True)
    else:
        if 'last_refresh_ts' in st.session_state:
            del st.session_state['last_refresh_ts']

    sector_df = load_sector_mapping(SECTOR_FILE)

    scan_mode = st.radio("Select Scanner Mode", ("Anticipation", "Post Breakout"), horizontal=True)
    if scan_mode == "Post Breakout":
        st.markdown("Automated lifecycle manager for Boom Boom, 1-2-3, and Coiled Spring setups.")
    else:
        st.markdown("Anticipation scanner for coiled setups as they are breaking out. BEWARE - MAKE SURE VOLUME IS COMING IN")

    st.sidebar.header("Scoring System Config")
    saved_scoring = load_scoring_prefs("NSE")

    st.sidebar.subheader("1. Weekly Setup (10w MA) — Max 6 pts")
    st.sidebar.caption("The foundation. How close is the weekly price to the 10w MA?")
    wk_defaults = saved_scoring.get('wk_thresholds', [3.0, 6.0])
    w_raw = [
        st.sidebar.number_input("Wk Dist 10wMA < this → 4 pts", value=float(wk_defaults[0]), step=0.5, key="sc_w1"),
        st.sidebar.number_input("Wk Dist 10wMA < this → 2 pts", value=float(wk_defaults[1]), step=0.5, key="sc_w2"),
    ]
    w1, w2 = sorted(w_raw)

    wclose_pts = st.sidebar.number_input(
        "Bonus pts if W_TightCloses (out of 5) >= 1",
        value=int(saved_scoring.get('wclose_pts', 2)),
        min_value=0, max_value=5, step=1, key="sc_wclose"
    )

    st.sidebar.subheader("2. Daily Tightness (Relative to ADR)")
    st.sidebar.caption("The trigger. Ratio = Tightness / ADR. Adjust for high/low beta stocks!")
    tight_defaults = saved_scoring.get('tightness_thresholds', [0.4, 0.6, 0.9, 1.2])
    t_raw = [
        st.sidebar.number_input("Rel Tightness < this → 4 pts", value=float(tight_defaults[0]), step=0.1, key="sc_t1"),
        st.sidebar.number_input("Rel Tightness < this → 3 pts", value=float(tight_defaults[1]), step=0.1, key="sc_t2"),
        st.sidebar.number_input("Rel Tightness < this → 2 pts", value=float(tight_defaults[2]), step=0.1, key="sc_t3"),
        st.sidebar.number_input("Rel Tightness < this → 1 pt", value=float(tight_defaults[3]), step=0.1, key="sc_t4"),
    ]
    t1, t2, t3, t4 = sorted(t_raw)

    st.sidebar.subheader("2. BO Volume (dvol/avg) — Max 3 pts")
    vol_defaults = saved_scoring.get('vol_thresholds', [3.0, 2.0, 1.5])
    v_raw = [
        st.sidebar.number_input("dvol/avg > this -> 3 pts", value=float(vol_defaults[0]), step=0.5, key="sc_v1"),
        st.sidebar.number_input("dvol/avg > this -> 2 pts", value=float(vol_defaults[1]), step=0.5, key="sc_v2"),
        st.sidebar.number_input("dvol/avg > this -> 1 pt", value=float(vol_defaults[2]), step=0.5, key="sc_v3"),
    ]
    v3, v2, v1 = sorted(v_raw)

    st.sidebar.subheader("3. TightCloses Bonus — Brownie Pts")
    tclose_pts = st.sidebar.number_input(
        "Points if W_TightCloses >= 1",
        value=int(saved_scoring.get('tclose_bonus_pts', 2)),
        min_value=0, max_value=5, step=1, key="sc_tclose"
    )

    st.sidebar.subheader("4. 20MADist — Max 3 pts")
    ma20_defaults = saved_scoring.get('ma20_tiers', [2.0, 4.0, 6.0])
    ma20_neg_cutoff = st.sidebar.number_input(
        "Avoid if 20MADist below this %",
        value=float(saved_scoring.get('ma20_neg_cutoff', -6.0)),
        step=0.5, key="sc_ma20_neg"
    )
    ma20_raw = [
        st.sidebar.number_input("abs(20MADist) < this -> 3 pts", value=float(ma20_defaults[0]), step=0.5,
                                key="sc_ma20_1"),
        st.sidebar.number_input("abs(20MADist) < this -> 2 pts", value=float(ma20_defaults[1]), step=0.5,
                                key="sc_ma20_2"),
        st.sidebar.number_input("abs(20MADist) < this -> 1 pt", value=float(ma20_defaults[2]), step=0.5,
                                key="sc_ma20_3"),
    ]
    ma20_t1, ma20_t2, ma20_t3 = sorted(ma20_raw)

    st.sidebar.subheader("5. 10MADist — Max 2 pts")
    ma10_defaults = saved_scoring.get('ma10_tiers', [4.0, 6.0])
    ma10_neg_cutoff = st.sidebar.number_input(
        "Avoid if 10MADist below this %",
        value=float(saved_scoring.get('ma10_neg_cutoff', -6.0)),
        step=0.5, key="sc_ma10_neg"
    )
    ma10_raw = [
        st.sidebar.number_input("abs(10MADist) < this -> 2 pts", value=float(ma10_defaults[0]), step=0.5,
                                key="sc_ma10_1"),
        st.sidebar.number_input("abs(10MADist) < this -> 1 pt", value=float(ma10_defaults[1]), step=0.5,
                                key="sc_ma10_2"),
    ]
    ma10_t1, ma10_t2 = sorted(ma10_raw)

    # ── Quick Filter Config ──
    st.sidebar.subheader("🚩 Quick Filter Config")
    filter_min_adr = st.sidebar.number_input(
        "Min ADR for Tight Flags",
        value=float(saved_scoring.get('filter_min_adr', 4.0)),
        step=0.5, key="sc_f_adr"
    )
    filter_min_avgvol = st.sidebar.number_input(
        "Min AvgVol (Mln) for Tight Flags",
        value=float(saved_scoring.get('filter_min_avgvol', 10.0)),
        step=1.0, key="sc_f_avgvol"
    )

    st.sidebar.subheader("Tier Thresholds")
    tier_a = st.sidebar.number_input(
        "Tier A min score",
        value=int(saved_scoring.get('tier_a_threshold', 10)),
        min_value=1, max_value=14, step=1, key="sc_tier_a"
    )
    tier_b = st.sidebar.number_input(
        "Tier B min score",
        value=int(saved_scoring.get('tier_b_threshold', 7)),
        min_value=1, max_value=14, step=1, key="sc_tier_b"
    )

    if st.sidebar.button("Save scoring config", key="save_scoring_btn"):
        prefs_to_save = {
            'tightness_thresholds': t_raw,
            'wk_thresholds': w_raw,  # <-- ADD
            'wclose_pts': int(wclose_pts),# <-- ADD
            'vol_thresholds': v_raw,
            'tclose_bonus_pts': int(tclose_pts),
            'ma20_tiers': ma20_raw,
            'ma20_neg_cutoff': ma20_neg_cutoff,
            'ma10_tiers': ma10_raw,
            'ma10_neg_cutoff': ma10_neg_cutoff,
            'tier_a_threshold': int(tier_a),
            'tier_b_threshold': int(tier_b),
            # Add the new quick filter configs here:
            'filter_min_adr': float(filter_min_adr),
            'filter_min_avgvol': float(filter_min_avgvol),
        }
        save_scoring_prefs(prefs_to_save,"NSE")
        st.sidebar.success("Saved! Will load by default next session.")

    st.subheader("Strategy & Risk Parameters")
    col1, col2, col3 = st.columns(3)
    with col1:
        account_equity = st.number_input("Total Account Equity ($)", min_value=10000, value=100000, step=10000)
    with col2:
        risk_pct = st.number_input("Max Risk Per Trade (%)", min_value=0.1, value=1.0, step=0.1)
    with col3:
        nr4_threshold = st.number_input("Max Tightness Range (NR4 %)", min_value=1.0, max_value=50.0, value=8.0,
                                        step=0.5)

    manual_fetch = st.button("Generate GTT Trading Plan", type="primary")
    auto_fetch = auto_refresh and ('gtt_base_df' in st.session_state)
    should_fetch = manual_fetch or auto_fetch or refresh_clicked

    if should_fetch:
        fetch_label = "Auto-refreshing scans..." if auto_fetch and not manual_fetch else "Fetching and merging multi-timeframe scans..."
        with st.spinner(fetch_label):
            df_1m = fetch_gtt_scan(gtt_endpoints["1M"], "1M")
            df_3m = fetch_gtt_scan(gtt_endpoints["3M"], "3M")
            df_6m = fetch_gtt_scan(gtt_endpoints["6M"], "6M")

            if df_1m is not None and not df_1m.empty:
                df_1m_renamed = df_1m.rename(columns={'_rs': 'RS_1M'})
                df_3m_renamed = df_3m.rename(
                    columns={'_rs': 'RS_3M'}) if df_3m is not None and not df_3m.empty else None
                df_6m_renamed = df_6m.rename(
                    columns={'_rs': 'RS_6M'}) if df_6m is not None and not df_6m.empty else None

                non_rs_cols = [c for c in df_1m_renamed.columns if c not in ['Symbol', 'RS_1M', 'RS_3M', 'RS_6M']]
                base_df = df_1m_renamed.copy()

                if df_3m_renamed is not None:
                    base_df = base_df.merge(df_3m_renamed, on='Symbol', how='outer', suffixes=('', '_3m'))
                    for col in non_rs_cols:
                        col_3m = f'{col}_3m'
                        if col_3m in base_df.columns:
                            base_df[col] = base_df[col].fillna(base_df[col_3m])
                            base_df.drop(col_3m, axis=1, inplace=True)
                else:
                    base_df['RS_3M'] = 0

                if df_6m_renamed is not None:
                    base_df = base_df.merge(df_6m_renamed, on='Symbol', how='outer', suffixes=('', '_6m'))
                    for col in non_rs_cols:
                        col_6m = f'{col}_6m'
                        if col_6m in base_df.columns:
                            base_df[col] = base_df[col].fillna(base_df[col_6m])
                            base_df.drop(col_6m, axis=1, inplace=True)
                else:
                    base_df['RS_6M'] = 0

                base_df['RS_1M'] = base_df['RS_1M'].fillna(0)
                base_df['RS_3M'] = base_df['RS_3M'].fillna(0)
                base_df['RS_6M'] = base_df['RS_6M'].fillna(0)

                if sector_df is not None:
                    base_df = base_df.merge(sector_df, on='Symbol', how='left')
                    base_df['Sector'] = base_df['Sector'].fillna('Unknown')
                    base_df['Industry'] = base_df['Industry'].fillna('Unknown')

                actionable_df = base_df
                if not actionable_df.empty:
                    rs_cols = ['RS_6M', 'RS_3M', 'RS_1M']
                    actionable_df['Avg_RS'] = actionable_df[rs_cols].replace(0, np.nan).mean(axis=1).fillna(0).round(2)
                    if 'Sector' in actionable_df.columns:
                        valid_mask = actionable_df['Sector'] != 'Unknown'
                        actionable_df['Sector_Rank'] = 0
                        actionable_df['Sector_Total'] = 0
                        actionable_df['Sector_Percentile'] = 0.0

                        sector_counts = actionable_df[valid_mask].groupby('Sector')['Symbol'].count()

                        actionable_df.loc[valid_mask, 'Sector_Rank'] = actionable_df[valid_mask].groupby('Sector')[
                            'Avg_RS'].rank(ascending=False, method='min').astype(int)
                        actionable_df.loc[valid_mask, 'Sector_Total'] = actionable_df.loc[valid_mask, 'Sector'].map(
                            sector_counts).astype(int)

                        actionable_df.loc[valid_mask, 'Sector_Percentile'] = ((actionable_df.loc[
                                                                                   valid_mask, 'Sector_Total'] -
                                                                               actionable_df.loc[
                                                                                   valid_mask, 'Sector_Rank'] + 1) /
                                                                              actionable_df.loc[
                                                                                  valid_mask, 'Sector_Total'] * 100).round(
                            1)
                    actionable_df['RS_1M'] = actionable_df['RS_1M'].round(2)
                    actionable_df['RS_3M'] = actionable_df['RS_3M'].round(2)
                    actionable_df['RS_6M'] = actionable_df['RS_6M'].round(2)
                    actionable_df['Adr'] = actionable_df['Adr'].round(2)
                    actionable_df['Ti65'] = actionable_df['Ti65'].round(2)
                    actionable_df['_nr4'] = actionable_df['_nr4'].round(2)
                    for col in ['dvol', '_avgvol_mln', '_bo_dollar_vol_mln', '_avg_vol_float_ratio']:
                        if col in actionable_df.columns:
                            actionable_df[col] = actionable_df[col].round(2)

                weekly_df = fetch_weekly_scan(weekly_endpoint)
                if weekly_df is not None and not weekly_df.empty:
                    weekly_full = weekly_df.copy()
                    if sector_df is not None:
                        weekly_full = weekly_full.merge(sector_df, on='Symbol', how='left')
                        weekly_full['Sector'] = weekly_full['Sector'].fillna('Unknown')
                        weekly_full['Industry'] = weekly_full['Industry'].fillna('Unknown')
                    st.session_state.weekly_full_df = weekly_full

                    weekly_subset = weekly_df[['Symbol', 'Pctof10wkhigh', 'Weeklyclose_chg_pct',
                                               'Tightcloses_10w_of5', 'Insidebars_of8', 'Dist_wema10_pct']].rename(columns={
                        'Pctof10wkhigh': 'W_PctOf10wkHigh',
                        'Weeklyclose_chg_pct': 'W_CloseChg_Pct',
                        'Tightcloses_10w_of5': 'W_TightCloses_10w',
                        'Insidebars_of8': 'W_InsideBars',
                        'Dist_wema10_pct': 'W_Dist10wMA'
                    })
                    actionable_df = actionable_df.merge(weekly_subset, on='Symbol', how='left')
                else:
                    st.session_state.weekly_full_df = None
                    st.warning("Weekly scan unavailable — table will show without W_ weekly columns.")

                st.session_state.gtt_base_df = actionable_df
            else:
                st.error("Failed to retrieve base 1M scan data.")
                st.session_state.gtt_base_df = None

    tab1, tab2, tab3 = st.tabs(["GTT Scanner", "Market Themes & Leaders", "Saved Breakouts"])

    with tab1:
        if 'gtt_base_df' in st.session_state and st.session_state.gtt_base_df is not None:
            actionable_df = st.session_state.gtt_base_df.copy()

            thresholds_ok = (
                    len(set([t1, t2, t3, t4])) >= 4 and
                    len(set([v1, v2, v3])) >= 3 and
                    len(set([ma20_t1, ma20_t2, ma20_t3])) >= 3 and
                    len(set([ma10_t1, ma10_t2])) >= 2
            )

            if not thresholds_ok:
                st.sidebar.error("Scoring Error: Threshold values within a criteria must be unique.")
                actionable_df['Tier'] = 'Error'
                actionable_df['Total_Score'] = 0
                actionable_df['Tight_Score'] = 0
                actionable_df['Vol_Score'] = 0
                actionable_df['TClose_Score'] = 0
                actionable_df['MA20_Score'] = 0
                actionable_df['MA10_Score'] = 0
            else:
                else:
                # 0) Weekly Setup Score (The Foundation)
                if 'W_Dist10wMA' in actionable_df.columns:
                    wk_abs = actionable_df['W_Dist10wMA'].fillna(999).abs()
                    wk_base_score = pd.cut(
                        wk_abs,
                        bins=[-float('inf'), w1, w2, float('inf')],
                        labels=[4, 2, 0]
                    ).astype(int)
                    wk_is_invalid = actionable_df['W_Dist10wMA'].isna()
                    actionable_df['Wk_Setup_Score'] = np.where(wk_is_invalid, 0, wk_base_score)
                else:
                    actionable_df['Wk_Setup_Score'] = 0

                actionable_df['Wk_TClose_Score'] = np.where(
                    actionable_df.get('W_TightCloses_10w', pd.Series(0, index=actionable_df.index)).fillna(0) >= 1,
                    wclose_pts, 0
                )

                # 1) Daily Tightness (The Trigger)
                tightness_col = '_nr4' if scan_mode == "Anticipation" else '_nr4_previous'
                safe_adr = actionable_df['Adr'].replace(0, np.nan)
                actionable_df['_rel_tightness'] = (actionable_df[tightness_col] / safe_adr).round(2)
                rel_tight_filled = actionable_df['_rel_tightness'].fillna(999)
                actionable_df['Tight_Score'] = pd.cut(
                    rel_tight_filled,
                    bins=[-float('inf'), t1, t2, t3, t4, float('inf')],
                    labels=[4, 3, 2, 1, 0]
                ).astype(int)

                if scan_mode == "Anticipation":
                    actionable_df['Vol_Score'] = 0
                else:
                    rvol_ratio = np.where(
                        actionable_df['_avgvol_mln'] > 0,
                        actionable_df['dvol'] / actionable_df['_avgvol_mln'],
                        0
                    )
                    actionable_df['Vol_Score'] = pd.cut(
                        rvol_ratio,
                        bins=[-float('inf'), v3, v2, v1, float('inf')],
                        labels=[0, 1, 2, 3]
                    ).astype(int)

                actionable_df['TClose_Score'] = np.where(
                    actionable_df['W_TightCloses_10w'].fillna(0) >= 1,
                    tclose_pts,
                    0
                )

                ma20_filled = actionable_df['_20madist'].fillna(999)
                ma20_abs = ma20_filled.abs()
                ma20_base_score = pd.cut(
                    ma20_abs,
                    bins=[-float('inf'), ma20_t1, ma20_t2, ma20_t3, float('inf')],
                    labels=[3, 2, 1, 0]
                ).astype(int)
                ma20_is_invalid = actionable_df['_20madist'].isna() | (actionable_df['_20madist'] < ma20_neg_cutoff)
                actionable_df['MA20_Score'] = np.where(ma20_is_invalid, 0, ma20_base_score)

                ma10_filled = actionable_df['_10madist'].fillna(999)
                ma10_abs = ma10_filled.abs()
                ma10_base_score = pd.cut(
                    ma10_abs,
                    bins=[-float('inf'), ma10_t1, ma10_t2, float('inf')],
                    labels=[2, 1, 0]
                ).astype(int)
                ma10_is_invalid = actionable_df['_10madist'].isna() | (actionable_df['_10madist'] < ma10_neg_cutoff)
                actionable_df['MA10_Score'] = np.where(ma10_is_invalid, 0, ma10_base_score)

                actionable_df['Total_Score'] = (
                        actionable_df['Wk_Setup_Score'] +
                        actionable_df['Wk_TClose_Score'] +
                        actionable_df['Tight_Score'] +
                        actionable_df['Vol_Score'] +
                        actionable_df['TClose_Score'] +
                        actionable_df['MA20_Score'] +
                        actionable_df['MA10_Score']
                )

                conditions = [
                    actionable_df['Total_Score'] >= tier_a,
                    actionable_df['Total_Score'] >= tier_b,
                ]
                choices = ['A', 'B']
                actionable_df['Tier'] = np.select(conditions, choices, default='Ignore')

            tier_order_map = {'A': 0, 'B': 1, 'Ignore': 2, 'Error': 3}
            if 'prev_scan_data' in st.session_state and st.session_state.prev_scan_data is not None:
                prev = st.session_state.prev_scan_data
                actionable_df['Change'] = ''
                for idx, row in actionable_df.iterrows():
                    symbol = row['Symbol']
                    cur_tier, cur_score = row['Tier'], row['Total_Score']
                    if symbol not in prev:
                        actionable_df.at[idx, 'Change'] = 'New'
                    else:
                        prev_tier, prev_score = prev[symbol]['tier'], prev[symbol]['score']
                        cur_rank = tier_order_map.get(cur_tier, 9)
                        prev_rank = tier_order_map.get(prev_tier, 9)
                        if cur_rank < prev_rank:
                            actionable_df.at[idx, 'Change'] = 'Up'
                        elif cur_rank > prev_rank:
                            actionable_df.at[idx, 'Change'] = 'Down'
                        elif cur_score > prev_score:
                            actionable_df.at[idx, 'Change'] = 'Score Up'
                        elif cur_score < prev_score:
                            actionable_df.at[idx, 'Change'] = 'Score Down'
                st.session_state.dropped_symbols = set(prev.keys()) - set(actionable_df['Symbol'])
            else:
                actionable_df['Change'] = ''
                st.session_state.dropped_symbols = set()

            st.session_state.prev_scan_data = {
                row['Symbol']: {'tier': row['Tier'], 'score': int(row['Total_Score'])}
                for _, row in actionable_df.iterrows()
            }

            cols = list(actionable_df.columns)
            cols.insert(0, cols.pop(cols.index('Tier')))
            actionable_df = actionable_df[cols]

            st.session_state.gtt_scored_df = actionable_df.copy()

            columns_to_show = [
                'Tier', 'Change', 'Total_Score',
                'Wk_Setup_Score', 'Wk_TClose_Score', 'Tight_Score', 'Vol_Score', 'MA20_Score', 'MA10_Score',
                'W_Dist10wMA', 'W_TightCloses_10w', 'W_PctOf10wkHigh', 'W_InsideBars', 'W_CloseChg_Pct',
                '_nr4_previous', '_rel_tightness', '_chg_percentclose',
                'dvol', '_avgvol_mln',
                '_20madist', '_10madist',
                'Symbol', 'Sector', 'Industry', 'Avg_RS', 'RS_6M', 'RS_3M', 'RS_1M',
                'Adr', 'Ti65', '_nr4',
                '_bo_engulfing_cndl', '_days_since_bo',
                '_bo_dollar_vol_mln', '_circuit', '_avg_vol_float_ratio',
                '_period_perf', '_10wmadist', '_insideday',
            ]

            if 'Sector_Rank' in actionable_df.columns:
                columns_to_show.insert(columns_to_show.index('Symbol') + 1, 'Sector_Rank')
            if 'Sector_Total' in actionable_df.columns:
                columns_to_show.insert(columns_to_show.index('Sector_Rank') + 1, 'Sector_Total')
            if 'Sector_Percentile' in actionable_df.columns:
                columns_to_show.insert(columns_to_show.index('Sector_Total') + 1, 'Sector_Percentile')

            valid_cols = [c for c in columns_to_show if c in actionable_df.columns]
            tier_sort_order = {'A': 0, 'B': 1, 'Ignore': 2, 'Error': 3}
            actionable_df['_tier_sort_key'] = actionable_df['Tier'].map(tier_sort_order).fillna(9)
            display_df = actionable_df[valid_cols].copy()
            display_df['_tier_sort_key'] = actionable_df['_tier_sort_key']

            sort_weekly_col = 'W_Dist10wMA'
            if sort_weekly_col in display_df.columns:
                display_df[sort_weekly_col] = pd.to_numeric(display_df[sort_weekly_col], errors='coerce')
                # Create an absolute value column to sort by (closest to 0 = tightest)
                display_df['_abs_sort_key'] = display_df[sort_weekly_col].abs()

                # Sort by Tier, then by Weekly Distance (tightest first), then by Daily Rel Tightness
                display_df = display_df.sort_values(
                    by=['_tier_sort_key', '_abs_sort_key', '_rel_tightness'],
                    ascending=[True, True, True],
                    na_position='last'
                )
                display_df = display_df.drop(columns=['_abs_sort_key'], errors='ignore')
            else:
                display_df = display_df.sort_values(by=['_tier_sort_key'], ascending=[True])

            display_df = display_df.drop(columns=['_tier_sort_key'], errors='ignore')
            st.session_state.gtt_display_df = display_df

            st.success(f"Generated {len(st.session_state.gtt_display_df)} actionable GTT setups.")

            filtered_df = filter_dataframe(st.session_state.gtt_display_df, scan_mode,t4,filter_min_adr, filter_min_avgvol)

            main_table_default_hidden = [
                'RS_6M', 'RS_3M', 'RS_1M',
                'Industry',
                '_bo_dollar_vol_mln', '_avg_vol_float_ratio', '_period_perf',
                '_10wmadist', '_insideday',
                '_bo_engulfing_cndl', '_days_since_bo', '_circuit',
                'W_CloseChg_Pct',
            ]
            all_main_cols = list(filtered_df.columns)
            with st.expander("Choose visible columns (saved as your default)"):
                selected_main_cols = st.multiselect(
                    "Columns to show in the table below",
                    options=all_main_cols,
                    default=get_persisted_columns('us_main_table', all_main_cols, main_table_default_hidden),
                    key="main_table_col_select",
                )
                if st.button("Save as my default column set", key="save_main_cols_btn"):
                    save_column_prefs('nse_main_table', selected_main_cols)
                    st.success("Saved — this set will load by default next time.")
            hidden_main_cols = [c for c in all_main_cols if c not in selected_main_cols]

            col_precedence = {c: i for i, c in enumerate(columns_to_show) if c in filtered_df.columns}
            selected_main_cols = sorted(selected_main_cols, key=lambda c: col_precedence.get(c, 9999))
            hidden_main_cols = sorted(hidden_main_cols, key=lambda c: col_precedence.get(c, 9999))

            filtered_df = filtered_df[selected_main_cols + hidden_main_cols]

            gb = GridOptionsBuilder.from_dataframe(filtered_df)
            gb.configure_default_column(resizable=True, filterable=True, sortable=True, minWidth=70, flex=0)
            gb.configure_side_bar()
            gb.configure_grid_options(enableBrowserTooltips=True)
            gb.configure_selection(selection_mode='multiple', use_checkbox=True)

            for col in filtered_df.columns:
                gb.configure_column(col, headerTooltip=col)

            for col in ['Avg_RS', 'RS_6M', 'RS_3M', 'RS_1M']:
                if col not in filtered_df.columns:
                    continue
                valid_data = filtered_df[filtered_df[col] > 0][col]
                col_min = valid_data.min() if not valid_data.empty else 0
                col_max = valid_data.max() if not valid_data.empty else 100
                dynamic_jscode = JsCode(f"""
                    function(params) {{
                        const val = params.value;
                        if (val <= 0) return null;
                        const min = {col_min}; const max = {col_max};
                        if (max === min) return {{ 'backgroundColor': '#ffffff', 'color': 'black' }};
                        const ratio = (val - min) / (max - min);
                        let r, g, b;
                        if (ratio < 0.5) {{ const pct = ratio / 0.5; r = 255; g = Math.round(100 + (155 * pct)); b = Math.round(100 + (155 * pct)); }}
                        else {{ const pct = (ratio - 0.5) / 0.5; r = Math.round(255 - (155 * pct)); g = 255; b = Math.round(255 - (155 * pct)); }}
                        return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')', 'color': 'black', 'fontWeight': ratio >= 0.9 ? 'bold' : 'normal' }};
                    }}
                """)
                if col == 'Avg_RS':
                    gb.configure_column(col, minWidth=60, maxWidth=90,
                                        cellStyle=dynamic_jscode)
                else:
                    gb.configure_column(col, minWidth=50, maxWidth=80, cellStyle=dynamic_jscode)

            tightness_highlight_jscode = JsCode(
                """function(params) { return { 'backgroundColor': '#fff3cd', 'color': '#664d03', 'fontWeight': 'bold' }; }""")
            if '_rel_tightness' in filtered_df.columns:
                gb.configure_column('_rel_tightness', minWidth=70, maxWidth=90, headerName='Rel Tight',
                                    cellStyle=tightness_highlight_jscode)

            if '_chg_percentclose' in filtered_df.columns:
                valid_chg = filtered_df[filtered_df['_chg_percentclose'] > 0]['_chg_percentclose']
                chg_min = float(valid_chg.min()) if not valid_chg.empty else 0.0
                chg_max = float(valid_chg.max()) if not valid_chg.empty else 10.0
                chg_jscode = JsCode(f"""
                    function(params) {{
                        const val = params.value; if (!val || val <= 0) return null;
                        const min = {chg_min}; const max = {chg_max};
                        if (max === min) return {{ 'backgroundColor': '#ffe6ff', 'color': 'black' }};
                        const ratio = Math.min((val - min) / (max - min), 1.0);
                        const r = Math.round(255 - (115 * ratio)); const g = Math.round(220 - (220 * ratio)); const b = Math.round(255 - (115 * ratio));
                        return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')', 'color': ratio > 0.5 ? 'white' : 'black', 'fontWeight': ratio >= 0.8 ? 'bold' : 'normal' }};
                    }}
                """)
                gb.configure_column('_chg_percentclose', minWidth=80, maxWidth=110, cellStyle=chg_jscode,
                                    filter='agNumberColumnFilter',
                                    filterParams={'filterOptions': ['greaterThan', 'lessThan', 'equals', 'inRange'],
                                                  'defaultOption': 'greaterThan', 'defaultValues': [0]})

            for col in ['Adr', 'Ti65', '_nr4']:
                if col in filtered_df.columns:
                    gb.configure_column(col, minWidth=55, maxWidth=75)

            if 'dvol' in filtered_df.columns and '_avgvol_mln' in filtered_df.columns:
                valid_rvol = filtered_df[(filtered_df['dvol'] > 0) & (filtered_df['_avgvol_mln'] > 0)].copy()
                if not valid_rvol.empty:
                    valid_rvol['rvol_ratio'] = valid_rvol['dvol'] / valid_rvol['_avgvol_mln']
                    above_avg = valid_rvol[valid_rvol['rvol_ratio'] > 1.0]['rvol_ratio']
                    rvol_floor = max(float(above_avg.min()), 1.0) if not above_avg.empty else 1.0
                    rvol_ceiling = float(above_avg.max()) if not above_avg.empty else 3.0
                else:
                    rvol_floor, rvol_ceiling = 1.0, 3.0

                rvol_jscode = JsCode(f"""
                    function(params) {{
                        const dvol = params.data.dvol; const avgvol = params.data._avgvol_mln;
                        if (!dvol || !avgvol || avgvol <= 0 || dvol <= 0) return null;
                        const ratio = dvol / avgvol; if (ratio <= 1.0) return null;
                        const floor = {rvol_floor}; const ceiling = {rvol_ceiling};
                        if (ceiling <= floor) return {{ 'backgroundColor': '#d4edda', 'color': 'black' }};
                        const normRatio = Math.min((ratio - floor) / (ceiling - floor), 1.0);
                        let r, g, b;
                        if (normRatio < 0.5) {{ const pct = normRatio / 0.5; r = Math.round(248 - (208 * pct)); g = Math.round(255 - (90 * pct)); b = Math.round(248 - (181 * pct)); }}
                        else {{ const pct = (normRatio - 0.5) / 0.5; r = Math.round(40 - (17 * pct)); g = Math.round(165 - (78 * pct)); b = Math.round(67 - (31 * pct)); }}
                        return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')', 'color': 'black', 'fontWeight': normRatio >= 0.8 ? 'bold' : 'normal' }};
                    }}
                """)
                gb.configure_column('dvol', minWidth=60, maxWidth=85, cellStyle=rvol_jscode)
                gb.configure_column('_avgvol_mln', minWidth=60, maxWidth=85, cellStyle=rvol_jscode)

            for col in ['_bo_dollar_vol_mln', '_avg_vol_float_ratio']:
                if col in filtered_df.columns:
                    gb.configure_column(col, minWidth=70, maxWidth=110)

            ma_dist_jscode = JsCode("""
                function(params) {
                    const val = params.value;
                    if (val === null || val === undefined || isNaN(val)) return null;
                    const absVal = Math.abs(val);
                    if (val < -6) return { 'backgroundColor': '#f8d7da', 'color': '#721c24', 'fontWeight': 'bold' };
                    if (absVal < 2) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                    if (absVal < 4) return { 'backgroundColor': '#8ee68e', 'color': 'black' };
                    if (absVal < 6) return { 'backgroundColor': '#d4edda', 'color': 'black' };
                    return null;
                }
            """)
            if '_20madist' in filtered_df.columns:
                gb.configure_column('_20madist', minWidth=70, maxWidth=90, cellStyle=ma_dist_jscode)
            if '_10madist' in filtered_df.columns:
                gb.configure_column('_10madist', minWidth=70, maxWidth=90, cellStyle=ma_dist_jscode)

            score_col_style = JsCode("""
                function(params) {
                    const val = params.value;
                    if (val === null || val === undefined) return null;
                    if (val >= 3) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                    if (val >= 2) return { 'backgroundColor': '#8ee68e', 'color': 'black' };
                    if (val >= 1) return { 'backgroundColor': '#d4edda', 'color': 'black' };
                    return null;
                }
            """)
            for sc_col in ['Tight_Score', 'Vol_Score', 'TClose_Score', 'MA20_Score', 'MA10_Score']:
                if sc_col in filtered_df.columns:
                    gb.configure_column(sc_col, minWidth=45, maxWidth=60, cellStyle=score_col_style)

            wk_pct_jscode = JsCode("""
                function(params) {
                    const val = params.value; if (val === null || val === undefined || isNaN(val)) return null;
                    if (val >= 1.0) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                    if (val >= 0.95) return { 'backgroundColor': '#8ee68e', 'color': 'black' };
                    if (val >= 0.85) return { 'backgroundColor': '#d4edda', 'color': 'black' };
                    return null;
                }
            """)
            if 'W_PctOf10wkHigh' in filtered_df.columns:
                gb.configure_column('W_PctOf10wkHigh', headerName='Wk % of 10wHi', minWidth=95, maxWidth=120,
                                    cellStyle=wk_pct_jscode)
            if 'W_CloseChg_Pct' in filtered_df.columns:
                gb.configure_column('W_CloseChg_Pct', headerName='Wk CloseChg%', minWidth=90, maxWidth=115)
            if 'W_TightCloses_10w' in filtered_df.columns:
                gb.configure_column('W_TightCloses_10w', headerName='Wk Tight 10w/5', minWidth=85, maxWidth=105)
            if 'W_InsideBars' in filtered_df.columns:
                gb.configure_column('W_InsideBars', headerName='Wk InsideB/8', minWidth=85, maxWidth=105)

            header_shortening = {
                'Change': 'Chg',
                '_chg_percentclose': 'Chg %',
                '_avgvol_mln': 'AvgVolcr',
                '_bo_dollar_vol_mln': 'BO$Volcr',
                '_avg_vol_float_ratio': 'VolFloatR',
                '_bo_engulfing_cndl': 'BOEngulf',
                '_days_since_bo': 'DaysSinceBO',
                'Sector_Percentile': 'SectPctile',
                '_nr4_previous': 'NR4Prev',
                '_period_perf': 'PeriodPerf',
                '_10wmadist': '10wMADist',
                '_10madist': '10MADist',
                '_20madist': '20MADist',
                '_insideday': 'InsideDay',
                'Tight_Score': 'Tight',
                'Vol_Score': 'Vol',
                'TClose_Score': 'TClose',
                'MA20_Score': 'MA20',
                'MA10_Score': 'MA10',
                'W_Dist10wMA': 'Wk 10wMA %',
                'W_PctOf10wkHigh': 'Wk % of 10wHi',
                'W_CloseChg_Pct': 'Wk CloseChg%',
                'W_TightCloses': 'Wk TightCl/5',
                'W_InsideBars': 'Wk InsideB/8'
            }
            for raw_col, short_name in header_shortening.items():
                if raw_col in filtered_df.columns:
                    gb.configure_column(raw_col, headerName=short_name)

            tier_jscode = JsCode("""
                function(params) {
                    if (!params.value) return null;
                    if (params.value.includes('A')) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                    if (params.value.includes('B')) return { 'backgroundColor': '#ffc107', 'color': 'black', 'fontWeight': 'bold' };
                    if (params.value.includes('Ignore')) return { 'backgroundColor': '#dc3545', 'color': 'white', 'fontWeight': 'bold' };
                    return null;
                }
            """)
            gb.configure_column('Tier', minWidth=70, maxWidth=85, cellStyle=tier_jscode, pinned='left')
            gb.configure_column('Total_Score', minWidth=55, maxWidth=70)

            if 'Sector_Rank' in filtered_df.columns:
                gb.configure_column('Sector_Rank', hide=True)
            if 'Sector_Total' in filtered_df.columns:
                gb.configure_column('Sector_Total', hide=True)
            if 'Sector_Percentile' in filtered_df.columns:
                gb.configure_column('Sector_Percentile', minWidth=100, maxWidth=120)

            symbol_renderer_jscode = JsCode("""
            function(params) {
                const symbol = params.value;
                const rank = params.data.Sector_Rank;
                const total = params.data.Sector_Total;
                if (rank && total && rank > 0) {
                    return symbol + ' (' + rank + '/' + total + ')';
                }
                return symbol;
            }
            """)
            gb.configure_column('Symbol', cellRenderer=symbol_renderer_jscode, minWidth=150, maxWidth=180,
                                pinned='left', checkboxSelection=True)

            if 'Sector' in filtered_df.columns:
                gb.configure_column('Sector', minWidth=120, maxWidth=150)
            if 'Industry' in filtered_df.columns:
                gb.configure_column('Industry', minWidth=120, maxWidth=150)

            for col in hidden_main_cols:
                gb.configure_column(col, hide=True)

            go = gb.build()

            safe_df = clean_df_for_json(filtered_df)
            grid_response = AgGrid(safe_df, gridOptions=go, height=600, width='100%',
                                   update_mode=GridUpdateMode.MODEL_CHANGED,
                                   data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                                   allow_unsafe_jscode=True)

            selected_rows = grid_response['selected_rows']
            if selected_rows is not None and len(selected_rows) > 0:
                if isinstance(selected_rows, pd.DataFrame):
                    selected_symbols = selected_rows['Symbol'].tolist()
                else:
                    selected_symbols = [row.get('Symbol') for row in selected_rows if row is not None]

                st.markdown(f"**{len(selected_symbols)} symbols selected.**")
                if st.button("Add selected to Saved Breakouts"):
                    try:
                        response = supabase.table("saved_breakouts").select("symbol").eq("user_id", "us_user").execute()
                        existing_symbols = [b['symbol'] for b in response.data]
                    except:
                        existing_symbols = []

                    new_added = 0
                    for sym in selected_symbols:
                        if sym not in existing_symbols:
                            try:
                                supabase.table("saved_breakouts").insert({
                                    "symbol": sym,
                                    "saved_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    "user_id": "us_user"
                                }).execute()
                                new_added += 1
                            except Exception as e:
                                st.error(f"Failed to save {sym}: {e}")
                    if new_added > 0:
                        st.success(f"Added {new_added} new ticker(s) to Saved Breakouts! Check the tab above.")
                    else:
                        st.info("All selected tickers are already in Saved Breakouts.")

            sorted_df = grid_response['data'] if grid_response and 'data' in grid_response and not grid_response[
                'data'].empty else filtered_df

            if not sorted_df.empty and 'Symbol' in sorted_df.columns and 'Tier' in sorted_df.columns:
                all_symbols_sorted = sorted_df['Symbol'].dropna().unique().tolist()
                all_tv_string = ",".join([f"nse:{s}" for s in all_symbols_sorted])

                tier_a_df = sorted_df[sorted_df['Tier'] == 'A']
                tier_a_symbols = tier_a_df['Symbol'].dropna().unique().tolist()
                tier_a_tv_string = ",".join([f"nse:{s}" for s in tier_a_symbols])

                tier_b_df = sorted_df[sorted_df['Tier'] == 'B']
                tier_b_symbols = tier_b_df['Symbol'].dropna().unique().tolist()
                tier_b_tv_string = ",".join([f"nse:{s}" for s in tier_b_symbols])

                st.markdown("---")
                st.subheader("Copy Symbols to TradingView")

                copy_col1, copy_col2, copy_col3 = st.columns(3)

                with copy_col1:
                    st.markdown(f"**Tier A only** — `{len(tier_a_symbols)} symbols`")
                    if tier_a_symbols:
                        st.code(tier_a_tv_string, language=None)
                        st.caption(f"Click the icon above to copy {len(tier_a_symbols)} symbols.")
                    else:
                        st.info("No Tier A stocks.")

                with copy_col2:
                    st.markdown(f"**Tier B only** — `{len(tier_b_symbols)} symbols`")
                    if tier_b_symbols:
                        st.code(tier_b_tv_string, language=None)
                        st.caption(f"Click the icon above to copy {len(tier_b_symbols)} symbols.")
                    else:
                        st.info("No Tier B stocks.")

                with copy_col3:
                    st.markdown(f"**All filtered** — `{len(all_symbols_sorted)} symbols`")
                    if all_symbols_sorted:
                        if st.button("Copy All", key="copy_all"):
                           st.code(all_tv_string, language=None)
                           st.caption(f"Click the icon above to copy {len(all_symbols_sorted)} symbols.")
                    else:
                        st.info("No symbols in view.")

        else:
            st.info("Click 'Generate GTT Trading Plan' to load data.")

    with tab2:
        if 'gtt_scored_df' in st.session_state and st.session_state.gtt_scored_df is not None:
            scored_df = st.session_state.gtt_scored_df.copy()
            if 'Sector' in scored_df.columns:
                tier_ab = scored_df[scored_df['Tier'].isin(['A', 'B'])].copy()
                if not tier_ab.empty:
                    sector_summary = tier_ab.groupby('Sector').agg(
                        Tier_A_Count=('Tier', lambda x: (x == 'A').sum()),
                        Tier_B_Count=('Tier', lambda x: (x == 'B').sum()),
                        Total_Count=('Symbol', 'count'),
                        Avg_RS=('Avg_RS', 'mean'),
                        Avg_Total_Score=('Total_Score', 'mean'),
                    ).round(2).sort_values('Total_Count', ascending=False).reset_index()

                    st.subheader("Sector Concentration (Tier A + B stocks)")

                    ss_gb = GridOptionsBuilder.from_dataframe(sector_summary)
                    ss_gb.configure_default_column(resizable=True, filterable=True, sortable=True, minWidth=70, flex=0)
                    ss_gb.configure_side_bar()
                    ss_gb.configure_grid_options(enableBrowserTooltips=True)
                    for col in sector_summary.columns:
                        ss_gb.configure_column(col, headerTooltip=col)

                    if 'Avg_RS' in sector_summary.columns:
                        valid_rs = sector_summary[sector_summary['Avg_RS'] > 0]['Avg_RS']
                        rs_min = float(valid_rs.min()) if not valid_rs.empty else 0
                        rs_max = float(valid_rs.max()) if not valid_rs.empty else 100
                        rs_jscode = JsCode(f"""
                            function(params) {{
                                const val = params.value;
                                if (val === null || val === undefined || val <= 0) return null;
                                const min = {rs_min}; const max = {rs_max};
                                if (max === min) return {{ 'backgroundColor': '#ffffff', 'color': 'black' }};
                                const ratio = (val - min) / (max - min);
                                let r, g, b;
                                if (ratio < 0.5) {{ const pct = ratio / 0.5; r = 255; g = Math.round(100 + (155 * pct)); b = Math.round(100 + (155 * pct)); }}
                                else {{ const pct = (ratio - 0.5) / 0.5; r = Math.round(255 - (155 * pct)); g = 255; b = Math.round(255 - (155 * pct)); }}
                                return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')', 'color': 'black', 'fontWeight': ratio >= 0.9 ? 'bold' : 'normal' }};
                            }}
                        """)
                        ss_gb.configure_column('Avg_RS', minWidth=70, maxWidth=100, cellStyle=rs_jscode)

                    if 'Avg_Total_Score' in sector_summary.columns:
                        valid_sc = sector_summary[sector_summary['Avg_Total_Score'] > 0]['Avg_Total_Score']
                        sc_min = float(valid_sc.min()) if not valid_sc.empty else 0
                        sc_max = float(valid_sc.max()) if not valid_sc.empty else 14
                        sc_jscode = JsCode(f"""
                            function(params) {{
                                const val = params.value;
                                if (val === null || val === undefined) return null;
                                const min = {sc_min}; const max = {sc_max};
                                if (max === min) return {{ 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' }};
                                const ratio = (val - min) / (max - min);
                                let r, g, b;
                                if (ratio < 0.5) {{ const pct = ratio / 0.5; r = 255; g = Math.round(100 + (155 * pct)); b = Math.round(100 + (155 * pct)); }}
                                else {{ const pct = (ratio - 0.5) / 0.5; r = Math.round(255 - (155 * pct)); g = 255; b = Math.round(255 - (155 * pct)); }}
                                return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')', 'color': 'black', 'fontWeight': ratio >= 0.9 ? 'bold' : 'normal' }};
                            }}
                        """)
                        ss_gb.configure_column('Avg_Total_Score', minWidth=90, maxWidth=120, headerName='Avg Score',
                                               cellStyle=sc_jscode)
                    ss_gb.configure_column('Sector', minWidth=140, maxWidth=200, pinned='left')
                    ss_go = ss_gb.build()
                    AgGrid(sector_summary, gridOptions=ss_go, height=400, width='100%',
                           update_mode=GridUpdateMode.MODEL_CHANGED,
                           data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                           allow_unsafe_jscode=True)

                    st.subheader("Top Setups by Sector")
                    for sector in sector_summary.head(10)['Sector'].tolist():
                        sector_stocks = tier_ab[tier_ab['Sector'] == sector].sort_values(
                            'Total_Score', ascending=False).head(15)
                        display_cols = [c for c in [
                            'Symbol', 'Tier', 'Change', 'Total_Score',
                            'Tight_Score', 'Vol_Score', 'TClose_Score', 'MA20_Score', 'MA10_Score',
                            'Last', '_chg_percentclose', 'Avg_RS', 'RS_6M', 'RS_3M', 'RS_1M',
                            'Adr', 'Ti65', '_nr4', '_nr4_previous', '_rel_tightness',
                            'dvol', '_avgvol_mln', '_20madist', '_10madist',
                            'W_TightCloses', 'W_InsideBars', 'W_PctOf10wkHigh', 'W_CloseChg_Pct',
                            'Sector', 'Industry', 'Sector_Rank', 'Sector_Total', 'Sector_Percentile'
                        ] if c in sector_stocks.columns]
                        sector_display = sector_stocks[display_cols].copy()

                        with st.expander(f"{sector} ({len(sector_stocks)} stocks)"):
                            gb = GridOptionsBuilder.from_dataframe(sector_display)
                            gb.configure_default_column(resizable=True, filterable=True, sortable=True, minWidth=70,
                                                        flex=0)
                            gb.configure_side_bar()
                            gb.configure_grid_options(enableBrowserTooltips=True)
                            for col in sector_display.columns:
                                gb.configure_column(col, headerTooltip=col)

                            for col in ['Avg_RS', 'RS_6M', 'RS_3M', 'RS_1M']:
                                if col not in sector_display.columns:
                                    continue
                                valid_data = sector_display[sector_display[col] > 0][col]
                                col_min = valid_data.min() if not valid_data.empty else 0
                                col_max = valid_data.max() if not valid_data.empty else 100
                                dynamic_jscode = JsCode(f"""
                                    function(params) {{
                                        const val = params.value;
                                        if (val <= 0) return null;
                                        const min = {col_min}; const max = {col_max};
                                        if (max === min) return {{ 'backgroundColor': '#ffffff', 'color': 'black' }};
                                        const ratio = (val - min) / (max - min);
                                        let r, g, b;
                                        if (ratio < 0.5) {{ const pct = ratio / 0.5; r = 255; g = Math.round(100 + (155 * pct)); b = Math.round(100 + (155 * pct)); }}
                                        else {{ const pct = (ratio - 0.5) / 0.5; r = Math.round(255 - (155 * pct)); g = 255; b = Math.round(255 - (155 * pct)); }}
                                        return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')', 'color': 'black', 'fontWeight': ratio >= 0.9 ? 'bold' : 'normal' }};
                                    }}
                                """)
                                if col == 'Avg_RS':
                                    gb.configure_column(col, minWidth=60, maxWidth=90, cellStyle=dynamic_jscode)
                                else:
                                    gb.configure_column(col, minWidth=50, maxWidth=80, cellStyle=dynamic_jscode)

                            nr4_prev_highlight_jscode = JsCode(
                                """function(params) { return { 'backgroundColor': '#fff3cd', 'color': '#664d03', 'fontWeight': 'bold' }; }""")
                            if '_nr4_previous' in sector_display.columns:
                                gb.configure_column('_nr4_previous', minWidth=55, maxWidth=75,
                                                    cellStyle=nr4_prev_highlight_jscode)
                            if '_rel_tightness' in sector_display.columns:
                                gb.configure_column('_rel_tightness', minWidth=70, maxWidth=90, headerName='Rel Tight',
                                                    cellStyle=nr4_prev_highlight_jscode)

                            if '_chg_percentclose' in sector_display.columns:
                                valid_chg = sector_display[sector_display['_chg_percentclose'] > 0]['_chg_percentclose']
                                chg_min = float(valid_chg.min()) if not valid_chg.empty else 0.0
                                chg_max = float(valid_chg.max()) if not valid_chg.empty else 10.0
                                chg_jscode = JsCode(f"""
                                    function(params) {{
                                        const val = params.value; if (!val || val <= 0) return null;
                                        const min = {chg_min}; const max = {chg_max};
                                        if (max === min) return {{ 'backgroundColor': '#ffe6ff', 'color': 'black' }};
                                        const ratio = Math.min((val - min) / (max - min), 1.0);
                                        const r = Math.round(255 - (115 * ratio)); const g = Math.round(220 - (220 * ratio)); const b = Math.round(255 - (115 * ratio));
                                        return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')', 'color': ratio > 0.5 ? 'white' : 'black', 'fontWeight': ratio >= 0.8 ? 'bold' : 'normal' }};
                                    }}
                                """)
                                gb.configure_column('_chg_percentclose', minWidth=80, maxWidth=110,
                                                    cellStyle=chg_jscode,
                                                    filter='agNumberColumnFilter',
                                                    filterParams={'filterOptions': ['greaterThan', 'lessThan', 'equals',
                                                                                    'inRange'],
                                                                  'defaultOption': 'greaterThan', 'defaultValues': [0]})

                            for col in ['Adr', 'Ti65', '_nr4']:
                                if col in sector_display.columns:
                                    gb.configure_column(col, minWidth=55, maxWidth=75)

                            if 'dvol' in sector_display.columns and '_avgvol_mln' in sector_display.columns:
                                valid_rvol = sector_display[
                                    (sector_display['dvol'] > 0) & (sector_display['_avgvol_mln'] > 0)].copy()
                                if not valid_rvol.empty:
                                    valid_rvol['rvol_ratio'] = valid_rvol['dvol'] / valid_rvol['_avgvol_mln']
                                    above_avg = valid_rvol[valid_rvol['rvol_ratio'] > 1.0]['rvol_ratio']
                                    rvol_floor = max(float(above_avg.min()), 1.0) if not above_avg.empty else 1.0
                                    rvol_ceiling = float(above_avg.max()) if not above_avg.empty else 3.0
                                else:
                                    rvol_floor, rvol_ceiling = 1.0, 3.0
                                rvol_jscode = JsCode(f"""
                                    function(params) {{
                                        const dvol = params.data.dvol; const avgvol = params.data._avgvol_mln;
                                        if (!dvol || !avgvol || avgvol <= 0 || dvol <= 0) return null;
                                        const ratio = dvol / avgvol; if (ratio <= 1.0) return null;
                                        const floor = {rvol_floor}; const ceiling = {rvol_ceiling};
                                        if (ceiling <= floor) return {{ 'backgroundColor': '#d4edda', 'color': 'black' }};
                                        const normRatio = Math.min((ratio - floor) / (ceiling - floor), 1.0);
                                        let r, g, b;
                                        if (normRatio < 0.5) {{ const pct = normRatio / 0.5; r = Math.round(248 - (208 * pct)); g = Math.round(255 - (90 * pct)); b = Math.round(248 - (181 * pct)); }}
                                        else {{ const pct = (normRatio - 0.5) / 0.5; r = Math.round(40 - (17 * pct)); g = Math.round(165 - (78 * pct)); b = Math.round(67 - (31 * pct)); }}
                                        return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')', 'color': 'black', 'fontWeight': normRatio >= 0.8 ? 'bold' : 'normal' }};
                                    }}
                                """)
                                gb.configure_column('dvol', minWidth=60, maxWidth=85, cellStyle=rvol_jscode)
                                gb.configure_column('_avgvol_mln', minWidth=60, maxWidth=85, cellStyle=rvol_jscode)

                            ma_dist_jscode = JsCode("""
                                function(params) {
                                    const val = params.value;
                                    if (val === null || val === undefined || isNaN(val)) return null;
                                    const absVal = Math.abs(val);
                                    if (val < -6) return { 'backgroundColor': '#f8d7da', 'color': '#721c24', 'fontWeight': 'bold' };
                                    if (absVal < 2) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                                    if (absVal < 4) return { 'backgroundColor': '#8ee68e', 'color': 'black' };
                                    if (absVal < 6) return { 'backgroundColor': '#d4edda', 'color': 'black' };
                                    return null;
                                }
                            """)
                            if '_20madist' in sector_display.columns:
                                gb.configure_column('_20madist', minWidth=70, maxWidth=90, cellStyle=ma_dist_jscode)
                            if '_10madist' in sector_display.columns:
                                gb.configure_column('_10madist', minWidth=70, maxWidth=90, cellStyle=ma_dist_jscode)

                            score_col_style = JsCode("""
                                function(params) {
                                    const val = params.value;
                                    if (val === null || val === undefined) return null;
                                    if (val >= 3) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                                    if (val >= 2) return { 'backgroundColor': '#8ee68e', 'color': 'black' };
                                    if (val >= 1) return { 'backgroundColor': '#d4edda', 'color': 'black' };
                                    return null;
                                }
                            """)
                            # Weekly 10wMA heatmap (reuse the daily MA distance styling)
                            if 'W_Dist10wMA' in filtered_df.columns:
                                gb.configure_column('W_Dist10wMA', minWidth=80, maxWidth=110, headerName='Wk 10wMA %',
                                                    cellStyle=ma_dist_jscode)

                            # Style the new score columns
                            for sc_col in ['Wk_Setup_Score', 'Wk_TClose_Score', 'Tight_Score', 'Vol_Score',
                                           'MA20_Score', 'MA10_Score']:
                                if sc_col in sector_display.columns:
                                    gb.configure_column(sc_col, minWidth=45, maxWidth=60, cellStyle=score_col_style)

                            wk_pct_jscode = JsCode("""
                                function(params) {
                                    const val = params.value; if (val === null || val === undefined || isNaN(val)) return null;
                                    if (val >= 1.0) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                                    if (val >= 0.95) return { 'backgroundColor': '#8ee68e', 'color': 'black' };
                                    if (val >= 0.85) return { 'backgroundColor': '#d4edda', 'color': 'black' };
                                    return null;
                                }
                            """)
                            if 'W_PctOf10wkHigh' in sector_display.columns:
                                gb.configure_column('W_PctOf10wkHigh', headerName='Wk % of 10wHi', minWidth=95,
                                                    maxWidth=120, cellStyle=wk_pct_jscode)
                            if 'W_CloseChg_Pct' in sector_display.columns:
                                gb.configure_column('W_CloseChg_Pct', headerName='Wk CloseChg%', minWidth=90,
                                                    maxWidth=115)
                            if 'W_TightCloses_10w' in sector_display.columns:
                                gb.configure_column('W_TightCloses_10w', headerName='Wk Tight 10w/5', minWidth=85,
                                                    maxWidth=105)
                            if 'W_InsideBars' in sector_display.columns:
                                gb.configure_column('W_InsideBars', headerName='Wk InsideB/8', minWidth=85,
                                                    maxWidth=105)

                            header_shortening = {
                                'Change': 'Chg',
                                '_chg_percentclose': 'Chg %',
                                '_avgvol_mln': 'AvgVolMln',
                                '_nr4_previous': 'NR4Prev',
                                '_10madist': '10MADist',
                                '_20madist': '20MADist',
                                'Tight_Score': 'Tight',
                                'Vol_Score': 'Vol',
                                'TClose_Score': 'TClose',
                                'MA20_Score': 'MA20',
                                'MA10_Score': 'MA10',
                            }
                            for raw_col, short_name in header_shortening.items():
                                if raw_col in sector_display.columns:
                                    gb.configure_column(raw_col, headerName=short_name)

                            tier_jscode = JsCode("""
                                function(params) {
                                    if (!params.value) return null;
                                    if (params.value.includes('A')) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                                    if (params.value.includes('B')) return { 'backgroundColor': '#ffc107', 'color': 'black', 'fontWeight': 'bold' };
                                    if (params.value.includes('Ignore')) return { 'backgroundColor': '#dc3545', 'color': 'white', 'fontWeight': 'bold' };
                                    return null;
                                }
                            """)
                            if 'Tier' in sector_display.columns:
                                gb.configure_column('Tier', minWidth=70, maxWidth=85, cellStyle=tier_jscode,
                                                    pinned='left')

                            if 'Change' in sector_display.columns:
                                change_cell_jscode = JsCode("""
                                    function(params) {
                                        if (!params.value) return null;
                                        if (params.value === 'New') return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                                        if (params.value === 'Up') return { 'backgroundColor': '#17a2b8', 'color': 'white', 'fontWeight': 'bold' };
                                        if (params.value === 'Score Up') return { 'backgroundColor': '#d4edda', 'color': '#155724' };
                                        if (params.value === 'Down') return { 'backgroundColor': '#ffc107', 'color': '#856404', 'fontWeight': 'bold' };
                                        if (params.value === 'Score Down') return { 'backgroundColor': '#fff3cd', 'color': '#856404' };
                                        return null;
                                    }
                                """)
                                gb.configure_column('Change', minWidth=50, maxWidth=60, cellStyle=change_cell_jscode,
                                                    headerName='Chg')

                            if 'Total_Score' in sector_display.columns:
                                gb.configure_column('Total_Score', minWidth=55, maxWidth=70)

                            symbol_renderer_jscode = JsCode("""
                            function(params) {
                                const symbol = params.value;
                                const rank = params.data.Sector_Rank;
                                const total = params.data.Sector_Total;
                                if (rank && total && rank > 0) {
                                    return symbol + ' (' + rank + '/' + total + ')';
                                }
                                return symbol;
                            }
                            """)
                            if 'Symbol' in sector_display.columns:
                                gb.configure_column('Symbol', cellRenderer=symbol_renderer_jscode, minWidth=150,
                                                    maxWidth=180, pinned='left')

                            if 'Sector_Rank' in sector_display.columns:
                                gb.configure_column('Sector_Rank', hide=True)
                            if 'Sector_Total' in sector_display.columns:
                                gb.configure_column('Sector_Total', hide=True)
                            if 'Sector_Percentile' in sector_display.columns:
                                gb.configure_column('Sector_Percentile', minWidth=100, maxWidth=120)
                            if 'Sector' in sector_display.columns:
                                gb.configure_column('Sector', minWidth=120, maxWidth=150)
                            if 'Industry' in sector_display.columns:
                                gb.configure_column('Industry', minWidth=120, maxWidth=150)

                            row_style_jscode = JsCode("""
                                function(params) {
                                    if (!params.data) return null;
                                    const change = params.data.Change;
                                    if (change === 'New') return { 'backgroundColor': '#e8f5e9' };
                                    if (change === 'Up') return { 'backgroundColor': '#e1f5fe' };
                                    return null;
                                }
                            """)
                            gb.configure_grid_options(getRowStyle=row_style_jscode)

                            go = gb.build()
                            safe_sector_df = clean_df_for_json(sector_display)
                            AgGrid(safe_sector_df, gridOptions=go, height=400, width='100%',
                                   update_mode=GridUpdateMode.MODEL_CHANGED,
                                   data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                                   allow_unsafe_jscode=True)
                else:
                    st.info("No Tier A or B stocks found.")
            else:
                st.info("No sector data available.")
        else:
            st.info("Generate data first.")

    with tab3:
        st.subheader("Saved Exceptional Breakouts")
        st.caption("Track multi-day bases and retests. Stored securely in your Supabase cloud database.")

        try:
            response = supabase.table("saved_breakouts").select("*").eq("user_id", "us_user").execute()
            saved_breakouts = response.data
        except Exception as e:
            saved_breakouts = []
            st.error(f"Database error: {e}")

        col1, col2 = st.columns([3, 1])
        with col1:
            new_ticker = st.text_input("Enter Ticker to Track manually (e.g., AAPL, TSLA):",
                                       key="save_ticker_input").upper().strip()
        with col2:
            st.write("")
            st.write("")
            if st.button("Save Ticker", key="save_ticker_btn") and new_ticker:
                exists = any(b['symbol'] == new_ticker for b in saved_breakouts)
                if not exists:
                    try:
                        supabase.table("saved_breakouts").insert({
                            "symbol": new_ticker,
                            "saved_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "user_id": "us_user"
                        }).execute()
                        st.success(f"Saved {new_ticker} to cloud!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to save: {e}")
                else:
                    st.warning("Ticker already saved.")

        st.markdown("---")

        if 'gtt_scored_df' in st.session_state and st.session_state.gtt_scored_df is not None and saved_breakouts:
            live_df = st.session_state.gtt_scored_df.copy()

            saved_df = pd.DataFrame(saved_breakouts)
            saved_df = saved_df.rename(columns={'symbol': 'Symbol', 'saved_date': 'Saved_On'})

            merged_df = saved_df[['Symbol', 'Saved_On']].merge(live_df, on='Symbol', how='left')

            merged_df['Status'] = merged_df['Total_Score'].apply(
                lambda x: 'Active in Scanner' if pd.notna(x) and x > 0 else 'Dropped from Scanner')

            columns_to_show_tab3 = [
                'Symbol', 'Saved_On', 'Status',
                'Tier', 'Change', 'Total_Score',
                '_nr4_previous', '_rel_tightness', '_chg_percentclose', 'Adr', 'Ti65', '_nr4',
                'Avg_RS', 'Sector', 'Sector_Percentile',
                '_avgvol_mln', '_20madist', '_10madist',
                'W_TightCloses_10w', 'W_PctOf10wkHigh', 'Last'
            ]
            available_cols_tab3 = [c for c in columns_to_show_tab3 if c in merged_df.columns]
            merged_df = merged_df[available_cols_tab3]

            for col in merged_df.columns:
                if col not in ['Symbol', 'Saved_On', 'Status', 'Tier', 'Change']:
                    merged_df[col] = merged_df[col].fillna('N/A')

            if 'Tier' not in merged_df.columns:
                merged_df['Tier'] = 'Dropped'
            else:
                merged_df['Tier'] = merged_df['Tier'].fillna('Dropped')

            if 'Change' not in merged_df.columns:
                merged_df['Change'] = ''
            else:
                merged_df['Change'] = merged_df['Change'].fillna('')

            st.markdown("---")
            st.subheader("Copy Saved Symbols to TradingView")
            all_saved_symbols = merged_df['Symbol'].dropna().unique().tolist()
            all_tv_string = ",".join([f"nse:{s}" for s in all_saved_symbols])

            copy_col1, copy_col2 = st.columns([1, 2])
            with copy_col1:
                if st.button("Copy All Saved Symbols"):
                    st.code(all_tv_string, language=None)
                    st.caption(f"Click the icon above to copy {len(all_saved_symbols)} symbols.")

            st.markdown("---")

            st.markdown("#### Manage Watchlist")
            cols_to_drop = st.multiselect("Select tickers to remove:", merged_df['Symbol'].tolist(), key="remove_saved")
            if st.button("Remove Selected", key="remove_saved_btn"):
                try:
                    for sym in cols_to_drop:
                        supabase.table("saved_breakouts").delete().eq("symbol", sym).eq("user_id", "us_user").execute()
                    st.success("Removed selected tickers from cloud.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to delete: {e}")

            st.markdown("#### Live Tracked Data (Updates on Scanner Run)")

            gb3 = GridOptionsBuilder.from_dataframe(merged_df)
            gb3.configure_default_column(resizable=True, filterable=True, sortable=True, minWidth=70, flex=0)
            gb3.configure_side_bar()
            gb3.configure_grid_options(enableBrowserTooltips=True)

            if 'Symbol' in merged_df.columns:
                gb3.configure_column('Symbol', minWidth=90, maxWidth=130, pinned='left')

            status_style = JsCode("""
                function(params) {
                    if (!params.value) return null;
                    if (params.value.includes('Active')) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                    if (params.value.includes('Dropped')) return { 'backgroundColor': '#dc3545', 'color': 'white', 'fontWeight': 'bold' };
                    return null;
                }
            """)
            gb3.configure_column('Status', minWidth=120, maxWidth=150, cellStyle=status_style)

            score_col_style = JsCode("""
                function(params) {
                    const val = params.value;
                    if (val === null || val === undefined || isNaN(val)) return null;
                    if (val >= 3) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                    if (val >= 2) return { 'backgroundColor': '#8ee68e', 'color': 'black' };
                    if (val >= 1) return { 'backgroundColor': '#d4edda', 'color': 'black' };
                    return null;
                }
            """)
            tier_style = JsCode("""
                function(params) {
                    const val = params.value;
                    if (!val) return null;
                    if (val.includes('A')) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                    if (val.includes('B')) return { 'backgroundColor': '#ffc107', 'color': 'black', 'fontWeight': 'bold' };
                    return { 'backgroundColor': '#f8d7da', 'color': '#721c24' };
                }
            """)
            if 'Total_Score' in merged_df.columns:
                gb3.configure_column('Total_Score', minWidth=55, maxWidth=70, cellStyle=score_col_style)
            if 'Tier' in merged_df.columns:
                gb3.configure_column('Tier', minWidth=55, maxWidth=75, cellStyle=tier_style)
            if '_nr4_previous' in merged_df.columns:
                gb3.configure_column('_nr4_previous', minWidth=55, maxWidth=75,
                                     cellStyle=JsCode(
                                         "function(params){return{'backgroundColor':'#fff3cd','color':'#664d03','fontWeight':'bold'};}"))
            if '_rel_tightness' in merged_df.columns:
                gb3.configure_column('_rel_tightness', minWidth=70, maxWidth=90, headerName='Rel Tight',
                                     cellStyle=JsCode(
                                         "function(params){return{'backgroundColor':'#fff3cd','color':'#664d03','fontWeight':'bold'};}"))
            if '_chg_percentclose' in merged_df.columns:
                gb3.configure_column('_chg_percentclose', minWidth=80, maxWidth=110)
            if 'Adr' in merged_df.columns:
                gb3.configure_column('Adr', minWidth=55, maxWidth=75)
            if 'Ti65' in merged_df.columns:
                gb3.configure_column('Ti65', minWidth=55, maxWidth=75)
            if 'Avg_RS' in merged_df.columns:
                gb3.configure_column('Avg_RS', minWidth=55, maxWidth=75)

            header_shortening = {
                'Change': 'Chg',
                '_chg_percentclose': 'Chg %',
                '_avgvol_mln': 'AvgVolcr',
                'Sector_Percentile': 'SectPctile',
                '_nr4_previous': 'NR4Prev',
                '_10madist': '10MADist',
                '_20madist': '20MADist',
                'W_PctOf10wkHigh': 'Wk % of 10wHi',
                'W_TightCloses_10w': 'Wk TightCl/5'
            }
            for raw_col, short_name in header_shortening.items():
                if raw_col in merged_df.columns:
                    gb3.configure_column(raw_col, headerName=short_name)

            safe_merged_df = clean_df_for_json(merged_df)
            AgGrid(safe_merged_df, gridOptions=gb3.build(),
                   update_mode=GridUpdateMode.MODEL_CHANGED,
                   fit_columns_on_grid_load=False,
                   height=600, theme='streamlit', key='us_saved_breakouts_grid',
                   allow_unsafe_jscode=True)

        elif not saved_breakouts:
            st.info("No saved breakouts yet. Select rows in the main scanner or add a ticker manually above.")
        else:
            st.warning("Click 'Generate GTT Trading Plan' first to pull live data for your saved tickers.")


if __name__ == "__main__":
    main()