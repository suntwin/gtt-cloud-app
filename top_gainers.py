"""Top gainers · last session — Martin Luk's "potent" scan: yesterday's strongest movers, grouped by sector,
so money rotating into a theme shows up (several names from one sector, day after day).

Data: your MarketInOut EP scan (whole market: Chg % > 3, volume > 2x 50-day avg, liquidity floors).
History: one row per gainer per session in Supabase table top_gainers (supabase_gainers.sql), so a sector
that keeps showing up across sessions stands out from a one-day pop.
"""
from io import StringIO

import numpy as np
import pandas as pd
import requests

import gtt_process as gp

# MarketInOut "EP scan" per market. Set the key once the screen is saved (Screens → API link).
GAINER_SCREEN = {"NSE": None, "USA": None}
# Column order of the EP scan output: Symbol, Last, Timestamp, then the `show(...)` fields in order
EP_COLS = ["Symbol", "Last", "Timestamp", "_chg_percentclose", "dvol", "_avgvol_cr", "_circuit", "_rvol_to_float",
           "Adr", "Ti65", "_avg_vol_float_ratio", "_20madist", "_10wmadist", "_10madist", "_nr4", "_nr4_previous",
           "_rs", "_period_perf", "_rvol", "_wtc", "_reversal"]
LOOKBACK = 10          # sessions of history kept in view
STREAK = 5             # "how many of the last 5 sessions did this sector have gainers"
_TXT = ("Symbol", "Timestamp", "_wtc", "_reversal")


def fetch_gainers(url):
    """The EP scan as a DataFrame (one row per stock), or None."""
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        if r.status_code != 200 or not r.text.strip():
            return None
        df = pd.read_csv(StringIO(r.text), sep="|", header=None)
        n = len(df.columns)
        df.columns = EP_COLS[:n] + [f"Extra_{i}" for i in range(len(EP_COLS), n)]
        df["Symbol"] = df["Symbol"].astype(str).str.upper().str.replace(".NS", "", regex=False).str.strip()
        for c in df.columns:
            if c not in _TXT:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.drop_duplicates("Symbol").reset_index(drop=True)
    except Exception:
        return None


def prepare(raw, sector_df, base_df):
    """Display frame: sector, volume multiple, whether the stock is already in your momentum scan."""
    g = raw.copy()
    if sector_df is not None and not sector_df.empty:
        g = g.merge(sector_df, on="Symbol", how="left")
    for c in ("Sector", "Industry"):
        g[c] = g[c].fillna("Unknown") if c in g.columns else "Unknown"
    vx = g["_rvol"] if "_rvol" in g.columns else pd.Series(np.nan, index=g.index)
    if "dvol" in g.columns and "_avgvol_cr" in g.columns:
        vx = vx.fillna(g["dvol"] / g["_avgvol_cr"].replace(0, np.nan))
    g["Vol x"] = vx.round(1)
    scan = set(base_df["Symbol"].astype(str).str.upper()) if base_df is not None and "Symbol" in base_df.columns else set()
    g["In scan"] = g["Symbol"].isin(scan).map({True: "✓", False: ""})
    return g.sort_values("_chg_percentclose", ascending=False, kind="stable").reset_index(drop=True)


def save_history(sb, mcfg, sd, g):
    def f(x):
        x = gp._num(x)
        return float(x) if np.isfinite(x) else None
    rows = [{"user_id": mcfg["user_id"], "market": mcfg["market"], "snap_date": str(sd), "symbol": r["Symbol"],
             "sector": r["Sector"], "industry": r["Industry"], "chg": f(r.get("_chg_percentclose")),
             "vol_x": f(r.get("Vol x")), "last": f(r.get("Last"))}
            for r in g.to_dict("records")]
    for i in range(0, len(rows), 500):
        sb.table("top_gainers").upsert(rows[i:i + 500], on_conflict="user_id,market,snap_date,symbol").execute()


def load_history(sb, mcfg, days=30):
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    r = (sb.table("top_gainers").select("snap_date,symbol,sector,chg").eq("user_id", mcfg["user_id"])
         .eq("market", mcfg["market"]).gte("snap_date", since).order("snap_date", desc=True).limit(5000).execute().data)
    h = pd.DataFrame(r)
    if not h.empty:
        h["snap_date"] = h["snap_date"].astype(str).str[:10]
    return h


