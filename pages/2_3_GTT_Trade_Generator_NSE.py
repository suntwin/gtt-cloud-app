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
from st_aggrid import AgGrid, GridOptionsBuilder
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode, GridUpdateMode, DataReturnMode
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

st.set_page_config(page_title="GTT Trade Generator", page_icon="⚡", layout="wide")


import pandas as pd

def merge_rs_dataframes(dfs_to_merge, rs_cols=['RS_1M', 'RS_3M', 'RS_6M']):
    if not dfs_to_merge:
        return pd.DataFrame()

    base_cols = [c for c in dfs_to_merge[0].columns if c not in rs_cols]

    processed = []
    for df in dfs_to_merge:
        rs_present = [c for c in df.columns if c in rs_cols]
        processed.append(df[base_cols + rs_present].copy())

    merged = processed[0]
    for df in processed[1:]:
        merge_keys = [c for c in base_cols if c in df.columns]
        merged = pd.merge(merged, df, on=merge_keys, how='outer')

    for col in rs_cols:
        if col not in merged.columns:
            merged[col] = 0.0
        else:
            merged[col] = merged[col].fillna(0.0)

    return merged

# --- 1. CONFIGURATION & ENDPOINTS ---
gtt_endpoints = {
    "1M": "https://api.marketinout.com/run/screen?key=dbf1d7c7f45c4fac",
    "3M": "https://api.marketinout.com/run/screen?key=29d147cbc8f1466b",
    "6M": "https://api.marketinout.com/run/screen?key=c53af41692ff4949"
}

weekly_endpoint = "https://api.marketinout.com/run/screen?key=64e86ed22d834681"

weekly_metric_columns = [
    'Wema10', 'Dist_wema10_pct', 'Weeklyclose_chg_pct', 'Tightcloses_of4',
    'Insidebar_thiswk', 'Insidebars_of8', 'Weeklycontraction', 'Pricevs2yrlow_ratio',
    'Pctof10wkhigh', 'Weeklyvolratio', 'Weeklyrsi'
]

gtt_columns = [
    'Symbol', 'Last', 'Timestamp', '_chg_percentclose', 'dvol', '_avgvol_mln','_bo_engulfing_cndl',
    '_circuit', '_days_since_bo', '_bo_dollar_vol_mln', '_rvol_to_float', 'Adr', 'Ti65',
    '_avg_vol_float_ratio', '_20madist', '_10wmadist', '_10madist',
    '_nr4', '_nr4_previous', '_rs', '_period_perf','_insideday'
]

import os
import json
from datetime import datetime
import sys
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
SECTOR_FILE = os.path.join(PROJECT_DIR, "TradingView", "Symbols_NSE.csv")

COLUMN_PREFS_FILE = os.path.join(BASE_DIR, "gtt_column_prefs.json")
SCORING_PREFS_FILE = os.path.join(BASE_DIR, "gtt_scoring_prefs.json")

