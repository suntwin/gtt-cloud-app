import streamlit as st
import pandas as pd
import numpy as np
import requests
from io import StringIO
import sys
import os
import json
import time
from datetime import datetime
from pandas.api.types import (
    is_datetime64_any_dtype,
    is_numeric_dtype,
    is_object_dtype,
)

# ── Patch for streamlit-aggrid + Streamlit 1.28+ compatibility ──
import streamlit.components.v1 as _components

if not hasattr(_components, 'MarshallComponentException'):
    _components.MarshallComponentException = Exception

from st_aggrid import AgGrid, GridOptionsBuilder, JsCode, GridUpdateMode, DataReturnMode

st.set_page_config(page_title="GTT Crypto Hourly Scanner", page_icon="⚡", layout="wide")

# ════════════════════════════════════════════════════════════════════
# 1. CONFIGURATION & ENDPOINTS
# ════════════════════════════════════════════════════════════════════
gtt_endpoints = {
    "22h": "https://api.marketinout.com/run/screen?key=9fdcdedbcee248d8",
    "68h": "https://api.marketinout.com/run/screen?key=30ae06ca7155498e",
    "126h": "https://api.marketinout.com/run/screen?key=e42d24a9aabc4a7e",
}

weekly_daily_endpoint = "https://api.marketinout.com/run/screen?key=9a2a66e365b7429c"

weekly_daily_metric_columns = [
    'Eema20', 'Eema10', 'Dist_dema10_pct', 'Dist_dema20_pct',
    'Dailyclose_chg_pct', 'Dailytightcloses_of4',
    'Insidebar_thiswk', 'Insidebar_thisday', 'Insidebars_of8',
    'Dailycontraction', 'Weeklyrsi', 'Dailyrsi'
]