def sector_table(g, hist, sd):
    """One row per sector: gainers this session, share, how many of the last 5 sessions it showed up, trend."""
    sd = str(sd)
    h = hist if hist is not None and not hist.empty else pd.DataFrame(columns=["snap_date", "symbol", "sector", "chg"])
    h = h[h["snap_date"] != sd]                                     # today comes from the live scan
    today = g.assign(snap_date=sd, symbol=g["Symbol"], sector=g["Sector"], chg=g["_chg_percentclose"])[
        ["snap_date", "symbol", "sector", "chg"]]
    allh = pd.concat([today, h], ignore_index=True)
    sessions = sorted(allh["snap_date"].unique(), reverse=True)[:LOOKBACK]
    allh = allh[allh["snap_date"].isin(sessions)]
    last5 = sessions[:STREAK]
    prev = sessions[1:STREAK]
    out = []
    for sec, grp in g.groupby("Sector"):
        hs = allh[allh["sector"] == sec]
        n5 = hs[hs["snap_date"].isin(last5)]
        days5 = n5["snap_date"].nunique()
        prev_n = len(hs[hs["snap_date"].isin(prev)]) / max(len(prev), 1)
        top = grp.sort_values("_chg_percentclose", ascending=False)
        out.append({"Sector": sec, "Today": len(grp), "Share %": round(100 * len(grp) / max(len(g), 1), 1),
                    "Avg chg %": round(grp["_chg_percentclose"].mean(), 1),
                    f"Days in last {len(last5)}": days5, f"Gainers {len(last5)}d": len(n5),
                    "Prior avg/day": round(prev_n, 1), "In scan": int((grp["In scan"] == "✓").sum()),
                    "Top names": ", ".join(top["Symbol"].head(4))})
    st_ = pd.DataFrame(out)
    if st_.empty:
        return st_, len(sessions)
    dcol = f"Days in last {len(last5)}"

    def trend(r):
        if r["Today"] >= 3 and r[dcol] >= 3:
            return "🔥 Rotation (repeat)"
        if r["Today"] >= 3 and r["Today"] >= 2 * max(r["Prior avg/day"], 1):
            return "🆕 New theme"
        if r[dcol] >= 3:
            return "Steady"
        return ""
    st_["Trend"] = st_.apply(trend, axis=1)
    st_ = st_.sort_values(["Today", dcol, "Avg chg %"], ascending=False).reset_index(drop=True)
    return st_[["Sector", "Trend", "Today", "Share %", dcol, f"Gainers {len(last5)}d", "Prior avg/day",
                "Avg chg %", "In scan", "Top names"]], len(sessions)


def _c_trend(x):
    s = str(x)
    if s.startswith("🔥"): return gp._css("#28a745", "white", True)
    if s.startswith("🆕"): return gp._css("#cff4fc", "#055160", True)
    if s == "Steady": return gp._css("#d4edda")
    return ""


def _c_days(v):
    v = gp._num(v)
    if not np.isfinite(v): return ""
    if v >= 4: return gp._css("#28a745", "white", True)
    if v >= 3: return gp._css("#8ee68e")
    if v >= 2: return gp._css("#d4edda")
    return ""


def _c_count(v):
    try:
        v = float(v)
    except Exception:
        return ""
    if v >= 5: return gp._css("#28a745", "white", True)
    if v >= 3: return gp._css("#8ee68e")
    if v >= 2: return gp._css("#d4edda")
    return ""