def load_column_prefs():
    if os.path.exists(COLUMN_PREFS_FILE):
        try:
            with open(COLUMN_PREFS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_column_prefs(prefs):
    try:
        with open(COLUMN_PREFS_FILE, 'w') as f:
            json.dump(prefs, f, indent=2)
    except Exception as e:
        st.warning(f"Could not save column preferences: {e}")


def get_persisted_columns(table_key, all_cols, default_hidden_cols):
    prefs = load_column_prefs()
    default_visible = [c for c in all_cols if c not in default_hidden_cols]
    saved = prefs.get(table_key, default_visible)
    saved = [c for c in saved if c in all_cols]
    return saved if saved else default_visible


def load_scoring_prefs():
    if os.path.exists(SCORING_PREFS_FILE):
        try:
            with open(SCORING_PREFS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_scoring_prefs(prefs):
    try:
        with open(SCORING_PREFS_FILE, 'w') as f:
            json.dump(prefs, f, indent=2)
    except Exception as e:
        st.warning(f"Could not save scoring preferences: {e}")


# --- 2. UTILITY FUNCTIONS ---
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

            # ── Numeric cols that get fillna(0) ──
            numeric_cols_fillna = [
                'Last', '_days_since_bo', '_nr4', '_rs', 'Adr', 'Ti65', 'dvol',
                '_avgvol_mln', '_bo_dollar_vol_mln', '_bo_engulfing_cndl', '_avg_vol_float_ratio',
                '_insideday'
            ]
            for col in numeric_cols_fillna:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

            # ── Numeric cols that KEEP NaN (so scoring can distinguish
            #    "no data" from "value is 0"). NaN → 0 pts in scoring. ──
            numeric_cols_keep_nan = ['_20madist', '_10wmadist', '_10madist', '_nr4_previous']
            for col in numeric_cols_keep_nan:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                    # Leave NaN as NaN — scoring handles it explicitly

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
            df['Symbol'] = df['Symbol'].str.upper().str.replace('.NS', '', regex=False)

            numeric_cols = ['Last'] + weekly_metric_columns
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            return df
        return None
    except Exception as e:
        st.error(f"Error fetching Weekly scan: {str(e)}")
        return None

def filter_dataframe(df: pd.DataFrame, scan_mode: str) -> pd.DataFrame:
    modify = st.checkbox("Add Advanced Filters")
    check_today_bo = st.checkbox("Check Today Breakouts (Chg% > 0 & Vol_Score >= 1, sorted by Tightness)", key="check_today_bo")

    if not modify:
        mask = pd.Series(True, index=df.index)
        if scan_mode == "Post Breakout":
            # Keep your strict Post Breakout filters
            if '_chg_percentclose' in df.columns:
                mask = mask & (df['_chg_percentclose'].fillna(0) > 0)
            if 'Adr' in df.columns:
                mask = mask & (df['Adr'].fillna(0) > 5)
            if 'Ti65' in df.columns:
                mask = mask & (df['Ti65'].fillna(0) > 1.05)
            if 'Avg_RS' in df.columns:
                mask = mask & (df['Avg_RS'].fillna(0) > 94.5)
            if '_avgvol_mln' in df.columns:
                mask = mask & (df['_avgvol_mln'].fillna(0) > 20)
        else:
            # Anticipation Mode Defaults:
            # 1. Coiled tight today (_nr4 <= 3)
            if '_nr4' in df.columns:
                mask = mask & (df['_nr4'].fillna(999) <= 3.0)
            # 2. Sector leadership (Sector_Percentile >= 60)
            if 'Sector_Percentile' in df.columns:
                mask = mask & (df['Sector_Percentile'].fillna(0) >= 60)
            # 3. Enough volatility for 1:4 RR (ADR >= 4)
            if 'Adr' in df.columns:
                mask = mask & (df['Adr'].fillna(0) >= 4)
            # 4. Basic liquidity (keeping a modest floor so micro-caps don't clutter)
            if '_avgvol_mln' in df.columns:
                mask = mask & (df['_avgvol_mln'].fillna(0) >= 10)
        df = df[mask].copy()
        with st.container():
            default_filt = ['_chg_percentclose', 'Adr', 'Ti65', 'Avg_RS',
                            '_avgvol_mln'] if scan_mode == "Post Breakout" else ['_nr4', '_chg_percentclose', 'Sector_Percentile']
            to_filter_columns = st.multiselect("Filter dataframe on", df.columns, default=default_filt)

            for column in to_filter_columns:
                left, right = st.columns((1, 20))
                left.write("↳")

                col_series = df[column]
                has_nans = col_series.isna().any()

                if is_categorical_dtype(col_series) or col_series.dropna().nunique() < 10:
                    unique_non_nan = list(col_series.dropna().unique())
                    NAN_LABEL = "⚠️ (blank / NaN)"
                    select_options = unique_non_nan + ([NAN_LABEL] if has_nans else [])
                    default_selection = list(select_options)

                    user_cat_input = right.multiselect(
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
                        right.info(f"Column **{column}** has no numeric values")
                        continue

                    _min = float(clean.min())
                    _max = float(clean.max())
                    step = (_max - _min) / 100 if (_max - _min) > 0 else 1

                    custom_defaults = {
                        '_chg_percentclose': 2.00, 'Adr': 4.0, 'Ti65': 1.05,
                        'Avg_RS': 92.0, '_avgvol_mln': 20.0, 'Sector_Percentile': 70.00
                    }
                    desired_min = custom_defaults.get(column, _min)
                    default_min = max(desired_min, _min)
                    user_num_input = right.slider(
                        f"Values for {column}", _min, _max, (default_min, _max), step=step
                    )

                    if has_nans:
                        keep_nans = right.checkbox(
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
                    user_text_input = right.text_input(f"Substring or regex in {column}")
                    if user_text_input:
                        text_mask = col_series.astype(str).str.contains(
                            user_text_input, case=False, na=False
                        )
                        df = df[text_mask | col_series.isna()]

    # ── Apply Today Breakouts Filter & Sort ──
    if check_today_bo:
        if '_chg_percentclose' in df.columns:
            df = df[df['_chg_percentclose'].fillna(0) > 0]
        if 'Vol_Score' in df.columns:
            df = df[df['Vol_Score'].fillna(0) >= 1]
        if '_nr4_previous' in df.columns:
            df['_nr4_previous'] = pd.to_numeric(df['_nr4_previous'], errors='coerce')
            df = df.sort_values(by='_nr4_previous', ascending=True, na_position='last')

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
        .css-1d391kg, .css-1Outsk {
            padding-top: 1rem !important;
        }
    </style>
    """
    st.title("⚡ GTT Trade Generator (NSE)")

    file_age = get_file_age_days(SECTOR_FILE)
    if file_age is not None:
        if file_age == 0:
            st.sidebar.success(f"📂 Sector data loaded today.")
        elif file_age <= 3:
            st.sidebar.info(f"📂 Sector data loaded {file_age} days ago.")
        else:
            st.sidebar.warning(f"⚠️ Sector data loaded {file_age} days ago. Update recommended!")
    else:
        st.sidebar.error("⚠️ Symbols_NSE.csv not found!")

    # ══════════════════════════════════════════════════════════════════
    # AUTO-REFRESH TOGGLE (always visible, independent of sector file)
    # ══════════════════════════════════════════════════════════════════
    # AUTO-REFRESH TOGGLE
    # Uses a "Refresh Now" button that JS clicks when the timer expires.
    # This triggers a proper Streamlit rerun (preserving session_state),
    # NOT a full page reload (which loses all filters/state).
    # ══════════════════════════════════════════════════════════════════
    st.sidebar.markdown("---")
    auto_refresh = st.sidebar.checkbox("🔄 Auto-refresh every 10 min", value=False, key="auto_refresh_toggle")

    # Always-visible refresh button (also clicked by JS timer automatically)
    refresh_clicked = st.sidebar.button("🔁 Refresh Now", key="manual_refresh_btn")

    if auto_refresh:
        import time
        import streamlit.components.v1 as components

        AUTO_REFRESH_INTERVAL = 600  # seconds (change to 600 for 10 min)

        if 'last_refresh_ts' not in st.session_state:
            st.session_state.last_refresh_ts = time.time()

        elapsed = time.time() - st.session_state.last_refresh_ts

        # ── If interval elapsed or user clicked Refresh Now ──
        if elapsed >= AUTO_REFRESH_INTERVAL or refresh_clicked:
            st.session_state.last_refresh_ts = time.time()
            st.cache_data.clear()

        # ── Recalculate remaining ──
        elapsed = time.time() - st.session_state.last_refresh_ts
        remaining = max(0, int(AUTO_REFRESH_INTERVAL - elapsed))

        # ── Last refreshed time ──
        last_refresh_dt = datetime.fromtimestamp(st.session_state.last_refresh_ts)
        st.sidebar.caption(f"🕐 Last refreshed: {last_refresh_dt.strftime('%H:%M:%S')}")

        # ── Live countdown timer ──
        # When it hits 0, it clicks the "Refresh Now" button in the parent page.
        # This triggers a proper Streamlit rerun preserving all session_state.
        mins, secs = divmod(remaining, 60)
        countdown_html = f"""
        <div style="font-size: 13px; color: #888; padding: 2px 0; font-family: 'Source Sans Pro', sans-serif;">
            ⏱️ Next refresh in <span id="cd-m">{mins}</span>m <span id="cd-s">{secs:02d}</span>s
        </div>
        <script>
            let totalSeconds = {remaining};
            const minEl = document.getElementById('cd-m');
            const secEl = document.getElementById('cd-s');
            const timer = setInterval(function() {{
                totalSeconds--;
                if (totalSeconds <= 0) {{
                    clearInterval(timer);
                    minEl.textContent = '0';
                    secEl.textContent = '00';
                    // Click the "Refresh Now" button in the parent Streamlit page
                    // This triggers a proper rerun preserving session_state
                    const buttons = window.top.document.querySelectorAll('button');
                    for (const btn of buttons) {{
                        if (btn.textContent.includes('Refresh Now')) {{
                            btn.click();
                            return;
                        }}
                    }}
                    // Fallback if button not found
                    window.top.location.reload();
                }} else {{
                    const m = Math.floor(totalSeconds / 60);
                    const s = totalSeconds % 60;
                    minEl.textContent = m;
                    secEl.textContent = (s < 10 ? '0' : '') + s;
                }}
            }}, 1000);
        </script>
        """
        components.html(countdown_html, height=30)

    else:
        if 'last_refresh_ts' in st.session_state:
            del st.session_state['last_refresh_ts']
    sector_df = load_sector_mapping(SECTOR_FILE)

    scan_mode = st.radio("Select Scanner Mode", ("Anticipation", "Post Breakout"), horizontal=True)
    if scan_mode == "Post Breakout":
        st.markdown("Automated lifecycle manager for Boom Boom, 1-2-3, and Coiled Spring setups.")
    else:
        st.markdown("Anticipation scanner for coiled setups as they are breaking out. BEWARE - MAKE SURE VOLUME IS COMING IN")

    # ══════════════════════════════════════════════════════════════════
    # UNIFIED SCORING SYSTEM CONFIGURATION (Sidebar)
    # ══════════════════════════════════════════════════════════════════
    st.sidebar.header("⚙️ Scoring System Config")
    saved_scoring = load_scoring_prefs()

    # ── Criteria 1: Tightness (_nr4_previous) — Max 4 pts ──
    st.sidebar.subheader("1️⃣ Tightness (_nr4_prev) — Max 4 pts")
    tight_defaults = saved_scoring.get('tightness_thresholds', [4.0, 6.0, 8.0, 10.0])
    t_raw = [
        st.sidebar.number_input("_nr4_prev < this → 4 pts", value=tight_defaults[0], step=0.5, key="sc_t1"),
        st.sidebar.number_input("_nr4_prev < this → 3 pts", value=tight_defaults[1], step=0.5, key="sc_t2"),
        st.sidebar.number_input("_nr4_prev < this → 2 pts", value=tight_defaults[2], step=0.5, key="sc_t3"),
        st.sidebar.number_input("_nr4_prev < this → 1 pt",  value=tight_defaults[3], step=0.5, key="sc_t4"),
    ]
    t1, t2, t3, t4 = sorted(t_raw)

    # ── Criteria 2: BO Volume (dvol/avg) — Max 3 pts ──
    st.sidebar.subheader("2️⃣ BO Volume (dvol/avg) — Max 3 pts")
    vol_defaults = saved_scoring.get('vol_thresholds', [3.0, 2.0, 1.5])
    v_raw = [
        st.sidebar.number_input("dvol/avg > this → 3 pts", value=vol_defaults[0], step=0.5, key="sc_v1"),
        st.sidebar.number_input("dvol/avg > this → 2 pts", value=vol_defaults[1], step=0.5, key="sc_v2"),
        st.sidebar.number_input("dvol/avg > this → 1 pt",  value=vol_defaults[2], step=0.5, key="sc_v3"),
    ]
    v3, v2, v1 = sorted(v_raw)

    # ── Criteria 3: TightCloses Bonus — Brownie Pts ──
    st.sidebar.subheader("3️⃣ TightCloses Bonus — Brownie Pts")
    tclose_pts = st.sidebar.number_input(
        "Points if W_TightCloses ≥ 1",
        value=saved_scoring.get('tclose_bonus_pts', 2),
        min_value=0, max_value=5, step=1, key="sc_tclose"
    )

    # ── Criteria 4a: 20MADist — Max 3 pts ──
    st.sidebar.subheader("4️⃣ 20MADist — Max 3 pts")
    ma20_defaults = saved_scoring.get('ma20_tiers', [2.0, 4.0, 6.0])
    ma20_neg_cutoff = st.sidebar.number_input(
        "Avoid if 20MADist below this %",
        value=saved_scoring.get('ma20_neg_cutoff', -6.0),
        step=0.5, key="sc_ma20_neg"
    )
    ma20_raw = [
        st.sidebar.number_input("abs(20MADist) < this → 3 pts", value=ma20_defaults[0], step=0.5, key="sc_ma20_1"),
        st.sidebar.number_input("abs(20MADist) < this → 2 pts", value=ma20_defaults[1], step=0.5, key="sc_ma20_2"),
        st.sidebar.number_input("abs(20MADist) < this → 1 pt",  value=ma20_defaults[2], step=0.5, key="sc_ma20_3"),
    ]
    ma20_t1, ma20_t2, ma20_t3 = sorted(ma20_raw)

    # ── Criteria 4b: 10MADist — Max 2 pts ──
    st.sidebar.subheader("5️⃣ 10MADist — Max 2 pts")
    ma10_defaults = saved_scoring.get('ma10_tiers', [4.0, 6.0])
    ma10_neg_cutoff = st.sidebar.number_input(
        "Avoid if 10MADist below this %",
        value=saved_scoring.get('ma10_neg_cutoff', -6.0),
        step=0.5, key="sc_ma10_neg"
    )
    ma10_raw = [
        st.sidebar.number_input("abs(10MADist) < this → 2 pts", value=ma10_defaults[0], step=0.5, key="sc_ma10_1"),
        st.sidebar.number_input("abs(10MADist) < this → 1 pt",  value=ma10_defaults[1], step=0.5, key="sc_ma10_2"),
    ]
    ma10_t1, ma10_t2 = sorted(ma10_raw)

    # ── Tier Thresholds ──
    st.sidebar.subheader("🏷️ Tier Thresholds")
    tier_a = st.sidebar.number_input(
        "Tier A (🟢) min score",
        value=saved_scoring.get('tier_a_threshold', 10),
        min_value=1, max_value=14, step=1, key="sc_tier_a"
    )
    tier_b = st.sidebar.number_input(
        "Tier B (🟡) min score",
        value=saved_scoring.get('tier_b_threshold', 7),
        min_value=1, max_value=14, step=1, key="sc_tier_b"
    )

    # ── Save Scoring Config ──
    if st.sidebar.button("💾 Save scoring config", key="save_scoring_btn"):
        prefs_to_save = {
            'tightness_thresholds': t_raw,
            'vol_thresholds': v_raw,
            'tclose_bonus_pts': int(tclose_pts),
            'ma20_tiers': ma20_raw,
            'ma20_neg_cutoff': ma20_neg_cutoff,
            'ma10_tiers': ma10_raw,
            'ma10_neg_cutoff': ma10_neg_cutoff,
            'tier_a_threshold': int(tier_a),
            'tier_b_threshold': int(tier_b),
        }
        save_scoring_prefs(prefs_to_save)
        st.sidebar.success("✅ Saved! Will load by default next session.")

    st.subheader("Strategy & Risk Parameters")
    col1, col2, col3 = st.columns(3)
    with col1:
        account_equity = st.number_input("Total Account Equity ($)", min_value=10000, value=100000, step=10000)
    with col2:
        risk_pct = st.number_input("Max Risk Per Trade (%)", min_value=0.1, value=1.0, step=0.1)
    with col3:
        nr4_threshold = st.number_input("Max Tightness Range (NR4 %)", min_value=1.0, max_value=50.0, value=8.0,
                                        step=0.5)

    # ── Determine if we should fetch data ──

    # 1. User explicitly clicks the button
    # 2. Auto-refresh is on AND interval elapsed (normal rerun with session_state intact)
    # 3. Auto-refresh is on AND no data exists (post page-reload — session_state was cleared)

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
                                                'Tightcloses_of4', 'Insidebars_of8']].rename(columns={
                        'Pctof10wkhigh': 'W_PctOf10wkHigh',
                        'Weeklyclose_chg_pct': 'W_CloseChg_Pct',
                        'Tightcloses_of4': 'W_TightCloses',
                        'Insidebars_of8': 'W_InsideBars',
                    })
                    actionable_df = actionable_df.merge(weekly_subset, on='Symbol', how='left')
                else:
                    st.session_state.weekly_full_df = None
                    st.warning("Weekly scan unavailable — table will show without W_ weekly columns.")

                st.session_state.gtt_base_df = actionable_df
            else:
                st.error("Failed to retrieve base 1M scan data.")
                st.session_state.gtt_base_df = None

    # --- TABS UI ---
    tab1, tab2, tab3 = st.tabs(["🎯 GTT Scanner", "🏛️ Market Themes & Leaders", "📅 Weekly Base Watch"])

    # --- TAB 1: SCANNER ---
    with tab1:
        if 'gtt_base_df' in st.session_state and st.session_state.gtt_base_df is not None:
            actionable_df = st.session_state.gtt_base_df.copy()

            # ══════════════════════════════════════════════════════════════════
            # UNIFIED SCORING CALCULATION
            # Key rule: NaN (no data) → 0 pts always. Never reward missing data.
            # ══════════════════════════════════════════════════════════════════

            thresholds_ok = (
                len(set([t1, t2, t3, t4])) >= 4 and
                len(set([v1, v2, v3])) >= 3 and
                len(set([ma20_t1, ma20_t2, ma20_t3])) >= 3 and
                len(set([ma10_t1, ma10_t2])) >= 2
            )

            if not thresholds_ok:
                st.sidebar.error("⚠️ Scoring Error: Threshold values within a criteria must be unique.")
                actionable_df['Tier'] = '🔴 Error'
                actionable_df['Total_Score'] = 0
                actionable_df['Tight_Score'] = 0
                actionable_df['Vol_Score'] = 0
                actionable_df['TClose_Score'] = 0
                actionable_df['MA20_Score'] = 0
                actionable_df['MA10_Score'] = 0
            else:
                # ── Determine Tightness Column based on Scanner Mode ──
                # Anticipation = Coiled setups (use _nr4). Post Breakout = BO setups (use _nr4_previous)
                tightness_col = '_nr4' if scan_mode == "Anticipation" else '_nr4_previous'
                if tightness_col not in actionable_df.columns:
                    tightness_col = '_nr4_previous'  # Fallback

                # ── Criteria 1: Tightness Score ──
                # NaN → 999 so pd.cut puts it in the 0-pt bin (no data = no reward)
                nr4_filled = actionable_df[tightness_col].fillna(999)
                actionable_df['Tight_Score'] = pd.cut(
                    nr4_filled,
                    bins=[-float('inf'), t1, t2, t3, t4, float('inf')],
                    labels=[4, 3, 2, 1, 0]
                ).astype(int)

                # ── Criteria 2: BO Volume Score ──
                # If Anticipation mode, there is no BO volume yet. Skip calculation and assign 0.
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

                # ── Criteria 3: TightCloses Bonus ──
                # (This remains the same)
                actionable_df['TClose_Score'] = np.where(
                    actionable_df['W_TightCloses'].fillna(0) >= 1,
                    tclose_pts,
                    0
                )
                # ── Criteria 4a: 20MADist Score ──
                # Step 1: fill NaN with 999 so pd.cut works (NaN → 0 pts)
                # Step 2: score by absolute distance
                # Step 3: override to 0 if original was NaN or below negative cutoff
                ma20_filled = actionable_df['_20madist'].fillna(999)
                ma20_abs = ma20_filled.abs()
                ma20_base_score = pd.cut(
                    ma20_abs,
                    bins=[-float('inf'), ma20_t1, ma20_t2, ma20_t3, float('inf')],
                    labels=[3, 2, 1, 0]
                ).astype(int)
                ma20_is_invalid = actionable_df['_20madist'].isna() | (actionable_df['_20madist'] < ma20_neg_cutoff)
                actionable_df['MA20_Score'] = np.where(ma20_is_invalid, 0, ma20_base_score)

                # ── Criteria 4b: 10MADist Score ──
                ma10_filled = actionable_df['_10madist'].fillna(999)
                ma10_abs = ma10_filled.abs()
                ma10_base_score = pd.cut(
                    ma10_abs,
                    bins=[-float('inf'), ma10_t1, ma10_t2, float('inf')],
                    labels=[2, 1, 0]
                ).astype(int)
                ma10_is_invalid = actionable_df['_10madist'].isna() | (actionable_df['_10madist'] < ma10_neg_cutoff)
                actionable_df['MA10_Score'] = np.where(ma10_is_invalid, 0, ma10_base_score)

                # ── Total Score ──
                actionable_df['Total_Score'] = (
                    actionable_df['Tight_Score'] +
                    actionable_df['Vol_Score'] +
                    actionable_df['TClose_Score'] +
                    actionable_df['MA20_Score'] +
                    actionable_df['MA10_Score']
                )

                # ── Tier Assignment ──
                conditions = [
                    actionable_df['Total_Score'] >= tier_a,
                    actionable_df['Total_Score'] >= tier_b,
                ]
                choices = ['🟢 A', '🟡 B']
                actionable_df['Tier'] = np.select(conditions, choices, default='🔴 Ignore')



            # Move Tier to front

            # Move Tier to front
            cols = list(actionable_df.columns)
            cols.insert(0, cols.pop(cols.index('Tier')))
            actionable_df = actionable_df[cols]

            st.session_state.gtt_scored_df = actionable_df.copy()

            columns_to_show = [
                'Tier','Total_Score',
                'Tight_Score', 'Vol_Score', 'TClose_Score', 'MA20_Score', 'MA10_Score',
                '_nr4_previous', '_chg_percentclose','W_TightCloses', 'W_InsideBars', 'W_PctOf10wkHigh',
                'dvol', '_avgvol_mln',
                '_20madist', '_10madist',
                'Symbol', 'Sector', 'Industry', 'Avg_RS', 'RS_6M', 'RS_3M', 'RS_1M',
                'Adr', 'Ti65', '_nr4',
                '_bo_engulfing_cndl', '_days_since_bo',
                '_bo_dollar_vol_mln', '_circuit', '_avg_vol_float_ratio',
                '_period_perf', '_10wmadist', '_insideday',
                'W_CloseChg_Pct',
            ]

            if 'Sector_Rank' in actionable_df.columns:
                columns_to_show.insert(columns_to_show.index('Symbol') + 1, 'Sector_Rank')
            if 'Sector_Total' in actionable_df.columns:
                columns_to_show.insert(columns_to_show.index('Sector_Rank') + 1, 'Sector_Total')
            if 'Sector_Percentile' in actionable_df.columns:
                columns_to_show.insert(columns_to_show.index('Sector_Total') + 1, 'Sector_Percentile')

            valid_cols = [c for c in columns_to_show if c in actionable_df.columns]
            # ── Sort: Tier A first, then B, then Ignore; within each tier, sorted by tightness (_nr4_previous) ──
            tier_sort_order = {'🟢 A': 0, '🟡 B': 1, '🔴 Ignore': 2, '🔴 Error': 3}
            actionable_df['_tier_sort_key'] = actionable_df['Tier'].map(tier_sort_order).fillna(9)
            display_df = actionable_df[valid_cols].copy()
            display_df['_tier_sort_key'] = actionable_df['_tier_sort_key']

            # Determine which tightness column to sort by
            sort_tightness_col = '_nr4' if scan_mode == "Anticipation" else '_nr4_previous'
            if sort_tightness_col in display_df.columns:
                display_df[sort_tightness_col] = pd.to_numeric(display_df[sort_tightness_col], errors='coerce')
                # Sort by Tier first, then by Tightness (ascending = tightest on top).
                display_df = display_df.sort_values(by=['_tier_sort_key', sort_tightness_col], ascending=[True, True],
                                                    na_position='last')
            else:
                display_df = display_df.sort_values(by=['_tier_sort_key'], ascending=[True])

            display_df = display_df.drop(columns=['_tier_sort_key'], errors='ignore')
            st.session_state.gtt_display_df = display_df


            st.success(f"Generated {len(st.session_state.gtt_display_df)} actionable GTT setups.")


            filtered_df = filter_dataframe(st.session_state.gtt_display_df, scan_mode)

            # ── Persisted column visibility ──
            main_table_default_hidden = [
                'RS_6M', 'RS_3M', 'RS_1M',
                'Industry',
                '_bo_dollar_vol_mln', '_avg_vol_float_ratio', '_period_perf',
                '_10wmadist', '_insideday',
                '_bo_engulfing_cndl', '_days_since_bo', '_circuit',
                'W_CloseChg_Pct',
            ]
            all_main_cols = list(filtered_df.columns)
            with st.expander("🧩 Choose visible columns (saved as your default)"):
                selected_main_cols = st.multiselect(
                    "Columns to show in the table below",
                    options=all_main_cols,
                    default=get_persisted_columns('main_table', all_main_cols, main_table_default_hidden),
                    key="main_table_col_select",
                )
                if st.button("💾 Save as my default column set", key="save_main_cols_btn"):
                    prefs = load_column_prefs()
                    prefs['main_table'] = selected_main_cols
                    save_column_prefs(prefs)
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
                    gb.configure_column(col,minWidth=60, maxWidth=90,
                                        cellStyle=dynamic_jscode)
                else:
                    gb.configure_column(col, minWidth=50, maxWidth=80, cellStyle=dynamic_jscode)

            tightness_highlight_jscode = JsCode(
                """function(params) { return { 'backgroundColor': '#fff3cd', 'color': '#664d03', 'fontWeight': 'bold' }; }""")
            active_tightness_col = '_nr4' if scan_mode == "Anticipation" else '_nr4_previous'
            if active_tightness_col in filtered_df.columns:
                gb.configure_column(active_tightness_col, minWidth=55, maxWidth=75,
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

            # ── 20MADist / 10MADist heatmap ──
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

            # ── Score column styling ──
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

            # ── Weekly column styling ──
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
            if 'W_TightCloses' in filtered_df.columns:
                gb.configure_column('W_TightCloses', headerName='Wk TightCl/4', minWidth=85, maxWidth=105)
            if 'W_InsideBars' in filtered_df.columns:
                gb.configure_column('W_InsideBars', headerName='Wk InsideB/8', minWidth=85, maxWidth=105)

            # ── Header shortening (FIXED: comma not 0) ──
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
            # gb.configure_column('Tier', minWidth=70, maxWidth=85, cellStyle=tier_jscode, pinned='left')
            gb.configure_column('Tier', minWidth=70, maxWidth=85, cellStyle=tier_jscode, pinned='left')
            # ── Change column styling ──


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
                                pinned='left')

            if 'Sector' in filtered_df.columns:
                gb.configure_column('Sector', minWidth=120, maxWidth=150)
            if 'Industry' in filtered_df.columns:
                gb.configure_column('Industry', minWidth=120, maxWidth=150)

            for col in hidden_main_cols:
                gb.configure_column(col, hide=True)


            go = gb.build()
            loud_message_1 = "Confirm volume on 1 hour if it matches previous swing or close"
            loud_message_2 = "SEPA identification is Key for Swing to Work"

            st.markdown(f"""
                            <style>
                            @keyframes pulse-warning {{
                                0% {{ box-shadow: 0 0 0 0 rgba(255, 25, 25, 0.7); }}
                                70% {{ box-shadow: 0 0 0 15px rgba(255, 25, 25, 0); }}
                                100% {{ box-shadow: 0 0 0 0 rgba(255, 25, 25, 0); }}
                            }}

                            .loud-alert {{
                                background-color: #dc143c; 
                                color: #ffffff; 
                                font-size: 20px; 
                                font-weight: 800; 
                                padding: 12px 20px; 
                                border-radius: 8px;
                                text-align: center;
                                border: 3px solid #ffd700; 
                                margin: 15px 0px;
                                text-transform: uppercase; 
                                letter-spacing: 0.5px;
                                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                                animation: pulse-warning 2s infinite; 
                            }}
                            </style>

                            <div class="loud-alert">
                                🚨 {loud_message_1} 🚨<br>
                                🚨 {loud_message_2} 🚨
                            </div>
                        """, unsafe_allow_html=True)


            grid_response = AgGrid(filtered_df, gridOptions=go, height=600, width='100%',
                                   update_mode=GridUpdateMode.MODEL_CHANGED,
                                   data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                                   allow_unsafe_jscode=True)

            # ══════════════════════════════════════════════════════════════════
            # COPY SYMBOL LIST TO TRADINGVIEW
            # FIX: We now use grid_response['data'] which captures the
            # exact sorting and filtering applied by the user inside the
            # AgGrid UI.
            # ══════════════════════════════════════════════════════════════════
            sorted_df = grid_response['data'] if grid_response and 'data' in grid_response and not grid_response['data'].empty else filtered_df

            if not sorted_df.empty and 'Symbol' in sorted_df.columns and 'Tier' in sorted_df.columns:
                all_symbols_sorted = sorted_df['Symbol'].dropna().unique().tolist()

                all_tv_string = ",".join([f"nse:{s}" for s in all_symbols_sorted])

                tier_a_df = sorted_df[sorted_df['Tier'] == '🟢 A']
                tier_a_symbols = tier_a_df['Symbol'].dropna().unique().tolist()
                tier_a_tv_string = ",".join([f"nse:{s}" for s in tier_a_symbols])

                tier_ab_df = sorted_df[sorted_df['Tier'].isin(['🟢 A', '🟡 B'])]
                tier_ab_symbols = tier_ab_df['Symbol'].dropna().unique().tolist()
                tier_ab_tv_string = ",".join([f"nse:{s}" for s in tier_ab_symbols])
                st.markdown("---")
                st.subheader("📋 Copy Symbols to TradingView")

                copy_col1, copy_col2, copy_col3 = st.columns(3)

                with copy_col1:
                    st.markdown(f"**Tier A only** — `{len(tier_a_symbols)} symbols`")
                    if tier_a_symbols:
                        st.code(tier_a_tv_string, language=None)
                        st.caption(f"Click the 📋 icon above to copy {len(tier_a_symbols)} symbols.")
                    else:
                        st.info("No Tier A stocks.")

                with copy_col2:
                    st.markdown(f"**Tier A + B** — `{len(tier_ab_symbols)} symbols`")
                    if tier_ab_symbols:
                        st.code(tier_ab_tv_string, language=None)
                        st.caption(f"Click the 📋 icon above to copy {len(tier_ab_symbols)} symbols.")
                    else:
                        st.info("No Tier A/B stocks.")

                with copy_col3:
                    st.markdown(f"**All filtered** — `{len(all_symbols_sorted)} symbols`")
                    if all_symbols_sorted:
                        if st.button("📋 Copy All", key="copy_all"):
                           st.code(all_tv_string, language=None)
                           st.caption(f"Click the 📋 icon above to copy {len(all_symbols_sorted)} symbols.")
                    else:
                        st.info("No symbols in view.")

        else:
            st.info("Click 'Generate GTT Trading Plan' to load data.")

    # --- TAB 2: MARKET THEMES ---
    # with tab2:
    #     if 'gtt_scored_df' in st.session_state and st.session_state.gtt_scored_df is not None:
    #         scored_df = st.session_state.gtt_scored_df.copy()
    #         if 'Sector' in scored_df.columns:
    #             tier_ab = scored_df[scored_df['Tier'].isin(['🟢 A', '🟡 B'])].copy()
    #             if not tier_ab.empty:
    #                 sector_summary = tier_ab.groupby('Sector').agg(
    #                     Tier_A_Count=('Tier', lambda x: (x == '🟢 A').sum()),
    #                     Tier_B_Count=('Tier', lambda x: (x == '🟡 B').sum()),
    #                     Total_Count=('Symbol', 'count'),
    #                     Avg_RS=('Avg_RS', 'mean'),
    #                     Avg_Total_Score=('Total_Score', 'mean'),
    #                 ).round(2).sort_values('Total_Count', ascending=False)
    #
    #                 st.subheader("Sector Concentration (Tier A + B stocks)")
    #                 st.dataframe(sector_summary, use_container_width=True)
    #
    #                 st.subheader("Top Setups by Sector")
    #                 for sector in sector_summary.head(10).index:
    #                     sector_stocks = tier_ab[tier_ab['Sector'] == sector].sort_values('Total_Score', ascending=False)
    #                     display_cols = [c for c in ['Symbol', 'Tier', 'Total_Score', 'Last',
    #                                                  '_chg_percentclose', 'Avg_RS', 'Adr',
    #                                                  '_nr4_previous', '_20madist', '_10madist',
    #                                                  'W_TightCloses'] if c in sector_stocks.columns]
    #                     with st.expander(f"🏛️ {sector} ({len(sector_stocks)} stocks)"):
    #                         st.dataframe(sector_stocks[display_cols].head(15), use_container_width=True, hide_index=True)
    #             else:
    #                 st.info("No Tier A or B stocks found.")
    #         else:
    #             st.info("No sector data available.")
    #     else:
    #         st.info("Generate data first.")
    # --- TAB 2: MARKET THEMES & LEADERS (with heatmaps, same as main scanner) ---
    with tab2:
        if 'gtt_scored_df' in st.session_state and st.session_state.gtt_scored_df is not None:
            scored_df = st.session_state.gtt_scored_df.copy()
            if 'Sector' in scored_df.columns:
                tier_ab = scored_df[scored_df['Tier'].isin(['🟢 A', '🟡 B'])].copy()
                if not tier_ab.empty:
                    # ── Sector summary aggregation ──
                    sector_summary = tier_ab.groupby('Sector').agg(
                        Tier_A_Count=('Tier', lambda x: (x == '🟢 A').sum()),
                        Tier_B_Count=('Tier', lambda x: (x == '🟡 B').sum()),
                        Total_Count=('Symbol', 'count'),
                        Avg_RS=('Avg_RS', 'mean'),
                        Avg_Total_Score=('Total_Score', 'mean'),
                    ).round(2).sort_values('Total_Count', ascending=False).reset_index()

                    st.subheader("Sector Concentration (Tier A + B stocks)")

                    # ── Build AgGrid for sector summary with RS + Score heatmaps ──
                    ss_gb = GridOptionsBuilder.from_dataframe(sector_summary)
                    ss_gb.configure_default_column(resizable=True, filterable=True, sortable=True, minWidth=70, flex=0)
                    ss_gb.configure_side_bar()
                    ss_gb.configure_grid_options(enableBrowserTooltips=True)
                    for col in sector_summary.columns:
                        ss_gb.configure_column(col, headerTooltip=col)

                    # Avg_RS heatmap (same red→green dynamic gradient as Tab 1)
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

                    # Avg_Total_Score heatmap
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

                    # ── Top setups by sector (one AgGrid per sector, same styling as main scanner) ──
                    st.subheader("Top Setups by Sector")
                    for sector in sector_summary.head(10)['Sector'].tolist():
                        sector_stocks = tier_ab[tier_ab['Sector'] == sector].sort_values(
                            'Total_Score', ascending=False).head(15)
                        display_cols = [c for c in [
                            'Symbol', 'Tier', 'Change', 'Total_Score',
                            'Tight_Score', 'Vol_Score', 'TClose_Score', 'MA20_Score', 'MA10_Score',
                            'Last', '_chg_percentclose', 'Avg_RS', 'RS_6M', 'RS_3M', 'RS_1M',
                            'Adr', 'Ti65', '_nr4', '_nr4_previous',
                            'dvol', '_avgvol_mln', '_20madist', '_10madist',
                            'W_TightCloses', 'W_InsideBars', 'W_PctOf10wkHigh', 'W_CloseChg_Pct',
                            'Sector', 'Industry', 'Sector_Rank', 'Sector_Total', 'Sector_Percentile'
                        ] if c in sector_stocks.columns]
                        sector_display = sector_stocks[display_cols].copy()

                        with st.expander(f"🏛️ {sector} ({len(sector_stocks)} stocks)"):
                            gb = GridOptionsBuilder.from_dataframe(sector_display)
                            gb.configure_default_column(resizable=True, filterable=True, sortable=True, minWidth=70, flex=0)
                            gb.configure_side_bar()
                            gb.configure_grid_options(enableBrowserTooltips=True)
                            for col in sector_display.columns:
                                gb.configure_column(col, headerTooltip=col)

                            # ── RS heatmap (same dynamic gradient as Tab 1) ──
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

                            # ── _nr4_previous yellow highlight ──
                            nr4_prev_highlight_jscode = JsCode(
                                """function(params) { return { 'backgroundColor': '#fff3cd', 'color': '#664d03', 'fontWeight': 'bold' }; }""")
                            if '_nr4_previous' in sector_display.columns:
                                gb.configure_column('_nr4_previous', minWidth=55, maxWidth=75, cellStyle=nr4_prev_highlight_jscode)

                            # ── _chg_percentclose pink→purple gradient ──
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
                                gb.configure_column('_chg_percentclose', minWidth=80, maxWidth=110, cellStyle=chg_jscode,
                                                    filter='agNumberColumnFilter',
                                                    filterParams={'filterOptions': ['greaterThan', 'lessThan', 'equals', 'inRange'],
                                                                  'defaultOption': 'greaterThan', 'defaultValues': [0]})

                            for col in ['Adr', 'Ti65', '_nr4']:
                                if col in sector_display.columns:
                                    gb.configure_column(col, minWidth=55, maxWidth=75)

                            # ── dvol / _avgvol_mln rvol heatmap ──
                            if 'dvol' in sector_display.columns and '_avgvol_mln' in sector_display.columns:
                                valid_rvol = sector_display[(sector_display['dvol'] > 0) & (sector_display['_avgvol_mln'] > 0)].copy()
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

                            # ── 20MADist / 10MADist heatmap ──
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

                            # ── Score column styling (green tiers) ──
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
                                if sc_col in sector_display.columns:
                                    gb.configure_column(sc_col, minWidth=45, maxWidth=60, cellStyle=score_col_style)

                            # ── Weekly column styling ──
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
                                gb.configure_column('W_PctOf10wkHigh', headerName='Wk % of 10wHi', minWidth=95, maxWidth=120, cellStyle=wk_pct_jscode)
                            if 'W_CloseChg_Pct' in sector_display.columns:
                                gb.configure_column('W_CloseChg_Pct', headerName='Wk CloseChg%', minWidth=90, maxWidth=115)
                            if 'W_TightCloses' in sector_display.columns:
                                gb.configure_column('W_TightCloses', headerName='Wk TightCl/4', minWidth=85, maxWidth=105)
                            if 'W_InsideBars' in sector_display.columns:
                                gb.configure_column('W_InsideBars', headerName='Wk InsideB/8', minWidth=85, maxWidth=105)

                            # ── Header shortening ──
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

                            # ── Tier styling ──
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
                                gb.configure_column('Tier', minWidth=70, maxWidth=85, cellStyle=tier_jscode, pinned='left')

                            # ── Change column styling ──
                            if 'Change' in sector_display.columns:
                                change_cell_jscode = JsCode("""
                                    function(params) {
                                        if (!params.value) return null;
                                        if (params.value === '🆕') return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                                        if (params.value === '⬆️') return { 'backgroundColor': '#17a2b8', 'color': 'white', 'fontWeight': 'bold' };
                                        if (params.value === '📈') return { 'backgroundColor': '#d4edda', 'color': '#155724' };
                                        if (params.value === '⬇️') return { 'backgroundColor': '#ffc107', 'color': '#856404', 'fontWeight': 'bold' };
                                        if (params.value === '📉') return { 'backgroundColor': '#fff3cd', 'color': '#856404' };
                                        return null;
                                    }
                                """)
                                gb.configure_column('Change', minWidth=50, maxWidth=60, cellStyle=change_cell_jscode, headerName='Chg')

                            if 'Total_Score' in sector_display.columns:
                                gb.configure_column('Total_Score', minWidth=55, maxWidth=70)

                            # ── Symbol renderer with (rank/total) ──
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
                                gb.configure_column('Symbol', cellRenderer=symbol_renderer_jscode, minWidth=150, maxWidth=180, pinned='left')

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

                            # ── Row-level highlight for new/upgraded stocks ──
                            row_style_jscode = JsCode("""
                                function(params) {
                                    if (!params.data) return null;
                                    const change = params.data.Change;
                                    if (change === '🆕') return { 'backgroundColor': '#e8f5e9' };
                                    if (change === '⬆️') return { 'backgroundColor': '#e1f5fe' };
                                    return null;
                                }
                            """)
                            gb.configure_grid_options(getRowStyle=row_style_jscode)

                            go = gb.build()
                            AgGrid(sector_display, gridOptions=go, height=400, width='100%',
                                   update_mode=GridUpdateMode.MODEL_CHANGED,
                                   data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                                   allow_unsafe_jscode=True)
                else:
                    st.info("No Tier A or B stocks found.")
            else:
                st.info("No sector data available.")
        else:
            st.info("Generate data first.")
    # --- TAB 3: WEEKLY BASE WATCH ---
    with tab3:
        if 'weekly_full_df' in st.session_state and st.session_state.weekly_full_df is not None:
            weekly_df = st.session_state.weekly_full_df.copy()
            st.subheader("Weekly Base Formation Watch")
            st.markdown("Stocks with tight weekly bases independent of daily RS filter.")

            w_col1, w_col2 = st.columns(2)
            with w_col1:
                min_tightcloses = st.slider("Min TightCloses_of4", 0, 4, 1, key="wc_tight")
            with w_col2:
                min_pct_10wk = st.slider("Min PctOf10wkHigh", 0.0, 1.0, 0.85, step=0.05, key="wc_pct")

            if 'Tightcloses_of4' in weekly_df.columns:
                weekly_df = weekly_df[weekly_df['Tightcloses_of4'].fillna(0) >= min_tightcloses]
            if 'Pctof10wkhigh' in weekly_df.columns:
                weekly_df = weekly_df[weekly_df['Pctof10wkhigh'].fillna(0) >= min_pct_10wk]

            st.dataframe(weekly_df, use_container_width=True, hide_index=True)
        else:
            st.info("Generate data first.")

if __name__ == "__main__":
    main()