gtt_columns = [
    'Symbol', 'Last', 'Timestamp',
    '_chg_percentclose_hourly', '_avgvol_mln_hourly', 'Adr', 'Ti65',
    '_10madist', '_20madist', '_period_perf', '_wtc',
    '_nr4', '_nr4_previous', 'strongopen', 'dvolhourly', 'dvoldaily'
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COLUMN_PREFS_FILE = os.path.join(BASE_DIR, "crypto_column_prefs.json")
SCORING_PREFS_FILE = os.path.join(BASE_DIR, "crypto_scoring_prefs.json")


# ════════════════════════════════════════════════════════════════════
# 2. PERSISTENCE & HELPER FUNCTIONS
# ════════════════════════════════════════════════════════════════════
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


def _is_categorical(series):
    """Safe categorical check that works across pandas versions."""
    try:
        from pandas.api.types import is_categorical_dtype
        return is_categorical_dtype(series)
    except (ImportError, AttributeError, TypeError):
        return isinstance(series.dtype, pd.CategoricalDtype)


def clean_df_for_json(df):
    """Make DataFrame JSON-safe for streamlit-aggrid."""
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


# ════════════════════════════════════════════════════════════════════
# 3. SCAN FETCHERS
# ════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=300)
def fetch_gtt_scan(url, name):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200 or not response.text.strip():
            return None
        df = pd.read_csv(StringIO(response.text), sep='|', header=None)

        if len(df.columns) < len(gtt_columns):
            df.columns = gtt_columns[:len(df.columns)]
        else:
            df.columns = gtt_columns + [f'Extra_{i}' for i in range(len(gtt_columns), len(df.columns))]

        df['Symbol'] = df['Symbol'].astype(str).str.upper().str.strip()

        numeric_fillna = [
            'Last', '_nr4', '_nr4_previous', 'Adr', 'Ti65',
            'dvolhourly', 'dvoldaily', '_avgvol_mln_hourly', 'strongopen'
        ]
        for col in numeric_fillna:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        numeric_keep_nan = ['_20madist', '_10madist', '_period_perf', '_wtc',
                            '_chg_percentclose_hourly']
        for col in numeric_keep_nan:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        return df
    except Exception as e:
        st.error(f"Error fetching {name} scan: {str(e)}")
        return None


@st.cache_data(ttl=300)
def fetch_weekly_daily_scan(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200 or not response.text.strip():
            return None

        n_metrics = len(weekly_daily_metric_columns)
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

        df = pd.DataFrame(rows, columns=['Symbol', 'Last'] + weekly_daily_metric_columns)
        df['Symbol'] = df['Symbol'].astype(str).str.upper().str.strip()

        for col in ['Last'] + weekly_daily_metric_columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        return df
    except Exception as e:
        st.error(f"Error fetching Weekly/Daily scan: {str(e)}")
        return None


# ════════════════════════════════════════════════════════════════════
# 4. FILTER WIDGET
# ════════════════════════════════════════════════════════════════════
def filter_dataframe(df: pd.DataFrame, scan_mode: str) -> pd.DataFrame:
    modify = st.checkbox("Add Advanced Filters")
    if not modify:
        mask = pd.Series(True, index=df.index)
        if '_chg_percentclose_hourly' in df.columns and scan_mode == "Post Breakout":
            mask = mask & (df['_chg_percentclose_hourly'].fillna(0) > 0)
        if 'Adr' in df.columns and scan_mode == "Post Breakout":
            mask = mask & (df['Adr'].fillna(0) > 3)
        if 'Ti65' in df.columns and scan_mode == "Post Breakout":
            mask = mask & (df['Ti65'].fillna(0) > 1.05)
        if 'Avg_Perf' in df.columns and scan_mode == "Post Breakout":
            mask = mask & (df['Avg_Perf'].fillna(0) > 0)
        if '_avgvol_mln_hourly' in df.columns and scan_mode == "Post Breakout":
            mask = mask & (df['_avgvol_mln_hourly'].fillna(0) > 0.5)
        return df[mask].copy()

    df = df.copy()
    with st.container():
        default_filt = (['_chg_percentclose_hourly', 'Adr', 'Ti65', 'Avg_Perf', '_avgvol_mln_hourly']
                        if scan_mode == "Post Breakout"
                        else ['_nr4_previous'])
        to_filter_columns = st.multiselect("Filter dataframe on", df.columns, default=default_filt)

        for column in to_filter_columns:
            left, right = st.columns((1, 20))
            left.write("↳")
            col_series = df[column]
            has_nans = col_series.isna().any()

            if _is_categorical(col_series) or col_series.dropna().nunique() < 10:
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
                _min = float(clean.min());
                _max = float(clean.max())
                step = (_max - _min) / 100 if (_max - _min) > 0 else 1
                custom_defaults = {
                    '_chg_percentclose_hourly': 1.0, 'Adr': 3.0, 'Ti65': 1.05,
                    'Avg_Perf': 0.0, '_avgvol_mln_hourly': 0.5,
                }
                desired_min = custom_defaults.get(column, _min)
                default_min = max(desired_min, _min)
                user_num_input = right.slider(
                    f"Values for {column}", _min, _max, (default_min, _max), step=step
                )
                if has_nans:
                    keep_nans = right.checkbox(
                        f"Keep rows where **{column}** is blank",
                        value=True, key=f"keep_nan_{column}"
                    )
                else:
                    keep_nans = False
                in_range = col_series.between(*user_num_input)
                mask = (in_range | col_series.isna()) if keep_nans else in_range
                df = df[mask]
            else:
                user_text_input = right.text_input(f"Substring or regex in {column}")
                if user_text_input:
                    text_mask = col_series.astype(str).str.contains(
                        user_text_input, case=False, na=False
                    )
                    df = df[text_mask | col_series.isna()]
    return df


# ════════════════════════════════════════════════════════════════════
# 5. MAIN APPLICATION
# ════════════════════════════════════════════════════════════════════
def main():
    st.title("⚡ GTT Crypto Hourly Scanner")

    # ── Auto-refresh toggle ──
    st.sidebar.markdown("---")
    auto_refresh = st.sidebar.checkbox("🔄 Auto-refresh every 10 min", value=False, key="auto_refresh_toggle")
    refresh_clicked = st.sidebar.button("🔁 Refresh Now", key="manual_refresh_btn")

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
        st.sidebar.caption(f"🕐 Last refreshed: {last_refresh_dt.strftime('%H:%M:%S')}")
        mins, secs = divmod(remaining, 60)
        countdown_html = f"""
        <div style="font-size:13px;color:#888;padding:2px 0;font-family:'Source Sans Pro',sans-serif;">
            ⏱️ Next refresh in <span id="cd-m">{mins}</span>m <span id="cd-s">{secs:02d}</span>s
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

    scan_mode = st.radio(
        "Select Scanner Mode",
        ("Anticipation", "Post Breakout"), horizontal=True
    )
    if scan_mode == "Post Breakout":
        st.markdown("Hourly crypto scanner for confirmed breakouts (Boom Boom / 1-2-3 / Coiled Spring).")
    else:
        st.markdown("Anticipation scanner for coiled hourly setups as they break. ⚠️ Wait for volume confirmation.")

    # ════════════════════════════════════════════════════════════════
    # SCORING CONFIG (Sidebar)
    # ════════════════════════════════════════════════════════════════
    st.sidebar.header("⚙️ Scoring System Config")
    saved_scoring = load_scoring_prefs()

    # 1: Tightness
    st.sidebar.subheader("1️⃣ Tightness (_nr4_prev) — Max 4 pts")
    tight_defaults = saved_scoring.get('tightness_thresholds', [4.0, 6.0, 8.0, 10.0])
    t_raw = [
        st.sidebar.number_input("_nr4_prev < this → 4 pts", value=tight_defaults[0], step=0.5, key="sc_t1"),
        st.sidebar.number_input("_nr4_prev < this → 3 pts", value=tight_defaults[1], step=0.5, key="sc_t2"),
        st.sidebar.number_input("_nr4_prev < this → 2 pts", value=tight_defaults[2], step=0.5, key="sc_t3"),
        st.sidebar.number_input("_nr4_prev < this → 1 pt", value=tight_defaults[3], step=0.5, key="sc_t4"),
    ]
    t1, t2, t3, t4 = sorted(t_raw)

    # 2: BO Volume (hourly)
    st.sidebar.subheader("2️⃣ BO Volume (dvolhourly/avgvol_hourly) — Max 3 pts")
    vol_defaults = saved_scoring.get('vol_thresholds', [3.0, 2.0, 1.5])
    v_raw = [
        st.sidebar.number_input("dvol/avg > this → 3 pts", value=vol_defaults[0], step=0.5, key="sc_v1"),
        st.sidebar.number_input("dvol/avg > this → 2 pts", value=vol_defaults[1], step=0.5, key="sc_v2"),
        st.sidebar.number_input("dvol/avg > this → 1 pt", value=vol_defaults[2], step=0.5, key="sc_v3"),
    ]
    v3, v2, v1 = sorted(v_raw)

    # 3: DailyTightCloses Bonus
    st.sidebar.subheader("3️⃣ DailyTightCloses Bonus — Brownie Pts")
    tclose_pts = st.sidebar.number_input(
        "Points if DailyTightCloses_of4 ≥ 1",
        value=saved_scoring.get('tclose_bonus_pts', 2),
        min_value=0, max_value=5, step=1, key="sc_tclose"
    )

    # 3b: StrongOpen Bonus
    st.sidebar.subheader("3b️⃣ StrongOpen Bonus — Brownie Pts")
    strongopen_pts = st.sidebar.number_input(
        "Points if strongopen == 1",
        value=saved_scoring.get('strongopen_bonus_pts', 1),
        min_value=0, max_value=5, step=1, key="sc_strongopen"
    )

    # 3c: WTC Bonus
    st.sidebar.subheader("3c️⃣ _wtc Bonus — Brownie Pts")
    wtc_pts = st.sidebar.number_input(
        "Points if _wtc == 1",
        value=saved_scoring.get('wtc_bonus_pts', 1),
        min_value=0, max_value=5, step=1, key="sc_wtc"
    )

    # 4: 20MADist
    st.sidebar.subheader("4️⃣ 20MADist — Max 3 pts")
    ma20_defaults = saved_scoring.get('ma20_tiers', [2.0, 4.0, 6.0])
    ma20_neg_cutoff = st.sidebar.number_input(
        "Avoid if 20MADist below this %",
        value=saved_scoring.get('ma20_neg_cutoff', -6.0), step=0.5, key="sc_ma20_neg"
    )
    ma20_raw = [
        st.sidebar.number_input("abs(20MADist) < this → 3 pts", value=ma20_defaults[0], step=0.5, key="sc_ma20_1"),
        st.sidebar.number_input("abs(20MADist) < this → 2 pts", value=ma20_defaults[1], step=0.5, key="sc_ma20_2"),
        st.sidebar.number_input("abs(20MADist) < this → 1 pt", value=ma20_defaults[2], step=0.5, key="sc_ma20_3"),
    ]
    ma20_t1, ma20_t2, ma20_t3 = sorted(ma20_raw)

    # 5: 10MADist
    st.sidebar.subheader("5️⃣ 10MADist — Max 2 pts")
    ma10_defaults = saved_scoring.get('ma10_tiers', [4.0, 6.0])
    ma10_neg_cutoff = st.sidebar.number_input(
        "Avoid if 10MADist below this %",
        value=saved_scoring.get('ma10_neg_cutoff', -6.0), step=0.5, key="sc_ma10_neg"
    )
    ma10_raw = [
        st.sidebar.number_input("abs(10MADist) < this → 2 pts", value=ma10_defaults[0], step=0.5, key="sc_ma10_1"),
        st.sidebar.number_input("abs(10MADist) < this → 1 pt", value=ma10_defaults[1], step=0.5, key="sc_ma10_2"),
    ]
    ma10_t1, ma10_t2 = sorted(ma10_raw)

    # Tier thresholds
    st.sidebar.subheader("🏷️ Tier Thresholds")
    tier_a = st.sidebar.number_input("Tier A (🟢) min score",
                                     value=saved_scoring.get('tier_a_threshold', 10),
                                     min_value=1, max_value=14, step=1, key="sc_tier_a")
    tier_b = st.sidebar.number_input("Tier B (🟡) min score",
                                     value=saved_scoring.get('tier_b_threshold', 7),
                                     min_value=1, max_value=14, step=1, key="sc_tier_b")

    if st.sidebar.button("💾 Save scoring config", key="save_scoring_btn"):
        prefs_to_save = {
            'tightness_thresholds': t_raw,
            'vol_thresholds': v_raw,
            'tclose_bonus_pts': int(tclose_pts),
            'strongopen_bonus_pts': int(strongopen_pts),
            'wtc_bonus_pts': int(wtc_pts),
            'ma20_tiers': ma20_raw,
            'ma20_neg_cutoff': ma20_neg_cutoff,
            'ma10_tiers': ma10_raw,
            'ma10_neg_cutoff': ma10_neg_cutoff,
            'tier_a_threshold': int(tier_a),
            'tier_b_threshold': int(tier_b),
        }
        save_scoring_prefs(prefs_to_save)
        st.sidebar.success("✅ Saved! Will load by default next session.")

    # ── Determine if we should fetch ──
    manual_fetch = st.button("Generate GTT Crypto Plan", type="primary")
    auto_fetch = auto_refresh and ('gtt_base_df' in st.session_state)
    should_fetch = manual_fetch or auto_fetch or refresh_clicked

    if should_fetch:
        fetch_label = ("Auto-refreshing scans..." if auto_fetch and not manual_fetch
                       else "Fetching & merging 22h / 68h / 126h crypto scans...")
        with st.spinner(fetch_label):
            df_22h = fetch_gtt_scan(gtt_endpoints["22h"], "22h")
            df_68h = fetch_gtt_scan(gtt_endpoints["68h"], "68h")
            df_126h = fetch_gtt_scan(gtt_endpoints["126h"], "126h")

            if df_22h is not None and not df_22h.empty:
                df_22h_r = df_22h.rename(columns={'_period_perf': 'Perf_22h'})
                df_68h_r = (df_68h.rename(columns={'_period_perf': 'Perf_68h'})
                            if df_68h is not None and not df_68h.empty else None)
                df_126h_r = (df_126h.rename(columns={'_period_perf': 'Perf_126h'})
                             if df_126h is not None and not df_126h.empty else None)

                non_perf_cols = [c for c in df_22h_r.columns
                                 if c not in ['Symbol', 'Perf_22h', 'Perf_68h', 'Perf_126h']]
                base_df = df_22h_r.copy()

                if df_68h_r is not None:
                    base_df = base_df.merge(df_68h_r, on='Symbol', how='outer', suffixes=('', '_68h'))
                    for col in non_perf_cols:
                        col_68h = f'{col}_68h'
                        if col_68h in base_df.columns:
                            base_df[col] = base_df[col].fillna(base_df[col_68h])
                            base_df.drop(col_68h, axis=1, inplace=True)
                else:
                    base_df['Perf_68h'] = np.nan

                if df_126h_r is not None:
                    base_df = base_df.merge(df_126h_r, on='Symbol', how='outer', suffixes=('', '_126h'))
                    for col in non_perf_cols:
                        col_126h = f'{col}_126h'
                        if col_126h in base_df.columns:
                            base_df[col] = base_df[col].fillna(base_df[col_126h])
                            base_df.drop(col_126h, axis=1, inplace=True)
                else:
                    base_df['Perf_126h'] = np.nan

                base_df['Perf_22h'] = base_df['Perf_22h'].fillna(0) if 'Perf_22h' in base_df.columns else 0
                base_df['Perf_68h'] = base_df['Perf_68h'].fillna(0) if 'Perf_68h' in base_df.columns else 0
                base_df['Perf_126h'] = base_df['Perf_126h'].fillna(0) if 'Perf_126h' in base_df.columns else 0

                perf_cols = ['Perf_22h', 'Perf_68h', 'Perf_126h']
                base_df['Avg_Perf'] = (base_df[perf_cols]
                                       .replace(0, np.nan)
                                       .mean(axis=1)
                                       .fillna(0)
                                       .round(2))

                for col in ['Perf_22h', 'Perf_68h', 'Perf_126h', 'Adr', 'Ti65', '_nr4']:
                    if col in base_df.columns:
                        base_df[col] = base_df[col].round(2)
                for col in ['dvolhourly', 'dvoldaily', '_avgvol_mln_hourly']:
                    if col in base_df.columns:
                        base_df[col] = base_df[col].round(2)

                actionable_df = base_df

                # ── Merge weekly/daily scan ──
                wd_df = fetch_weekly_daily_scan(weekly_daily_endpoint)
                if wd_df is not None and not wd_df.empty:
                    st.session_state.wd_full_df = wd_df.copy()

                    # Safely select available columns to prevent KeyError
                    wd_cols_to_keep = ['Symbol', 'Dailyclose_chg_pct', 'Dailytightcloses_of4',
                                       'Insidebar_thisday', 'Insidebars_of8',
                                       'Weeklyrsi', 'Dailyrsi', 'Dailycontraction']
                    available_wd_cols = [c for c in wd_cols_to_keep if c in wd_df.columns]

                    wd_subset = wd_df[available_wd_cols].rename(columns={
                        'Dailyclose_chg_pct': 'D_CloseChg_Pct',
                        'Dailytightcloses_of4': 'D_TightCloses',
                        'Insidebar_thisday': 'D_InsideBar',
                        'Insidebars_of8': 'D_InsideBars8',
                        'Weeklyrsi': 'W_RSI',
                        'Dailyrsi': 'D_RSI',
                        'Dailycontraction': 'D_Contraction',
                    })
                    actionable_df = actionable_df.merge(wd_subset, on='Symbol', how='left')
                else:
                    st.session_state.wd_full_df = None
                    st.warning("Weekly/Daily scan unavailable — table will show without D_/W_ columns.")

                st.session_state.gtt_base_df = actionable_df
            else:
                st.error("Failed to retrieve base 22h scan data.")
                st.session_state.gtt_base_df = None

    # ─── TABS ───
    tab1, tab2 = st.tabs(["🎯 GTT Crypto Scanner", "📅 Weekly/Daily Watch"])

    # ════════════════════════════════════════════════════════════════
    # TAB 1 — GTT SCANNER
    # ════════════════════════════════════════════════════════════════
    with tab1:
        if 'gtt_base_df' in st.session_state and st.session_state.gtt_base_df is not None:
            actionable_df = st.session_state.gtt_base_df.copy()

            # ── Scoring (NaN → 0 pts always) ──
            thresholds_ok = (
                    len(set([t1, t2, t3, t4])) >= 4 and
                    len(set([v1, v2, v3])) >= 3 and
                    len(set([ma20_t1, ma20_t2, ma20_t3])) >= 3 and
                    len(set([ma10_t1, ma10_t2])) >= 2
            )

            if not thresholds_ok:
                st.sidebar.error("⚠️ Scoring Error: Threshold values within a criteria must be unique.")
                for c in ['Tier', 'Total_Score', 'Tight_Score', 'Vol_Score',
                          'TClose_Score', 'StrongOpen_Score', 'WTC_Score',
                          'MA20_Score', 'MA10_Score']:
                    actionable_df[c] = 0 if c != 'Tier' else '🔴 Error'
            else:
                # 1) Tightness
                if '_nr4_previous' in actionable_df.columns:
                    nr4_prev_filled = actionable_df['_nr4_previous'].fillna(999)
                    actionable_df['Tight_Score'] = pd.cut(
                        nr4_prev_filled,
                        bins=[-float('inf'), t1, t2, t3, t4, float('inf')],
                        labels=[4, 3, 2, 1, 0]
                    ).astype(int)
                else:
                    actionable_df['Tight_Score'] = 0

                # 2) BO Volume (hourly)
                if 'dvolhourly' in actionable_df.columns and '_avgvol_mln_hourly' in actionable_df.columns:
                    rvol_ratio = np.where(
                        actionable_df['_avgvol_mln_hourly'] > 0,
                        actionable_df['dvolhourly'] / actionable_df['_avgvol_mln_hourly'],
                        0
                    )
                    actionable_df['Vol_Score'] = pd.cut(
                        rvol_ratio,
                        bins=[-float('inf'), v3, v2, v1, float('inf')],
                        labels=[0, 1, 2, 3]
                    ).astype(int)
                else:
                    actionable_df['Vol_Score'] = 0

                # 3) DailyTightCloses Bonus
                actionable_df['TClose_Score'] = np.where(
                    actionable_df.get('D_TightCloses', pd.Series(0, index=actionable_df.index)).fillna(0) >= 1,
                    tclose_pts, 0
                )

                # 3b) StrongOpen Bonus
                if 'strongopen' in actionable_df.columns:
                    actionable_df['StrongOpen_Score'] = np.where(
                        actionable_df['strongopen'].fillna(0) >= 1,
                        strongopen_pts, 0
                    )
                else:
                    actionable_df['StrongOpen_Score'] = 0

                # 3c) _wtc Bonus
                if '_wtc' in actionable_df.columns:
                    actionable_df['WTC_Score'] = np.where(
                        actionable_df['_wtc'].fillna(0) >= 1,
                        wtc_pts, 0
                    )
                else:
                    actionable_df['WTC_Score'] = 0

                # 4a) 20MADist
                if '_20madist' in actionable_df.columns:
                    ma20_filled = actionable_df['_20madist'].fillna(999)
                    ma20_abs = ma20_filled.abs()
                    ma20_base_score = pd.cut(
                        ma20_abs,
                        bins=[-float('inf'), ma20_t1, ma20_t2, ma20_t3, float('inf')],
                        labels=[3, 2, 1, 0]
                    ).astype(int)
                    ma20_is_invalid = (actionable_df['_20madist'].isna() |
                                       (actionable_df['_20madist'] < ma20_neg_cutoff))
                    actionable_df['MA20_Score'] = np.where(ma20_is_invalid, 0, ma20_base_score)
                else:
                    actionable_df['MA20_Score'] = 0

                # 4b) 10MADist
                if '_10madist' in actionable_df.columns:
                    ma10_filled = actionable_df['_10madist'].fillna(999)
                    ma10_abs = ma10_filled.abs()
                    ma10_base_score = pd.cut(
                        ma10_abs,
                        bins=[-float('inf'), ma10_t1, ma10_t2, float('inf')],
                        labels=[2, 1, 0]
                    ).astype(int)
                    ma10_is_invalid = (actionable_df['_10madist'].isna() |
                                       (actionable_df['_10madist'] < ma10_neg_cutoff))
                    actionable_df['MA10_Score'] = np.where(ma10_is_invalid, 0, ma10_base_score)
                else:
                    actionable_df['MA10_Score'] = 0

                # ── Total ──
                actionable_df['Total_Score'] = (
                        actionable_df['Tight_Score'] +
                        actionable_df['Vol_Score'] +
                        actionable_df['TClose_Score'] +
                        actionable_df['StrongOpen_Score'] +
                        actionable_df['WTC_Score'] +
                        actionable_df['MA20_Score'] +
                        actionable_df['MA10_Score']
                )

                # ── Tier ──
                conditions = [
                    actionable_df['Total_Score'] >= tier_a,
                    actionable_df['Total_Score'] >= tier_b,
                ]
                choices = ['🟢 A', '🟡 B']
                actionable_df['Tier'] = np.select(conditions, choices, default='🔴 Ignore')

            # ── Change detection ──
            tier_order_map = {'🟢 A': 0, '🟡 B': 1, '🔴 Ignore': 2, '🔴 Error': 3}
            if 'prev_scan_data' in st.session_state and st.session_state.prev_scan_data is not None:
                prev = st.session_state.prev_scan_data
                actionable_df['Change'] = ''
                for idx, row in actionable_df.iterrows():
                    symbol = row['Symbol']
                    cur_tier, cur_score = row['Tier'], row['Total_Score']
                    if symbol not in prev:
                        actionable_df.at[idx, 'Change'] = '🆕'
                    else:
                        prev_tier, prev_score = prev[symbol]['tier'], prev[symbol]['score']
                        cur_rank = tier_order_map.get(cur_tier, 9)
                        prev_rank = tier_order_map.get(prev_tier, 9)
                        if cur_rank < prev_rank:
                            actionable_df.at[idx, 'Change'] = '⬆️'
                        elif cur_rank > prev_rank:
                            actionable_df.at[idx, 'Change'] = '⬇️'
                        elif cur_score > prev_score:
                            actionable_df.at[idx, 'Change'] = '📈'
                        elif cur_score < prev_score:
                            actionable_df.at[idx, 'Change'] = '📉'
                st.session_state.dropped_symbols = set(prev.keys()) - set(actionable_df['Symbol'])
            else:
                actionable_df['Change'] = ''
                st.session_state.dropped_symbols = set()

            st.session_state.prev_scan_data = {
                row['Symbol']: {'tier': row['Tier'], 'score': int(row['Total_Score'])}
                for _, row in actionable_df.iterrows()
            }

            # Move Tier to front
            cols = list(actionable_df.columns)
            cols.insert(0, cols.pop(cols.index('Tier')))
            actionable_df = actionable_df[cols]
            st.session_state.gtt_scored_df = actionable_df.copy()

            columns_to_show = [
                'Tier', 'Change', 'Total_Score',
                'Tight_Score', 'Vol_Score', 'TClose_Score',
                'StrongOpen_Score', 'WTC_Score',
                'MA20_Score', 'MA10_Score',
                '_nr4_previous', '_chg_percentclose_hourly', 'D_TightCloses', 'D_InsideBars8', 'D_InsideBar',
                'dvolhourly', '_avgvol_mln_hourly', 'dvoldaily',
                '_20madist', '_10madist', '_wtc', 'strongopen',
                'Symbol',
                'Avg_Perf', 'Perf_126h', 'Perf_68h', 'Perf_22h',
                'Adr', 'Ti65', '_nr4',
                'D_CloseChg_Pct', 'W_RSI', 'D_RSI', 'D_Contraction',
            ]
            valid_cols = [c for c in columns_to_show if c in actionable_df.columns]

            tier_sort_order = {'🟢 A': 0, '🟡 B': 1, '🔴 Ignore': 2, '🔴 Error': 3}
            actionable_df['_tier_sort_key'] = actionable_df['Tier'].map(tier_sort_order).fillna(9)
            display_df = actionable_df[valid_cols].copy()
            display_df['_tier_sort_key'] = actionable_df['_tier_sort_key']
            display_df = display_df.sort_values(by=['_tier_sort_key', 'Total_Score'],
                                                ascending=[True, False])
            display_df = display_df.drop(columns=['_tier_sort_key'], errors='ignore')
            st.session_state.gtt_display_df = display_df

            st.success(f"Generated {len(display_df)} actionable GTT crypto setups.")

            # ── Change summary bar ──
            change_col = actionable_df['Change']
            n_new = (change_col == '🆕').sum()
            n_tier_up = (change_col == '⬆️').sum()
            n_tier_down = (change_col == '⬇️').sum()
            n_score_up = (change_col == '📈').sum()
            n_score_dn = (change_col == '📉').sum()
            n_dropped = len(st.session_state.get('dropped_symbols', set()))
            if n_new + n_tier_up + n_tier_down + n_score_up + n_score_dn + n_dropped > 0:
                parts = []
                if n_new:       parts.append(f"🆕 **{n_new}** new")
                if n_tier_up:   parts.append(f"⬆️ **{n_tier_up}** tier up")
                if n_tier_down: parts.append(f"⬇️ **{n_tier_down}** tier down")
                if n_score_up:  parts.append(f"📈 **{n_score_up}** score up")
                if n_score_dn:  parts.append(f"📉 **{n_score_dn}** score down")
                if n_dropped:   parts.append(f"❌ **{n_dropped}** dropped out")
                st.info("Changes since last scan: " + " | ".join(parts))

            filtered_df = filter_dataframe(st.session_state.gtt_display_df, scan_mode)

            # ── Persisted column visibility ──
            main_table_default_hidden = [
                'Perf_22h', 'Perf_68h', 'Perf_126h',
                'D_InsideBars8', 'D_InsideBar', 'D_Contraction',
                'W_RSI', 'D_RSI', 'D_CloseChg_Pct',
                'dvoldaily', '_wtc', 'strongopen',
                'MA10_Score', 'WTC_Score', 'StrongOpen_Score',
            ]
            all_main_cols = list(filtered_df.columns)
            with st.expander("🧩 Choose visible columns (saved as your default)"):
                selected_main_cols = st.multiselect(
                    "Columns to show in the table below",
                    options=all_main_cols,
                    default=get_persisted_columns('crypto_main_table', all_main_cols, main_table_default_hidden),
                    key="main_table_col_select",
                )
                if st.button("💾 Save as my default column set", key="save_main_cols_btn"):
                    prefs = load_column_prefs()
                    prefs['crypto_main_table'] = selected_main_cols
                    save_column_prefs(prefs)
                    st.success("Saved — this set will load by default next time.")
            hidden_main_cols = [c for c in all_main_cols if c not in selected_main_cols]
            col_precedence = {c: i for i, c in enumerate(columns_to_show) if c in filtered_df.columns}
            selected_main_cols = sorted(selected_main_cols, key=lambda c: col_precedence.get(c, 9999))
            hidden_main_cols = sorted(hidden_main_cols, key=lambda c: col_precedence.get(c, 9999))
            filtered_df = filtered_df[selected_main_cols + hidden_main_cols]

            # ════════════════════════════════════════════════════════
            # AgGrid — build options from ORIGINAL df (keeps dtypes),
            # then pass CLEANED df for JSON-safe serialization
            # ════════════════════════════════════════════════════════
            gb = GridOptionsBuilder.from_dataframe(filtered_df)
            gb.configure_default_column(resizable=True, filterable=True, sortable=True, minWidth=70, flex=0)
            gb.configure_side_bar()
            gb.configure_grid_options(enableBrowserTooltips=True)
            for col in filtered_df.columns:
                gb.configure_column(col, headerTooltip=col)

            # Perf heatmap
            for col in ['Avg_Perf', 'Perf_126h', 'Perf_68h', 'Perf_22h']:
                if col not in filtered_df.columns:
                    continue
                valid_data = filtered_df[filtered_df[col] > 0][col]
                col_min = float(valid_data.min()) if not valid_data.empty else 0.0
                col_max = float(valid_data.max()) if not valid_data.empty else 100.0
                dynamic_jscode = JsCode(f"""
                    function(params) {{
                        const val = params.value;
                        if (val === null || val === undefined || val <= 0) return null;
                        const min = {col_min}; const max = {col_max};
                        if (max === min) return {{ 'backgroundColor': '#ffffff', 'color': 'black' }};
                        const ratio = (val - min) / (max - min);
                        let r, g, b;
                        if (ratio < 0.5) {{ const pct = ratio / 0.5; r = 255; g = Math.round(100 + (155 * pct)); b = Math.round(100 + (155 * pct)); }}
                        else {{ const pct = (ratio - 0.5) / 0.5; r = Math.round(255 - (155 * pct)); g = 255; b = Math.round(255 - (155 * pct)); }}
                        return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')', 'color': 'black', 'fontWeight': ratio >= 0.9 ? 'bold' : 'normal' }};
                    }}
                """)
                if col == 'Avg_Perf':
                    gb.configure_column(col, minWidth=70, maxWidth=90, cellStyle=dynamic_jscode)
                else:
                    gb.configure_column(col, minWidth=55, maxWidth=80, cellStyle=dynamic_jscode)

            # _nr4_previous highlight
            if '_nr4_previous' in filtered_df.columns:
                gb.configure_column('_nr4_previous', minWidth=55, maxWidth=75,
                                    cellStyle=JsCode(
                                        "function(params){return{'backgroundColor':'#fff3cd','color':'#664d03','fontWeight':'bold'};}"))

            # _chg_percentclose_hourly heatmap
            if '_chg_percentclose_hourly' in filtered_df.columns:
                valid_chg = filtered_df[filtered_df['_chg_percentclose_hourly'] > 0]['_chg_percentclose_hourly']
                chg_min = float(valid_chg.min()) if not valid_chg.empty else 0.0
                chg_max = float(valid_chg.max()) if not valid_chg.empty else 10.0
                chg_jscode = JsCode(f"""
                    function(params) {{
                        const val = params.value; if (!val || val <= 0) return null;
                        const min = {chg_min}; const max = {chg_max};
                        if (max === min) return {{ 'backgroundColor': '#ffe6ff', 'color': 'black' }};
                        const ratio = Math.min((val - min) / (max - min), 1.0);
                        const r = Math.round(255 - (115 * ratio));
                        const g = Math.round(220 - (220 * ratio));
                        const b = Math.round(255 - (115 * ratio));
                        return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')',
                                 'color': ratio > 0.5 ? 'white' : 'black',
                                 'fontWeight': ratio >= 0.8 ? 'bold' : 'normal' }};
                    }}
                """)
                gb.configure_column('_chg_percentclose_hourly', minWidth=80, maxWidth=110,
                                    cellStyle=chg_jscode, filter='agNumberColumnFilter',
                                    filterParams={'filterOptions': ['greaterThan', 'lessThan', 'equals', 'inRange'],
                                                  'defaultOption': 'greaterThan', 'defaultValues': [0]})

            for col in ['Adr', 'Ti65', '_nr4']:
                if col in filtered_df.columns:
                    gb.configure_column(col, minWidth=55, maxWidth=75)

            # rvol heatmap
            if 'dvolhourly' in filtered_df.columns and '_avgvol_mln_hourly' in filtered_df.columns:
                valid_rvol = filtered_df[(filtered_df['dvolhourly'] > 0) &
                                         (filtered_df['_avgvol_mln_hourly'] > 0)].copy()
                if not valid_rvol.empty:
                    valid_rvol['rvol'] = valid_rvol['dvolhourly'] / valid_rvol['_avgvol_mln_hourly']
                    above_avg = valid_rvol[valid_rvol['rvol'] > 1.0]['rvol']
                    rvol_floor = max(float(above_avg.min()), 1.0) if not above_avg.empty else 1.0
                    rvol_ceiling = float(above_avg.max()) if not above_avg.empty else 3.0
                else:
                    rvol_floor, rvol_ceiling = 1.0, 3.0

                rvol_jscode = JsCode(f"""
                    function(params) {{
                        const dvol = params.data ? params.data.dvolhourly : null;
                        const avgvol = params.data ? params.data._avgvol_mln_hourly : null;
                        if (!dvol || !avgvol || avgvol <= 0 || dvol <= 0) return null;
                        const ratio = dvol / avgvol; if (ratio <= 1.0) return null;
                        const floor = {rvol_floor}; const ceiling = {rvol_ceiling};
                        if (ceiling <= floor) return {{ 'backgroundColor': '#d4edda', 'color': 'black' }};
                        const normRatio = Math.min((ratio - floor) / (ceiling - floor), 1.0);
                        let r, g, b;
                        if (normRatio < 0.5) {{ const pct = normRatio / 0.5; r = Math.round(248 - (208 * pct)); g = Math.round(255 - (90 * pct)); b = Math.round(248 - (181 * pct)); }}
                        else {{ const pct = (normRatio - 0.5) / 0.5; r = Math.round(40 - (17 * pct)); g = Math.round(165 - (78 * pct)); b = Math.round(67 - (31 * pct)); }}
                        return {{ 'backgroundColor': 'rgb(' + r + ',' + g + ',' + b + ')',
                                  'color': 'black',
                                  'fontWeight': normRatio >= 0.8 ? 'bold' : 'normal' }};
                    }}
                """)
                gb.configure_column('dvolhourly', minWidth=65, maxWidth=90, cellStyle=rvol_jscode)
                gb.configure_column('_avgvol_mln_hourly', minWidth=65, maxWidth=90, cellStyle=rvol_jscode)

            if 'dvoldaily' in filtered_df.columns:
                gb.configure_column('dvoldaily', minWidth=65, maxWidth=90)

            # MA dist heatmap
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
            for c in ['_20madist', '_10madist']:
                if c in filtered_df.columns:
                    gb.configure_column(c, minWidth=70, maxWidth=90, cellStyle=ma_dist_jscode)

            # Score column styling
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
            for sc_col in ['Tight_Score', 'Vol_Score', 'TClose_Score', 'StrongOpen_Score',
                           'WTC_Score', 'MA20_Score', 'MA10_Score', 'Total_Score']:
                if sc_col in filtered_df.columns:
                    gb.configure_column(sc_col, minWidth=55, maxWidth=75, cellStyle=score_col_style)

            # Tier pill styling
            tier_style = JsCode("""
                function(params) {
                    const val = params.value;
                    if (!val) return null;
                    if (val.includes('A')) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                    if (val.includes('B')) return { 'backgroundColor': '#ffc107', 'color': 'black', 'fontWeight': 'bold' };
                    return { 'backgroundColor': '#f8d7da', 'color': '#721c24' };
                }
            """)
            if 'Tier' in filtered_df.columns:
                gb.configure_column('Tier', minWidth=55, maxWidth=75, cellStyle=tier_style, pinned='left')
            if 'Symbol' in filtered_df.columns:
                gb.configure_column('Symbol', minWidth=90, maxWidth=130, pinned='left')

            gridOptions = gb.build()

            # ── Clean the df for JSON, then render ──
            safe_df = clean_df_for_json(filtered_df)
            AgGrid(
                safe_df,
                gridOptions=gridOptions,
                update_mode=GridUpdateMode.MODEL_CHANGED,
                fit_columns_on_grid_load=False,
                height=680,
                theme='streamlit',
                key='crypto_gtt_grid',
                allow_unsafe_jscode=True
            )

            # ── Export ──
            csv = filtered_df.to_csv(index=False).encode('utf-8')
            st.download_button(
                "📥 Download filtered CSV",
                data=csv,
                file_name=f"gtt_crypto_scanner_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime='text/csv'
            )
        else:
            st.info("Click **Generate GTT Crypto Plan** to fetch and score scans.")

    # ════════════════════════════════════════════════════════════════
    # TAB 2 — WEEKLY / DAILY WATCH
    # ════════════════════════════════════════════════════════════════
    with tab2:
        if 'wd_full_df' in st.session_state and st.session_state.wd_full_df is not None:
            wd = st.session_state.wd_full_df.copy()
            st.subheader("Weekly & Daily Base-Formation Watch")
            st.caption("Mirror of the stock scanner's Weekly Base Watch — full 12-metric breakdown "
                       "for crypto symbols from the daily/weekly show-only screen.")

            wd_default_hidden = ['Eema20', 'Eema10']
            all_wd_cols = list(wd.columns)
            with st.expander("🧩 Choose visible columns (Weekly/Daily table)"):
                sel_wd_cols = st.multiselect(
                    "Columns to show",
                    options=all_wd_cols,
                    default=get_persisted_columns('crypto_wd_table', all_wd_cols, wd_default_hidden),
                    key="wd_table_col_select"
                )
                if st.button("💾 Save as default", key="save_wd_cols_btn"):
                    prefs = load_column_prefs()
                    prefs['crypto_wd_table'] = sel_wd_cols
                    save_column_prefs(prefs)
                    st.success("Saved.")
            hidden_wd = [c for c in all_wd_cols if c not in sel_wd_cols]
            col_prec_wd = {c: i for i, c in enumerate(['Symbol', 'Last'] + weekly_daily_metric_columns)}
            sel_wd_cols = sorted(sel_wd_cols, key=lambda c: col_prec_wd.get(c, 9999))
            hidden_wd = sorted(hidden_wd, key=lambda c: col_prec_wd.get(c, 9999))
            wd = wd[sel_wd_cols + hidden_wd]

            gb2 = GridOptionsBuilder.from_dataframe(wd)
            gb2.configure_default_column(resizable=True, filterable=True, sortable=True, minWidth=70, flex=0)
            gb2.configure_side_bar()
            gb2.configure_grid_options(enableBrowserTooltips=True)
            if 'Symbol' in wd.columns:
                gb2.configure_column('Symbol', minWidth=90, maxWidth=130, pinned='left')

            # RSI heatmap
            for rsi_col in ['Weeklyrsi', 'Dailyrsi']:
                if rsi_col not in wd.columns:
                    continue
                rsi_js = JsCode("""
                    function(params) {
                        const v = params.value;
                        if (v === null || v === undefined || isNaN(v)) return null;
                        if (v >= 70) return { 'backgroundColor': '#28a745', 'color': 'white', 'fontWeight': 'bold' };
                        if (v >= 55) return { 'backgroundColor': '#8ee68e', 'color': 'black' };
                        if (v >= 45) return { 'backgroundColor': '#ffffff', 'color': 'black' };
                        if (v >= 30) return { 'backgroundColor': '#ffe6ff', 'color': 'black' };
                        return { 'backgroundColor': '#f8d7da', 'color': '#721c24', 'fontWeight': 'bold' };
                    }
                """)
                gb2.configure_column(rsi_col, minWidth=60, maxWidth=85, cellStyle=rsi_js)

            # Dist_dema heatmap
            for c in ['Dist_dema10_pct', 'Dist_dema20_pct']:
                if c in wd.columns:
                    gb2.configure_column(c, minWidth=70, maxWidth=90, cellStyle=ma_dist_jscode)

            # ── Clean the df for JSON, then render ──
            safe_wd = clean_df_for_json(wd)
            AgGrid(safe_wd, gridOptions=gb2.build(),
                   update_mode=GridUpdateMode.MODEL_CHANGED,
                   fit_columns_on_grid_load=False,
                   height=600, theme='streamlit', key='crypto_wd_grid',
                   allow_unsafe_jscode=True)
        else:
            st.info("Generate a scan first to populate the Weekly/Daily watch.")


if __name__ == "__main__":
    main()