def render_top_gainers(st, sb, base_df, mcfg, sector_df=None):
    mkt = mcfg["market"]
    with st.expander("🚀 Top gainers · last session — spot sector rotation"):
        url = GAINER_SCREEN.get(mkt)
        if not url:
            st.info(f"Add your MarketInOut EP scan's API link for {mkt} (top_gainers.py → GAINER_SCREEN) to turn this on.")
            return
        cache = st.cache_data(ttl=300, show_spinner=False)(fetch_gainers)
        raw = cache(url)
        if raw is None or raw.empty:
            st.warning("The EP scan returned nothing (MarketInOut down, or no stock passed today)."); return
        g = prepare(raw, sector_df, base_df)
        sd = gp.scan_data_date(g) or gp.effective_session(base_df, mcfg, sb)[0]
        hist, hist_ok = pd.DataFrame(), sb is not None
        if sb is not None:
            try:
                key = f"tg_saved_{mkt}"
                if st.session_state.get(key) != (str(sd), len(g)):          # once per session date (and list size)
                    save_history(sb, mcfg, sd, g)
                    st.session_state[key] = (str(sd), len(g))
                hist = load_history(sb, mcfg)
            except Exception as e:
                hist_ok = False
                st.warning(f"History not saved — run supabase_gainers.sql once in Supabase. ({str(e)[:120]})")
        tbl, n_sess = sector_table(g, hist, sd)
        counts = hist.groupby("snap_date")["symbol"].nunique() if not hist.empty else pd.Series(dtype=float)
        counts = counts[counts.index != str(sd)]
        avg_n = f" · average {counts.tail(LOOKBACK).mean():.0f} over the previous {min(len(counts), LOOKBACK)} sessions" \
            if len(counts) else ""
        st.markdown(f"**{len(g)} gainers** on **{sd}** (Chg % > 3 on > 2x volume){avg_n}. "
                    "More gainers = healthier tape; a shrinking list = money leaving.")
        if hist_ok and n_sess < 3:
            st.caption(f"History builds up from today — trend and repeat counts get useful after a few sessions "
                       f"({n_sess} so far).")

        st.markdown("**Sectors** — 🔥 = 3+ gainers today and in 3+ of the last 5 sessions (money keeps coming back) · "
                    "🆕 = 3+ today, at least double its usual count")
        dcol = [c for c in tbl.columns if c.startswith("Days in last")]
        sty = tbl.style.map(_c_trend, subset=["Trend"]).map(_c_count, subset=["Today"])
        if dcol:
            sty = sty.map(_c_days, subset=dcol)
        sty = sty.format({"Share %": "{:.1f}", "Avg chg %": "{:+.1f}", "Prior avg/day": "{:.1f}"})
        st.dataframe(sty, hide_index=True, width="stretch", height=min(36 * len(tbl) + 40, 420),
                     column_config={"Sector": st.column_config.TextColumn(pinned=True, width="medium"),
                                    "Top names": st.column_config.TextColumn(width="large")})

        secs = tbl["Sector"].tolist()
        pick = st.multiselect("Show gainers from", secs, default=[], key=f"tg_secs_{mkt}",
                              placeholder="All sectors")
        v = g[g["Sector"].isin(pick)] if pick else g
        disp = pd.DataFrame({
            "Symbol": v["Symbol"], "Last": v["Last"], "Chg %": v["_chg_percentclose"].round(2), "Vol x": v["Vol x"],
            "Sector": v["Sector"], "Industry": v["Industry"], "In scan": v["In scan"],
            "ADR": v.get("Adr", np.nan), "Ti65": v.get("Ti65", np.nan),
            "Day vol": v.get("dvol", np.nan), "Avg vol": v.get("_avgvol_cr", np.nan),
            "10MA %": v.get("_10madist", np.nan), "20MA %": v.get("_20madist", np.nan),
            "1M perf %": v.get("_period_perf", np.nan), "Circuit %": v.get("_circuit", np.nan)})
        if mkt != "NSE":
            disp = disp.drop(columns=["Circuit %"])
        if not hist.empty:
            rep = hist[hist["snap_date"] != str(sd)].groupby("symbol")["snap_date"].nunique()
            disp.insert(7, "Prior days", disp["Symbol"].map(rep).fillna(0).astype(int))
        for c in disp.columns:                                      # round for display
            if pd.api.types.is_float_dtype(disp[c]):
                disp[c] = disp[c].round(0 if c in ("Day vol", "Avg vol", "Circuit %") else 1 if c == "Vol x" else 2)
        dsty = disp.style.map(gp._c_chg, subset=["Chg %"]).map(gp._c_vol, subset=["Vol x"]) \
            .map(gp._c_madist, subset=["10MA %", "20MA %"])
        st.dataframe(dsty, hide_index=True, width="stretch", height=min(36 * len(disp) + 40, 520),
                     column_config={**{c: st.column_config.NumberColumn(
                                        format="%.0f" if c in ("Day vol", "Avg vol", "Circuit %", "Prior days")
                                        else "%.1f" if c == "Vol x" else "%.2f", pinned=c in ("Last", "Chg %"))
                                        for c in disp.columns if pd.api.types.is_numeric_dtype(disp[c])},
                                    "Symbol": st.column_config.TextColumn(pinned=True)})
        st.caption("In scan ✓ = already in your 1M/3M/6M momentum scan (a leader moving); blank = a fresh mover "
                   "outside your universe — check whether its sector shows up above.")
        syms = ",".join(gp.tv_symbol(s, mcfg) for s in disp["Symbol"])
        gp.copy_area(st, f"{len(disp)} gainers{' in ' + ', '.join(pick) if pick else ''} — for TradingView", syms,
                     key=f"tg_copy_{mkt}")
