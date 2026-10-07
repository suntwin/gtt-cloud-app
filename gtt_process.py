"""
GTT process — the daily routine built around the scanner.  Market-agnostic (NSE now, USA later).

Goal: manage the lists so no potential trade is missed, with a fixed routine:

  EVENING, ONCE      Anticipation scanner → tick tomorrow's list → "Save tomorrow's list"
                     (daily_snapshots, scanner = ANTICIPATION)
  LATE SESSION, ONCE Post Breakout scanner → tag missed breakouts → "Save to watchlist"
                     (watchlist rows EP / TIGHT_BO / WEMA_BO, plus daily_snapshots, scanner = BREAKOUT)
  MARKET HOURS       No scanning. Work the list from the Watchlist tab; act only on alerts.
  WEEKEND            Watchlist tab → clean-up.

Tables / functions: see supabase_migration.sql.
"""
from datetime import datetime, date, timedelta
import copy
import time
import numpy as np
import pandas as pd

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


# ════════════════════════════════════════════════════════════════════════════
# Market settings — NSE page uses MARKETS["NSE"], USA page uses MARKETS["USA"]
# ════════════════════════════════════════════════════════════════════════════
MARKETS = {
    "NSE": {"market": "NSE", "exchange": "NSE", "user_id": "nse_user", "tz": "Asia/Kolkata",
            "open": (9, 15), "close": (15, 30), "local_hint": "8:00 PM Sydney"},
    "USA": {"market": "USA", "exchange": "NASDAQ", "user_id": "us_user", "tz": "America/New_York",
            "open": (9, 30), "close": (16, 0), "local_hint": "6:00 AM Sydney",
            "tv_prefix": False},   # US stocks trade on NASDAQ and NYSE — TradingView finds bare symbols
}

PROCESS_VERSION = "v2026-10-07k · Data patterns: 10W Launch Pad filter + trade alert"   # shown on the page so you can tell which code is running
SETUP_TYPES = ["EP", "TIGHT_BO", "WEMA_BO", "ATH", "CONTINUATION"]
SETUP_NAMES = {"EP": "Episodic pivot", "TIGHT_BO": "Tight-range breakout", "WEMA_BO": "10-week EMA breakout",
               "ATH": "All-time-high breakout",
               "CONTINUATION": "Continuation — second leg after an earlier breakout",
               "UNTAGGED": "Untagged (old saved list)"}
NO_TAG = "—"
RATINGS = ["—", "3★", "4★", "5★"]


def rating_int(v):
    try:
        return int(str(v).strip()[0]) if str(v).strip()[:1] in "12345" else None
    except Exception:
        return None


def rating_label(v):
    try:
        return f"{int(v)}★" if v is not None and not pd.isna(v) else "—"
    except Exception:
        return "—"

# Anticipation / tomorrow's list rules (fixed for 8 weeks)
TOMORROW_DEFAULTS = {
    "min_adr": 3.0, "min_liq": 10.0, "max_reltight": 0.8, "max_rwd": 1.5,
    "min_wktight": 2, "min_chg": -1.0, "max_chg": 3.0, "list_size": 10,
    "bo_min_chg": 2.0, "bo_min_vol": 1.5, "gap_max_adr": 2.0,
    "pullback_band": 2.0, "stale_days": 20,
    "missing_days": 3, "min_circuit": 10,
    "recent_bo_days": 5, "recent_bo_max_rwd": 3.0,
    # Data pattern "10W Launch Pad" (UNIVCABLES / KSHINTL): on the 10w line, tight, sitting on the 10/20 MA.
    # MA distances in ADRs, so a small dip below the MAs still counts. Volume is NOT part of the pattern;
    # it only raises the trade alert in the Reason column.
    "lp_max_rwd": 1.0, "lp_max_reltight": 0.8, "lp_ma_min_adr": -0.3, "lp_ma_max_adr": 1.0,
    "lp_max_chg_adr": 1.0, "lp_alert_vol": 0.9,
    "tier1_adr": 7.0, "tier2_adr": 5.0,     # ADR tiers: P1 ≥ tier1, P2 tier2–tier1, P3 below (NSE defaults below)
}
TIER_DEFAULTS = {"USA": {"tier1_adr": 7.0, "tier2_adr": 5.0}, "NSE": {"tier1_adr": 6.0, "tier2_adr": 4.5}}


def adr_tier(adr, cfg):
    """P1 = high ADR, P2 = middle, P3 = low; '' when ADR is missing."""
    a = pd.to_numeric(adr, errors="coerce")
    t = pd.Series("P3", index=a.index)
    t[a >= cfg["tier2_adr"]] = "P2"
    t[a >= cfg["tier1_adr"]] = "P1"
    t[a.isna()] = ""
    return t
# Post-breakout tagging rules
BREAKOUT_DEFAULTS = {
    "bo_min_chg": 2.0, "bo_min_vol": 1.5,   # what counts as a breakout
    "strong_min_vol": 3.5,                  # Strong BO batch (same split as "Copy Symbols": ≥3.5x strong, 1.5–3.5x moderate)
    "ep_min_chg": 8.0, "ep_min_vol": 3.0,   # EP: big move on huge volume
    "tight_max_reltight": 1.0,              # TIGHT_BO: yesterday's NR4 / ADR
    "wema_max_adr": 2.0,                    # WEMA_BO: within N ADRs of the 10w EMA
    "cont_max_days": 15,                    # CONTINUATION: an earlier breakout within this many days (scan's days-since-BO)
}
CLEANUP_DEFAULTS = {"max_age_days": 30, "unseen_days": 10}

LIST_LABELS = ["4_RESETUP", "SAVED_WAIT", "ADDED", "1_BUY_SIGNAL", "2_SAVE", "3_WAIT", "5_REMOVE", "RECENT_BO", "PATTERN", "CANDIDATE", "CHECK", "SCANNED"]
LABEL_NAMES = {"4_RESETUP": "Saved & tight", "SAVED_WAIT": "Saved, not tight yet", "ADDED": "Added by you", "1_BUY_SIGNAL": "1 · Buy signal", "2_SAVE": "2 · Save (tag it)", "3_WAIT": "3 · Wait",
               "5_REMOVE": "5 · Weak (review)", "RECENT_BO": "Recent BO, now tight", "CANDIDATE": "New candidate",
               "PATTERN": "Pattern match", "CHECK": "Check chart"}
TOMORROW_LABELS = {"3_WAIT", "4_RESETUP"}   # always carried onto tomorrow's list

SNAPSHOT_METRICS = ["Last", "_chg_percentclose", "_vol_ratio", "Adr", "_nr4", "_nr4_previous",
                    "_rel_tightness_today", "_rel_tightness_prev", "_rel_wk_dist", "W_Dist10wMA",
                    "W_TightCloses_10w", "_10madist", "_20madist", "_avgvol_mln", "Avg_RS",
                    "RS_1M", "RS_3M", "RS_6M", "Sector", "Suggested", "rating"]


# ════════════════════════════════════════════════════════════════════════════
# Time
# ════════════════════════════════════════════════════════════════════════════
def market_now(mcfg):
    return datetime.now(ZoneInfo(mcfg["tz"])) if ZoneInfo else datetime.now()


def session_date(now, mcfg):
    """Date of the latest session: before the open, or on weekends, step back to the last weekday."""
    d = now.date()
    oh, om = mcfg["open"]
    if now.hour * 60 + now.minute < oh * 60 + om:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def market_is_open(now, mcfg):
    m = now.hour * 60 + now.minute
    return now.weekday() < 5 and mcfg["open"][0] * 60 + mcfg["open"][1] <= m < mcfg["close"][0] * 60 + mcfg["close"][1]


# ════════════════════════════════════════════════════════════════════════════
# Pure logic (no Streamlit, no DB)
# ════════════════════════════════════════════════════════════════════════════
def _num(x, default=np.nan):
    try:
        v = float(x)
        return v if np.isfinite(v) else default
    except (TypeError, ValueError):
        return default


CIRCUIT_HELP = "NSE price band %: 20 = free to move · 10 = caution · 5 or less = can lock in circuit, a stop may not fill"


def _with_circuit(cols, mcfg, after="Adr"):
    """NSE only: show the circuit (price band) column right after ADR."""
    if mcfg.get("market") != "NSE" or "_circuit" in cols:
        return cols
    i = cols.index(after) + 1 if after in cols else len(cols)
    return cols[:i] + ["_circuit"] + cols[i:]


def _circuit_num(df):
    if "_circuit" in df.columns:
        df["_circuit"] = pd.to_numeric(df["_circuit"], errors="coerce")
    return df


def tv_symbol(sym, mcfg, exchange=None):
    """TradingView import symbol. USA → bare ticker (AAPL); NSE → NSE:RELIANCE."""
    if mcfg.get("market") == "USA" or not mcfg.get("tv_prefix", True):
        return str(sym).strip().upper()
    return f"{exchange or mcfg['exchange']}:{sym}"


def parse_ts_dates(ts):
    """MarketInOut Timestamp (e.g. '09/25/2026 09:31') → date per row (NaT if unreadable)."""
    try:
        num = pd.to_numeric(ts, errors="coerce")
        if num.notna().mean() > 0.8 and num.dropna().median() > 1e9:        # epoch seconds / ms
            unit = "ms" if num.dropna().median() > 1e12 else "s"
            parsed = pd.to_datetime(num, unit=unit, errors="coerce")
        else:
            txt = ts.astype(str).str.strip()
            parsed = pd.to_datetime(txt, errors="coerce", format="%m/%d/%Y %H:%M")   # MarketInOut format
            if parsed.notna().mean() < 0.5:
                try:
                    parsed = pd.to_datetime(txt, errors="coerce", format="mixed")
                except (TypeError, ValueError):
                    parsed = pd.to_datetime(txt, errors="coerce")
        return parsed.dt.date.where(parsed.notna(), None)
    except Exception:
        return pd.Series([None] * len(ts), index=ts.index)


def add_derived(df):
    """Scan count and tightness/distance metrics. Never drops rows."""
    d = df.copy()
    for c in ["RS_1M", "RS_3M", "RS_6M"]:
        if c not in d.columns:
            d[c] = 0
    for c in ["_nr4", "_nr4_previous", "_10madist", "_20madist", "Avg_RS", "_avgvol_mln", "Last",
              "_chg_percentclose", "Adr", "W_Dist10wMA", "W_TightCloses_10w"]:
        if c not in d.columns:
            d[c] = np.nan
    d["Scan_Count"] = (d[["RS_1M", "RS_3M", "RS_6M"]].fillna(0) > 0).sum(axis=1).astype(int)
    adr = d["Adr"].replace(0, np.nan)
    d["_rel_tightness_today"] = (d["_nr4"] / adr).round(2)
    d["_rel_tightness_prev"] = (d["_nr4_previous"] / adr).round(2)
    d["_rel_wk_dist"] = (d["W_Dist10wMA"].abs() / adr).round(2)      # NaN stays NaN
    if "dvol" in d.columns and "_avgvol_mln" in d.columns:
        d["_vol_ratio"] = (d["dvol"] / d["_avgvol_mln"].replace(0, np.nan)).round(2)
    elif "_vol_ratio" not in d.columns:
        d["_vol_ratio"] = np.nan
    d["Missing_Weekly"] = d["W_Dist10wMA"].isna() | d["W_TightCloses_10w"].isna()
    d["Data_Date"] = parse_ts_dates(d["Timestamp"]) if "Timestamp" in d.columns else None
    return d


# ── Colours: same bands as the main scanner grid ──
def _css(bg, fg="black", bold=False):
    return f"background-color:{bg};color:{fg}" + (";font-weight:bold" if bold else "")


def _c_vol(v):
    v = _num(v)
    if not np.isfinite(v) or v <= 0: return ""
    if v >= 6.5: return _css("#155724", "white", True)
    if v >= 3.5: return _css("#28a745", "white", True)
    if v >= 2.5: return _css("#5cb85c", "white", True)
    if v >= 1.5: return _css("#8ee68e")
    if v >= 1.0: return _css("#d4edda")
    if v < 0.5: return _css("#f8d7da", "#721c24")
    return ""


def _c_relwk(v):
    v = _num(v)
    if not np.isfinite(v): return ""
    if v < 1: return _css("#28a745", "white", True)
    if v < 2: return _css("#8ee68e")
    if v < 3: return _css("#d4edda")
    if v < 5: return _css("#fff3cd", "#664d03")
    return _css("#f8d7da", "#721c24")


def _c_madist(v):
    v = _num(v)
    if not np.isfinite(v): return ""
    if v < -6: return _css("#f8d7da", "#721c24", True)
    a = abs(v)
    if a < 2: return _css("#28a745", "white", True)
    if a < 4: return _css("#8ee68e")
    if a < 6: return _css("#d4edda")
    return ""


def _c_tight(v):
    v = _num(v)
    if not np.isfinite(v): return ""
    if v <= 0.5: return _css("#fff3cd", "#664d03", True)
    if v <= 1.0: return _css("#fff3cd", "#664d03")
    return ""


def _c_chg(v):
    v = _num(v)
    if not np.isfinite(v) or v <= 0: return ""
    if v >= 8: return _css("#8c008c", "white", True)
    if v >= 5: return _css("#b44cb4", "white")
    if v >= 2: return _css("#e6b3e6")
    return _css("#ffe6ff")


SITUATION_COLOURS = {"Saved & tight": _css("#28a745", "white", True), "Saved, not tight yet": _css("#d4edda"),
                     "Added by you": _css("#e2d9f3", "#3d2a73"), "1 · Buy signal": _css("#155724", "white", True),
                     "2 · Save (tag it)": _css("#cfe2ff", "#084298"), "3 · Wait": _css("#fff3cd", "#664d03"),
                     "5 · Weak (review)": _css("#f8d7da", "#721c24", True), "Check chart": _css("#ffe5d0", "#8a4b08"),
                     "Recent BO, now tight": _css("#cff4fc", "#055160", True), "Pattern match": _css("#ffd8a8", "#7a3e00", True)}


def _c_circuit(v):
    """NSE price band: 20% = free to move; 10% caution; 5% or less = can lock in circuit (stop may not fill)."""
    v = _num(v)
    if not np.isfinite(v) or v <= 0: return ""
    if v <= 5: return _css("#f8d7da", "#721c24", True)
    if v <= 10: return _css("#fff3cd", "#664d03")
    return ""


def _c_rating(v):
    return {"5★": _css("#28a745", "white", True), "4★": _css("#8ee68e"), "3★": _css("#d4edda")}.get(str(v), "")


LIQ_HELP = ("Average daily volume from the scan (same figure as the scanner's AvgVol). Liquidity: green = top third "
            "of this table, red = below the new-candidate liquidity floor. Prefer the green ones among equally tight setups.")


def _liq_styles(col, floor):
    v = pd.to_numeric(col, errors="coerce")
    hi = v[v > 0].quantile(2 / 3) if (v > 0).sum() >= 3 else np.inf
    def one(x):
        if pd.isna(x) or x <= 0: return ""
        if x < floor: return "background-color:#f8d7da;color:#721c24"
        if x >= hi: return "background-color:#28a745;color:white;font-weight:bold"
        return ""
    return [one(x) for x in v]


def style_table(df, liq_floor=None):
    """pandas Styler with the scanner's colour bands (works inside st.data_editor)."""
    cols = set(df.columns)
    sty = df.style
    if "_avgvol_mln" in cols:
        _fl = float(TOMORROW_DEFAULTS["min_liq"] if liq_floor is None else liq_floor)
        sty = sty.apply(lambda c: _liq_styles(c, _fl), subset=["_avgvol_mln"])
    for c, f in [("_vol_ratio", _c_vol), ("_rel_wk_dist", _c_relwk), ("_10madist", _c_madist), ("_20madist", _c_madist),
                 ("_rel_tightness_today", _c_tight), ("_rel_tightness_prev", _c_tight), ("_chg_percentclose", _c_chg),
                 ("Rating", _c_rating), ("_circuit", _c_circuit)]:
        if c in cols:
            sty = sty.map(f, subset=[c])
    if "Label" in cols:
        sty = sty.map(lambda v: SITUATION_COLOURS.get(str(v), ""), subset=["Label"])
    fmt = {c: "{:.1f}" for c in ["_chg_percentclose", "_vol_ratio", "Adr", "_rel_wk_dist", "_10madist", "_20madist"] if c in cols}
    fmt.update({c: "{:.0f}" for c in ["_circuit", "_avgvol_mln"] if c in cols})
    fmt.update({c: "{:.2f}" for c in ["_rel_tightness_today", "_rel_tightness_prev"] if c in cols})
    return sty.format(fmt, na_rep="")


def _multi_ok(st):
    """True if this Streamlit has the pick-list (multiselect) table column."""
    return hasattr(st.column_config, "MultiselectColumn")


def _tags_column(st, label="Tags", help_=None, extra=()):
    return st.column_config.MultiselectColumn(label, options=list(SETUP_TYPES) + list(extra), help=help_, width="medium")


def tags_of(row):
    """All setup tags of a watchlist entry (one entry per stock)."""
    t = row.get("setup_types")
    if isinstance(t, (list, tuple)) and len(t):
        return [x for x in t if x]
    return [row["setup_type"]] if row.get("setup_type") else []


def _age_days(row, today):
    s = row.get("last_trigger_date") or row.get("trigger_date") or row.get("added_date")
    if not s:
        return None
    try:
        return (today - pd.to_datetime(str(s)[:10]).date()).days
    except Exception:
        return None


def classify_tomorrow(scan_df, yesterday_list, watch_rows, cfg=None, today=None):
    """
    Anticipation / evening: give every stock one label and pre-tick tomorrow's list.
      scan_df         today's merged scan
      yesterday_list  symbols ticked on the last saved ANTICIPATION snapshot
      watch_rows      active watchlist rows (breakouts saved for later)
    returns (labelled_df, watch_updates)
      watch_updates: [{id, symbol, action: 'remove'|'seen', reason}]
    """
    cfg = {**TOMORROW_DEFAULTS, **(cfg or {})}
    today = today or date.today()
    d = add_derived(scan_df)
    d["Symbol"] = d["Symbol"].astype(str).str.strip().str.upper()
    d = d.drop_duplicates("Symbol").set_index("Symbol", drop=False)

    # one entry per symbol (a stock can sit under two tags; use the most recent trigger)
    watch = {}
    for r in sorted(watch_rows or [], key=lambda r: str(r.get("trigger_date") or r.get("added_date") or "")):
        watch[str(r["symbol"]).strip().upper()] = r
    d["On_List"] = d["Symbol"].isin(yesterday_list)
    d["Saved"] = d["Symbol"].isin(watch.keys())
    all_tags, stars = {}, {}
    for r in watch_rows or []:
        all_tags.setdefault(r["symbol"], []).extend(tags_of(r))
        if r.get("rating"):
            stars[r["symbol"]] = max(stars.get(r["symbol"], 0), int(r["rating"]))
    d["Tag"] = d["Symbol"].map(lambda s: " + ".join(sorted(all_tags.get(s, [])))
                               + (f" · {stars[s]}★" if s in stars else ""))
    d["Label"] = "SCANNED"
    d["Reason"] = ""
    d["CONT"] = False
    d["Rating_n"] = 0.0

    chg = d["_chg_percentclose"].fillna(0)
    vol = d["_vol_ratio"].fillna(0)
    adr = d["Adr"].fillna(0)
    broke_out = chg >= cfg["bo_min_chg"]
    too_far = (adr > 0) & (chg > cfg["gap_max_adr"] * adr)

    def put(mask, label, reason):
        m = mask & (d["Label"] == "SCANNED")
        d.loc[m, "Label"] = label
        d.loc[m, "Reason"] = reason if isinstance(reason, str) else reason[m]

    c1, v1 = chg.round(1).astype(str), vol.round(1).astype(str)
    if "Data_Date" in d.columns:
        stale = d["Data_Date"].map(lambda x: x is not None and not pd.isna(x) and x < today)
        put(stale, "CHECK", "Not updated yet — still " + d["Data_Date"].astype(str) + " data. Refresh later.")
    put(d["On_List"] & too_far, "5_REMOVE", "Ran " + c1 + "% (> " + str(cfg["gap_max_adr"]) + " ADR). Skip.")
    put(d["On_List"] & broke_out & (vol >= cfg["bo_min_vol"]), "1_BUY_SIGNAL", "On list, +" + c1 + "% on " + v1 + "x vol")
    put(d["On_List"] & broke_out & (vol < cfg["bo_min_vol"]), "3_WAIT", "Broke out on only " + v1 + "x vol. Keep waiting.")
    put(~d["On_List"] & ~d["Saved"] & broke_out & (vol >= cfg["bo_min_vol"]), "2_SAVE",
        "Off-list breakout +" + c1 + "% on " + v1 + "x vol — tag it in Post Breakout")

    # Saved breakouts: re-setup (4) or remove (5)
    updates = []
    for sym, row in watch.items():
        if sym not in d.index:
            continue
        wid = row.get("id")
        if d.at[sym, "Label"] != "SCANNED":
            updates.append({"id": wid, "symbol": sym, "action": "seen"})
            continue
        r = d.loc[sym]
        circ = _num(r.get("_circuit"))
        if cfg.get("circuit_check") and np.isfinite(circ) and 0 < circ < cfg["min_circuit"]:
            d.at[sym, "Label"], d.at[sym, "Reason"] = "5_REMOVE", f"Price band cut to {circ:g}% — can lock in circuit, stop may not fill"
            updates.append({"id": wid, "symbol": sym, "action": "remove", "reason": "circuit"})
            continue
        last, prev_close, bo_close = _num(r["Last"]), _num(row.get("prev_close")), _num(row.get("trigger_close"))
        radr = _num(r["Adr"], 0)
        d10, d20 = abs(_num(r["_10madist"], 99)), abs(_num(r["_20madist"], 99))
        rt = _num(r["_rel_tightness_today"], 99)
        saved_on_bo = _num(row.get("chg_pct"), 0) >= cfg["bo_min_chg"]
        if saved_on_bo and rt > cfg["max_reltight"] and np.isfinite(prev_close) and np.isfinite(last) and last < prev_close:
            d.at[sym, "Label"], d.at[sym, "Reason"] = "5_REMOVE", f"Failed: closed {last:g} below pre-breakout close {prev_close:g}"
            updates.append({"id": wid, "symbol": sym, "action": "remove", "reason": "failed"})
            continue
        tags_txt = "+".join(tags_of(row))
        since = f", {((last / bo_close) - 1) * 100:+.0f}% since breakout" if np.isfinite(bo_close) and bo_close and np.isfinite(last) else ""
        d.at[sym, "Rating_n"] = _num(row.get("rating"), 0)
        # 1st priority: the same tightness rule as the Anticipation scan, wherever the price is (catches flags / HTFs).
        # Re-checked every evening, so a stock that keeps getting tighter keeps coming up.
        if rt <= cfg["max_reltight"]:
            d.at[sym, "Label"], d.at[sym, "Reason"] = "4_RESETUP", f"{tags_txt}: tight (rel tight {rt:.2f}){since}"
            if "CONTINUATION" not in all_tags.get(sym, []):
                d.at[sym, "CONT"] = True
        elif min(d10, d20) <= cfg["pullback_band"]:
            which = "10" if d10 <= d20 else "20"
            d.at[sym, "Label"], d.at[sym, "Reason"] = "4_RESETUP", f"{tags_txt}: pulled back to {which} MA ({min(d10, d20):.1f}% away){since}"
        else:
            age = _age_days(row, today)
            if age is not None and age >= cfg["stale_days"]:
                d.at[sym, "Label"], d.at[sym, "Reason"] = "5_REMOVE", f"Stale: {age} days, no re-setup"
                updates.append({"id": wid, "symbol": sym, "action": "remove", "reason": "stale"})
                continue
            d.at[sym, "Label"], d.at[sym, "Reason"] = "SAVED_WAIT", f"{tags_txt}: not tight yet (rel tight {rt:.2f}){since}"
        updates.append({"id": wid, "symbol": sym, "action": "seen"})

    # New tight candidates
    cand = ((d["Label"] == "SCANNED") & (adr >= cfg["min_adr"])
            & (d["_avgvol_mln"].fillna(0) >= cfg["min_liq"])
            & (d["_rel_tightness_today"].fillna(99) <= cfg["max_reltight"])
            & chg.between(cfg["min_chg"], cfg["max_chg"]))
    weekly_ok = (d["_rel_wk_dist"] <= cfg["max_rwd"]) & (d["W_TightCloses_10w"] >= cfg["min_wktight"])
    put(cand & weekly_ok, "CANDIDATE",
        "Rel tight " + d["_rel_tightness_today"].round(2).astype(str) + ", RS " + d["Avg_RS"].round(0).astype("Int64").astype(str))
    # Safety net: broke out in the last few days (tagged or not) and is getting tight again — a flag after the breakout.
    # These sit further from the 10w line than fresh coils, so they get their own (wider) limit.
    dsb = pd.to_numeric(d.get("_days_since_bo"), errors="coerce") if "_days_since_bo" in d.columns else pd.Series(np.nan, index=d.index)
    rwd_now = pd.to_numeric(d["_rel_wk_dist"], errors="coerce")
    recent = ((d["Label"] == "SCANNED") & dsb.between(1, cfg["recent_bo_days"]) & (adr >= cfg["min_adr"])
              & (d["_avgvol_mln"].fillna(0) >= cfg["min_liq"])
              & (d["_rel_tightness_today"].fillna(99) <= cfg["max_reltight"])
              & chg.between(cfg["min_chg"], cfg["max_chg"])
              & (rwd_now <= cfg["recent_bo_max_rwd"]))
    put(recent, "RECENT_BO",
        "Broke out " + dsb.fillna(0).astype(int).astype(str) + "d ago, now tight (rel tight "
        + d["_rel_tightness_today"].round(2).astype(str) + ", " + rwd_now.round(1).astype(str)
        + " ADR from 10w) — grade the chart; save as CONTINUATION if A/B")
    # Data patterns: tag every row; scanned names that match but aren't on the list for another reason get a row
    pat = {k: p["mask"](d, cfg) for k, p in DATA_PATTERNS.items()}
    names = pd.Series([[] for _ in range(len(d))], index=d.index)
    for k, m in pat.items():
        for i in d.index[m]:
            names[i] = names[i] + [DATA_PATTERNS[k]["name"]]
    d["Patterns"] = names.map(lambda xs: ", ".join(xs))
    any_pat = d["Patterns"] != ""
    put(any_pat & (adr >= cfg["min_adr"]) & (d["_avgvol_mln"].fillna(0) >= cfg["min_liq"]) & ~d["Missing_Weekly"], "PATTERN",
        d["Patterns"] + ": " + d["_rel_wk_dist"].round(2).astype(str) + " ADR from 10w, rel tight "
        + d["_rel_tightness_today"].round(2).astype(str) + ", 10/20MA "
        + (d["_10madist"] / adr.replace(0, np.nan)).round(1).astype(str) + "/"
        + (d["_20madist"] / adr.replace(0, np.nan)).round(1).astype(str) + " ADR")
    alert = any_pat & (vol >= cfg["lp_alert_vol"]) & (chg > 0)
    d.loc[alert, "Reason"] = ("⚡ TRADE ALERT · " + d.loc[alert, "Patterns"] + " + volume " + v1[alert] + "x — "
                              + d.loc[alert, "Reason"].fillna("").astype(str))
    put(cand & d["Missing_Weekly"], "CHECK", "Passes daily rules, weekly data missing — check chart")
    d.loc[d["On_List"] & (d["Label"] == "SCANNED"), "Reason"] = "Was on list, no longer qualifies"

    # Listed / saved stocks missing from today's scan — never silently disappear
    missing = []
    for sym in sorted(set(yesterday_list) | set(watch.keys())):
        if sym in d.index:
            continue
        row = watch.get(sym)
        age = _age_days(row, today) if row else None
        seen = str((row or {}).get("last_seen") or "")[:10]
        gone = None
        if seen:
            try:
                gone = int(np.busday_count(date.fromisoformat(seen), today))
            except Exception:
                gone = None
        if row and age is not None and age >= cfg["stale_days"]:
            label, reason = "5_REMOVE", f"Not in scan, {age} days old — suggest remove"
            updates.append({"id": row.get("id"), "symbol": sym, "action": "remove", "reason": "stale"})
        elif row and gone is not None and gone >= cfg["missing_days"]:
            label, reason = "5_REMOVE", f"Dropped out of the scan — last seen {seen} ({gone} trading days)"
            updates.append({"id": row.get("id"), "symbol": sym, "action": "remove", "reason": "dropped out"})
        else:
            label, reason = "CHECK", "Not in today's scan — check chart"
        missing.append({"Symbol": sym, "Label": label, "Reason": reason, "On_List": sym in yesterday_list,
                        "Saved": row is not None, "Tag": " + ".join(tags_of(row)) if row else "", "Scan_Count": 0})
    if missing:
        d = pd.concat([d, pd.DataFrame(missing).set_index("Symbol", drop=False)])

    d["CONT"] = d["CONT"].fillna(False).astype(bool)
    d["Rank"] = np.nan
    cm = d["Label"] == "CANDIDATE"
    d["Tier"] = adr_tier(d["Adr"], cfg) if "Adr" in d.columns else ""
    ranked = d[cm].assign(_t=d.loc[cm, "Tier"].replace("", "P9")).sort_values(
        ["_t", "_rel_tightness_today", "Avg_RS"], ascending=[True, True, False], na_position="last")
    d.loc[ranked.index, "Rank"] = range(1, len(ranked) + 1)
    d["Keep"] = d["Label"].isin(TOMORROW_LABELS) | (cm & (d["Rank"] <= cfg["list_size"]))
    order = {k: i for i, k in enumerate(LIST_LABELS)}
    d["_o"] = d["Label"].map(order)
    d["_rt"] = d["_rel_tightness_today"].fillna(99)
    d = d.sort_values(["_o", "Rating_n", "_rt", "Rank"], ascending=[True, False, True, True],
                      na_position="last").drop(columns=["_o", "_rt"]).reset_index(drop=True)
    return d, updates


# ── Data patterns: named, rule-based shapes in the scan columns. Each has a mask (no volume) and is shown
#    as a filter checkbox next to P1/P2/P3. Add new ones to DATA_PATTERNS.
def _pnum(d, c):
    return pd.to_numeric(d[c], errors="coerce") if c in d.columns else pd.Series(np.nan, index=d.index)


def launch_pad_mask(d, cfg):
    """10W Launch Pad: ≤ lp_max_rwd ADR from the 10w line, rel tight ≤ lp_max_reltight, 10MA and 20MA dist each
    between lp_ma_min_adr and lp_ma_max_adr ADRs, day's move −0.5 to lp_max_chg_adr ADR (not already broken out)."""
    adr = _pnum(d, "Adr").replace(0, np.nan)
    m10, m20 = _pnum(d, "_10madist") / adr, _pnum(d, "_20madist") / adr
    lo, hi = cfg["lp_ma_min_adr"], cfg["lp_ma_max_adr"]
    return ((_pnum(d, "_rel_wk_dist") <= cfg["lp_max_rwd"]) & (_pnum(d, "_rel_tightness_today") <= cfg["lp_max_reltight"])
            & m10.between(lo, hi) & m20.between(lo, hi)
            & (_pnum(d, "_chg_percentclose") / adr).between(-0.5, cfg["lp_max_chg_adr"])).fillna(False)


DATA_PATTERNS = {
    "LP10W": {"name": "10W Launch Pad", "mask": launch_pad_mask,
              "help": lambda c: (f"On the 10-week line (≤ {c['lp_max_rwd']:g} ADR), tight (rel tight ≤ {c['lp_max_reltight']:g}), "
                                 f"sitting on the 10/20 MA ({c['lp_ma_min_adr']:g} to +{c['lp_ma_max_adr']:g} ADR), "
                                 f"not broken out yet (day's move ≤ {c['lp_max_chg_adr']:g} ADR). Volume isn't required — "
                                 f"when it comes in (≥ {c['lp_alert_vol']:g}x on an up day) the Reason shows a ⚡ trade alert.")},
}


def suggest_tags(r, cfg, saved_as=None):
    """Every setup type the scan columns point to (a breakout can be TIGHT_BO and WEMA_BO). Confirm on the chart."""
    chg, vol = _num(r.get("_chg_percentclose"), 0), _num(r.get("_vol_ratio"), 0)
    rtp = _num(r.get("_rel_tightness_prev"), 99)
    d10, d20 = _num(r.get("_10madist"), -1), _num(r.get("_20madist"), -1)
    rwd = _num(r.get("_rel_wk_dist"), 99)
    tags, why = [], []
    if chg >= cfg["ep_min_chg"] and vol >= cfg["ep_min_vol"]:
        tags.append("EP"); why.append(f"EP: +{chg:.1f}% on {vol:.1f}x vol — confirm the gap")
    if rtp <= cfg["tight_max_reltight"] and d10 > 0 and d20 > 0:
        tags.append("TIGHT_BO"); why.append(f"TIGHT: tight before (rel {rtp:.2f}), above 10 & 20 — confirm both rising")
    if rwd <= cfg["wema_max_adr"]:
        tags.append("WEMA_BO"); why.append(f"WEMA: {rwd:.1f} ADR from 10w EMA")
    dsb = _num(r.get("_days_since_bo"), 0)
    prev = sorted(t for t in (saved_as or set()) if t != "CONTINUATION")
    if prev:
        tags.append("CONTINUATION"); why.append(f"CONT: already saved as {'+'.join(prev)} — breaking out again")
    elif 2 <= dsb <= cfg["cont_max_days"]:
        tags.append("CONTINUATION"); why.append(f"CONT: earlier breakout {dsb:.0f} days ago — confirm on the chart")
    return tags, ("; ".join(why) if why else "no clear setup — check the chart")


def find_breakouts(scan_df, yesterday_list, watch_rows, cfg=None, today=None):
    """Post Breakout: today's breakouts, split Strong / Moderate, one tick column per setup type (pre-ticked from
    the suggestion, never for a tag it's already saved under)."""
    cfg = {**BREAKOUT_DEFAULTS, **(cfg or {})}
    d = add_derived(scan_df).drop_duplicates("Symbol")
    fresh = pd.Series(True, index=d.index)
    if today is not None and "Data_Date" in d.columns:
        fresh = d["Data_Date"].map(lambda x: x is None or pd.isna(x) or x >= today)
    bo = d[fresh & (d["_chg_percentclose"].fillna(0) >= cfg["bo_min_chg"]) & (d["_vol_ratio"].fillna(0) >= cfg["bo_min_vol"])].copy()
    active, earlier, rated = {}, {}, {}
    for r in watch_rows or []:
        active.setdefault(r["symbol"], set()).update(tags_of(r))
        # CONTINUATION needs an EARLIER breakout — a tag saved for this same session doesn't count
        td = str(r.get("trigger_date") or r.get("added_date") or "")[:10]
        if today is None or (td and td < today.isoformat()):
            earlier.setdefault(r["symbol"], set()).update(tags_of(r))
        if r.get("rating"):
            rated[r["symbol"]] = max(rated.get(r["symbol"], 0), int(r["rating"]))
    sug = [suggest_tags(r, cfg, earlier.get(r["Symbol"])) for _, r in bo.iterrows()]
    bo["Suggested"] = [" + ".join(t) if t else NO_TAG for t, _ in sug]
    bo["Why"] = [w for _, w in sug]
    for t in SETUP_TYPES:
        bo[t] = [(t in tags) and (t not in active.get(sym, set())) for (tags, _), sym in zip(sug, bo["Symbol"])]
    bo["On_List"] = bo["Symbol"].isin(yesterday_list)
    bo["In_Watchlist"] = bo["Symbol"].map(lambda s: ", ".join(sorted(active.get(s, []))))
    bo["Rating"] = bo["Symbol"].map(lambda s: rating_label(rated.get(s)))
    bo["Skip"] = True     # safe default: nothing is saved unless you untick Skip for that stock
    bo["Batch"] = np.where(bo["_vol_ratio"].fillna(0) >= cfg["strong_min_vol"], "Strong", "Moderate")
    return bo.sort_values("_vol_ratio", ascending=False).reset_index(drop=True)


def breakout_batch_lists(base_df, mcfg, bo_min_chg, bo_min_vol, bo_prefs=None):
    """(strong, moderate) symbol lists — exactly the Tag breakouts panel's batches, no database needed."""
    if base_df is None or base_df.empty:
        return [], []
    cfg = {**BREAKOUT_DEFAULTS, **(bo_prefs or {}), "bo_min_chg": bo_min_chg, "bo_min_vol": bo_min_vol}
    bo = find_breakouts(base_df, set(), [], cfg, today=effective_session(base_df, mcfg)[0])
    if bo.empty:
        return [], []
    return bo[bo["Batch"] == "Strong"]["Symbol"].tolist(), bo[bo["Batch"] == "Moderate"]["Symbol"].tolist()


def _clean_metrics(r, cols):
    m = {}
    for c in cols:
        if c in r.index:
            v = r[c]
            if isinstance(v, np.generic):
                v = v.item()
            if v is None or (isinstance(v, float) and not np.isfinite(v)):
                continue
            m[c] = v
    return m


def snapshot_rows(df, snap_date, mcfg, scanner, list_col, selected_col):
    rows = []
    for _, r in df.iterrows():
        m = _clean_metrics(r, SNAPSHOT_METRICS)
        for k in ("Reason", "Why"):
            if k in r.index and isinstance(r[k], str) and r[k]:
                m[k.lower()] = r[k]
        if "On_List" in r.index:
            m["on_list"] = bool(r["On_List"])
        if "Rating" in r.index and rating_int(r["Rating"]):
            m["rating"] = rating_int(r["Rating"])
        rows.append({"user_id": mcfg["user_id"], "market": mcfg["market"], "exchange": mcfg["exchange"],
                     "scanner": scanner, "snap_date": snap_date.isoformat(), "symbol": r["Symbol"],
                     "list": str(r[list_col]), "selected": bool(r[selected_col]),
                     "scan_count": int(_num(r.get("Scan_Count"), 0)), "metrics": m})
    return rows


def breakout_payload(r, mcfg):
    last, chg = _num(r.get("Last")), _num(r.get("_chg_percentclose"), 0)
    p = {"exchange": mcfg["exchange"],
         "trigger_close": round(last, 2) if np.isfinite(last) else None,
         "prev_close": round(last / (1 + chg / 100), 2) if np.isfinite(last) else None,
         "chg_pct": round(chg, 2),
         "vol_ratio": _num(r.get("_vol_ratio"), None), "adr": _num(r.get("Adr"), None),
         "rel_tightness": _num(r.get("_rel_tightness_prev"), None),
         "dist_10wema_pct": _num(r.get("W_Dist10wMA"), None),
         "dist_10ma_pct": _num(r.get("_10madist"), None), "dist_20ma_pct": _num(r.get("_20madist"), None),
         "scan_count": int(_num(r.get("Scan_Count"), 0)),
         "sector": r.get("Sector") if isinstance(r.get("Sector"), str) else None,
         "why": r.get("Why"), "rating": rating_int(r.get("Rating"))}
    return {k: v for k, v in p.items() if v is not None}


# ════════════════════════════════════════════════════════════════════════════
# Database
# ════════════════════════════════════════════════════════════════════════════
# Every click in Streamlit reruns the page, and each rerun used to make 7-8 trips to Supabase.
# CachedClient remembers read results (for DB_CACHE_TTL seconds) and forgets them all on any write,
# so reruns are served from memory and saves are never shown stale.
DB_CACHE_TTL = 300
_DB_CACHE = {}


def clear_db_cache():
    _DB_CACHE.clear()


class _CachedQuery:
    def __init__(self, sb, head):
        self._sb, self._calls = sb, [head]

    def __getattr__(self, name):
        def rec(*a, **k):
            self._calls.append((name, a, k)); return self
        return rec

    def _replay(self):
        (kind, name, a0, k0), rest = self._calls[0], self._calls[1:]
        obj = getattr(self._sb, kind)(name, *a0, **k0)
        for name_, a, k in rest:
            obj = getattr(obj, name_)(*a, **k)
        return obj

    def execute(self):
        head = self._calls[0]
        is_read = (head[0] == "table" and not any(c[0] in ("insert", "update", "upsert", "delete") for c in self._calls[1:])) \
            or (head[0] == "rpc" and str(head[1]).startswith("get_"))
        if not is_read:
            res = self._replay().execute()
            clear_db_cache()
            return res
        key = repr(self._calls)
        hit = _DB_CACHE.get(key)
        if hit and time.time() - hit[0] < DB_CACHE_TTL:
            return _Res(copy.deepcopy(hit[1]))          # a copy, so callers can't change the cached rows
        data = self._replay().execute().data
        _DB_CACHE[key] = (time.time(), data)
        return _Res(copy.deepcopy(data))


class _Res:
    def __init__(self, data):
        self.data = data


class CachedClient:
    """Drop-in wrapper for the supabase client: same .table(...) / .rpc(...) calls, cached reads."""
    def __init__(self, sb):
        self._sb = sb

    def table(self, name):
        return _CachedQuery(self._sb, ("table", name, (), {}))

    def rpc(self, fn, params=None):
        return _CachedQuery(self._sb, ("rpc", fn, (params or {},), {}))


def cached_client(sb):
    return None if sb is None else (sb if isinstance(sb, CachedClient) else CachedClient(sb))


def load_active_watchlist(sb, mcfg):
    return (sb.table("watchlist").select("*").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
            .eq("status", "active").execute().data)


def load_last_list(sb, mcfg, before_date=None, scanner="ANTICIPATION"):
    """(date, symbols) of the most recent saved tomorrow's list (strictly before `before_date` if given)."""
    q = (sb.table("daily_snapshots").select("snap_date").eq("user_id", mcfg["user_id"])
         .eq("market", mcfg["market"]).eq("scanner", scanner).eq("selected", True))
    if before_date:
        q = q.lt("snap_date", before_date.isoformat())
    r = q.order("snap_date", desc=True).limit(1).execute()
    if not r.data:
        return None, []
    last = r.data[0]["snap_date"]
    rows = (sb.table("daily_snapshots").select("symbol,metrics").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
            .eq("scanner", scanner).eq("snap_date", last).eq("selected", True).execute().data)
    return last, _tightest_first(rows)


def _tightest_first(rows):
    """Symbols ordered by relative tightness on the day saved (tightest first); no tightness → last, A–Z."""
    rt = {}
    for x in rows:
        v = _num((x.get("metrics") or {}).get("_rel_tightness_today"), np.nan)
        rt[x["symbol"]] = v if np.isfinite(v) else np.inf
    return sorted(rt, key=lambda s: (rt[s], s))


def sort_by_tightness(df, col="_rel_tightness_today"):
    """Tightest first (rel tightness = NR4 range ÷ ADR); rows without it go to the bottom."""
    if df is None or df.empty or col not in df.columns:
        return df
    k = pd.to_numeric(df[col], errors="coerce")
    return df.assign(_k=k.fillna(np.inf)).sort_values(["_k", "Symbol"], kind="stable").drop(columns="_k")


def upsert_snapshots(sb, rows):
    for i in range(0, len(rows), 500):
        sb.table("daily_snapshots").upsert(rows[i:i + 500], on_conflict="user_id,market,scanner,snap_date,symbol").execute()


def save_tomorrow(sb, labelled, updates, snap_date, mcfg):
    upsert_snapshots(sb, snapshot_rows(labelled, snap_date, mcfg, "ANTICIPATION", "Label", "Keep"))
    removed = 0
    seen = [u["id"] for u in updates if u["action"] == "seen" and u.get("id")]
    if seen:
        sb.table("watchlist").update({"last_seen": snap_date.isoformat()}).in_("id", seen).execute()
    for _, r in labelled[labelled["Label"] == "4_RESETUP"].iterrows():
        sb.table("watchlist").update({"last_status": "RESETUP", "last_seen": snap_date.isoformat()}) \
            .eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"]).eq("symbol", r["Symbol"]).eq("status", "active").execute()
    for u in updates:
        if u["action"] == "remove" and u.get("confirmed") and u.get("id"):
            sb.rpc("remove_from_watchlist", {"p_id": u["id"], "p_reason": u.get("reason", "manual"),
                                             "p_status": "removed"}).execute()
            removed += 1
    return int(labelled["Keep"].sum()), removed


def save_breakouts(sb, bo, snap_date, mcfg):
    """Save every ticked tag (a row can have several). Tags it's already saved under are skipped."""
    added = 0
    lists = []
    for _, r in bo.iterrows():
        prior = [t for t in str(r.get("In_Watchlist", "")).split(", ") if t]
        if bool(r.get("Skip")):
            lists.append("SKIPPED"); continue
        ticked = [t for t in SETUP_TYPES if bool(r.get(t))]
        for t in ticked:
            if t in prior:
                continue
            sb.rpc("add_to_watchlist", {"p_user": mcfg["user_id"], "p_market": mcfg["market"], "p_symbol": r["Symbol"],
                                        "p_setup_type": t, "p_source": "BREAKOUT",
                                        "p_trigger_date": snap_date.isoformat(), "p_data": breakout_payload(r, mcfg),
                                        "p_tags": ["missed"] if bool(r.get("On_List")) else ["off-list"]}).execute()
            added += 1
        rt = rating_int(r.get("Rating"))
        if rt and prior:
            sb.table("watchlist").update({"rating": rt}).eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"]) \
                .eq("symbol", r["Symbol"]).eq("status", "active").execute()
        allt = sorted(set(prior) | set(ticked), key=SETUP_TYPES.index) if (prior or ticked) else []
        lists.append("+".join(t for t in allt if t in SETUP_TYPES) or "NOT_SAVED")
    snap = bo.copy()
    snap["_list"] = lists
    snap["_sel"] = ~snap["_list"].isin(["NOT_SAVED", "SKIPPED"])
    upsert_snapshots(sb, snapshot_rows(snap, snap_date, mcfg, "BREAKOUT", "_list", "_sel"))
    return added, len(bo)


def _snap_q(sb, mcfg, scanner, sd):
    return (sb.table("daily_snapshots").select("symbol,list,selected").eq("user_id", mcfg["user_id"])
            .eq("market", mcfg["market"]).eq("scanner", scanner).eq("snap_date", sd.isoformat()))


def load_today_selected(sb, mcfg, sd):
    """Symbols already put on tomorrow's list today (e.g. added from the scanner table)."""
    return {r["symbol"] for r in _snap_q(sb, mcfg, "ANTICIPATION", sd).eq("selected", True).execute().data}


def _one_row(base_df, sym):
    if base_df is None or base_df.empty or sym not in set(base_df["Symbol"]):
        return None
    return add_derived(base_df[base_df["Symbol"] == sym]).iloc[0]


def add_to_tomorrow(sb, symbols, base_df, sd, mcfg):
    """Put symbols on tomorrow's list straight away (today's ANTICIPATION snapshot, selected = true)."""
    existing = {r["symbol"] for r in _snap_q(sb, mcfg, "ANTICIPATION", sd).in_("symbol", list(symbols)).execute().data}
    for s in symbols:
        if s in existing:
            sb.table("daily_snapshots").update({"selected": True}).eq("user_id", mcfg["user_id"]) \
                .eq("market", mcfg["market"]).eq("scanner", "ANTICIPATION").eq("snap_date", sd.isoformat()) \
                .eq("symbol", s).execute()
    new = [s for s in symbols if s not in existing]
    if new:
        rows = []
        for s in new:
            r = _one_row(base_df, s)
            df = pd.DataFrame([r]) if r is not None else pd.DataFrame([{"Symbol": s}])
            df["Symbol"], df["Label"], df["Keep"], df["Reason"] = s, "ADDED", True, "Added from the scanner"
            rows += snapshot_rows(df, sd, mcfg, "ANTICIPATION", "Label", "Keep")
        upsert_snapshots(sb, rows)
    return len(symbols)


def save_symbols_to_watchlist(sb, symbols, tag, base_df, sd, mcfg, source="BREAKOUT"):
    """Save symbols under one setup tag straight from the scanner table. Skips ones already saved with that tag."""
    active = {(r["symbol"], t) for r in load_active_watchlist(sb, mcfg) for t in tags_of(r)}
    added, skipped, snap = 0, [], []
    for s in symbols:
        if (s, tag) in active:
            skipped.append(s); continue
        r = _one_row(base_df, s)
        data = breakout_payload(r, mcfg) if r is not None else {"exchange": mcfg["exchange"]}
        sb.rpc("add_to_watchlist", {"p_user": mcfg["user_id"], "p_market": mcfg["market"], "p_symbol": s,
                                    "p_setup_type": tag, "p_source": source, "p_trigger_date": sd.isoformat(),
                                    "p_data": data, "p_tags": ["scanner" if r is not None else "manual"]}).execute()
        added += 1
        if r is not None:
            df = pd.DataFrame([r]); df["_list"], df["_sel"] = tag, True
            snap += snapshot_rows(df, sd, mcfg, "BREAKOUT", "_list", "_sel")
    if snap:
        upsert_snapshots(sb, snap)
    return added, skipped


def _parse_symbols(text):
    return [t.strip().upper() for t in (text or "").replace("\n", ",").replace(" ", ",").split(",") if t.strip()]


# ════════════════════════════════════════════════════════════════════════════
# Journal — market note (the gate) + running notes
# ════════════════════════════════════════════════════════════════════════════
# One table, market_journal. kind = MARKET (the daily market note, required before the scanner runs),
# SKIP (logged "skip today"), NOTE (running notes, any time). Times shown in Sydney time.
LOCAL_TZ = "Australia/Sydney"
JOURNAL_MIN_CHARS = 80
TREND_OPTS = ["Up", "Chop", "Down"]
REGIMES = {
    "Aggressive": {"max_gtts": 6, "risk": "full risk", "help": "Indices trending up, breadth strong, breakouts working"},
    "Normal":     {"max_gtts": 4, "risk": "full risk", "help": "Indices up or flat, breadth OK, mixed follow-through"},
    "Defensive":  {"max_gtts": 2, "risk": "half risk", "help": "Indices choppy or rolling over, breakouts failing"},
    "Cash":       {"max_gtts": 0, "risk": "no new trades", "help": "Indices down, breadth weak — tag breakouts only"},
}
INDEX_NAMES = {"NSE": ["Nifty 50", "Nifty 500"], "USA": ["S&P 500", "Nasdaq 100"]}


# ── Market breadth (stock counts, one row per session in market_breadth) ──
BREADTH_COLS = [("up45", "Up 4.5% today"), ("down45", "Down 4.5% today"),
                ("up20", "Up 20% in 5 days"), ("down20", "Down 20% in 5 days"),
                ("above20", "Above 20 DMA"), ("below20", "Below 20 DMA"),
                ("above50", "Above 50 DMA"), ("below50", "Below 50 DMA")]
BREADTH_BULL = {"up45", "up20", "above20", "above50"}     # high = green; the others high = red
BREADTH_KEYS = [k for k, _ in BREADTH_COLS]


def load_breadth(sb, mcfg):
    """All breadth rows for this market, newest first, as a DataFrame (empty if none)."""
    rows, start = [], 0
    while True:
        r = (sb.table("market_breadth").select("*").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
             .order("session_date", desc=True).range(start, start + 999).execute().data)
        rows += r
        if len(r) < 1000:
            break
        start += 1000
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["session_date"] + BREADTH_KEYS)
    df["session_date"] = pd.to_datetime(df["session_date"]).dt.date
    return df


def load_breadth_day(sb, mcfg, sd):
    r = (sb.table("market_breadth").select("*").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
         .eq("session_date", sd.isoformat()).limit(1).execute().data)
    return r[0] if r else None


def upsert_breadth(sb, mcfg, rows, source="manual"):
    """rows: [{'session_date': date, 'up45': int, ...}] — one per day; replaces that day's counts."""
    out = []
    for r in rows:
        row = {"user_id": mcfg["user_id"], "market": mcfg["market"], "source": source,
               "session_date": pd.Timestamp(r["session_date"]).date().isoformat(),
               "updated_at": datetime.utcnow().isoformat() + "Z"}
        for k in BREADTH_KEYS:
            v = r.get(k)
            row[k] = int(v) if v is not None and not pd.isna(v) else None
        out.append(row)
    for i in range(0, len(out), 500):
        sb.table("market_breadth").upsert(out[i:i + 500], on_conflict="user_id,market,session_date").execute()
    return len(out)


def _breadth_col_key(name):
    """Map a spreadsheet header (e.g. 'Stocks up 4.5 % in last session', 'Stocks Below 20 DMA(LeADING)') to a key."""
    s = " ".join(str(name).lower().replace("%", " % ").split())
    if "date" in s:
        return "session_date"
    down = any(w in s for w in ("down", "below", "under"))
    if "4.5" in s:
        return "down45" if down else "up45"
    if "dma" in s or "sma" in s or "ma" in s.split():
        if "50" in s:
            return "below50" if down else "above50"
        if "20" in s:
            return "below20" if down else "above20"
    if "20" in s and "%" in s:
        return "down20" if down else "up20"
    return None


def parse_breadth_file(f):
    """CSV / XLSX / XLS with a date column + the 8 counts → (DataFrame, list of unmatched headers)."""
    name = getattr(f, "name", "").lower()
    if name.endswith(".csv"):
        raw = pd.read_csv(f)
    else:
        raw = pd.read_excel(f)
    raw = raw.dropna(how="all")
    if "session_date" not in [_breadth_col_key(c) for c in raw.columns] and len(raw.columns):
        raw = raw.rename(columns={raw.columns[0]: "Date"})          # first column holds the dates
    mapping, unmatched = {}, []
    for c in raw.columns:
        k = _breadth_col_key(c)
        if k and k not in mapping.values():
            mapping[c] = k
        else:
            unmatched.append(str(c))
    df = raw.rename(columns=mapping)[list(mapping.values())]
    df["session_date"] = pd.to_datetime(df["session_date"], dayfirst=True, errors="coerce").dt.date
    df = df.dropna(subset=["session_date"])
    for k in BREADTH_KEYS:
        df[k] = pd.to_numeric(df[k], errors="coerce").round() if k in df.columns else np.nan
    df = df.drop_duplicates("session_date", keep="first").sort_values("session_date", ascending=False)
    return df[["session_date"] + BREADTH_KEYS].reset_index(drop=True), unmatched


def _rgb_mix(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def _heat(t):
    """0 = red, 0.5 = yellow, 1 = green (Excel-style 3-colour scale)."""
    red, yel, grn = (248, 105, 107), (255, 235, 132), (99, 190, 123)
    r, g, b = _rgb_mix(red, yel, t / 0.5) if t < 0.5 else _rgb_mix(yel, grn, (t - 0.5) / 0.5)
    return f"background-color: rgb({r},{g},{b}); color: black"


def breadth_heatmap(df):
    """Styler: each column coloured on its own min–max over the rows shown; 'up/above' high = green, 'down/below' high = red."""
    show = df.rename(columns=dict(BREADTH_COLS))
    sty = show.style.format({lab: "{:.0f}" for _, lab in BREADTH_COLS}, na_rep="")
    for k, lab in BREADTH_COLS:
        v = pd.to_numeric(df[k], errors="coerce")
        lo, hi = v.min(), v.max()
        def col_style(col, lo=lo, hi=hi, bull=k in BREADTH_BULL):
            out = []
            for x in pd.to_numeric(col, errors="coerce"):
                if pd.isna(x) or not np.isfinite(hi - lo):
                    out.append("")
                    continue
                t = 0.5 if hi == lo else (x - lo) / (hi - lo)
                out.append(_heat(t if bull else 1 - t))
            return out
        sty = sty.apply(col_style, subset=[lab])
    return sty


def journal_session(mcfg):
    return session_date(market_now(mcfg), mcfg)


def fmt_local(ts):
    """ISO timestamp from the database → 'Tue 30 Sep 8:10 PM' in Sydney time."""
    if not ts:
        return ""
    try:
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        if ZoneInfo:
            t = t.tz_convert(ZoneInfo(LOCAL_TZ))
        return t.strftime("%a %d %b %I:%M %p").replace(" 0", " ")
    except Exception:
        return str(ts)[:16]


def note_symbols(text, known=()):
    """Stocks a note talks about: #ICIL / $ICIL always; a plain CAPS word only if it's a known symbol."""
    import re
    known = {str(k).upper() for k in known}
    out = []
    for tok in re.findall(r"[#$]?[A-Za-z][A-Za-z0-9&\-]{1,19}", text or ""):
        if tok[0] in "#$":
            s = tok[1:].upper()
        elif tok.isupper() and tok in known:
            s = tok
        else:
            continue
        if s not in out:
            out.append(s)
    return out


def _jq(sb, mcfg):
    return sb.table("market_journal").select("*").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])


def load_market_note(sb, mcfg, sd):
    r = _jq(sb, mcfg).eq("session_date", sd.isoformat()).in_("kind", ["MARKET", "SKIP"]).limit(1).execute().data
    return r[0] if r else None


def load_prev_market_note(sb, mcfg, sd):
    r = (_jq(sb, mcfg).lt("session_date", sd.isoformat()).in_("kind", ["MARKET", "SKIP"])
         .order("session_date", desc=True).limit(1).execute().data)
    return r[0] if r else None


def load_notes(sb, mcfg, limit=300):
    return _jq(sb, mcfg).order("created_at", desc=True).limit(limit).execute().data


def save_market_note(sb, mcfg, sd, kind, body, fields, symbols=(), existing=None):
    row = {"kind": kind, "body": body, "fields": fields, "symbols": list(symbols),
           "updated_at": datetime.utcnow().isoformat() + "Z"}
    if existing:
        sb.table("market_journal").update(row).eq("id", existing["id"]).execute()
    else:
        sb.table("market_journal").insert({**row, "user_id": mcfg["user_id"], "market": mcfg["market"],
                                           "session_date": sd.isoformat()}).execute()


def add_note(sb, mcfg, text, symbols):
    sb.table("market_journal").insert({"user_id": mcfg["user_id"], "market": mcfg["market"],
                                       "session_date": journal_session(mcfg).isoformat(), "kind": "NOTE",
                                       "body": text.strip(), "symbols": list(symbols), "fields": {}}).execute()


def latest_note_by_symbol(sb, mcfg):
    """{symbol: 'note text · Tue 30 Sep 8:10 PM'} — the newest note that mentions each stock."""
    out = {}
    try:
        rows = load_notes(sb, mcfg)
    except Exception:
        return out
    for r in rows:                                   # newest first
        for s in (r.get("symbols") or []):
            if s not in out:
                body = (r.get("body") or "").replace("\n", " ")
                out[s] = f"{body[:90]}{'…' if len(body) > 90 else ''} · {fmt_local(r.get('created_at'))}"
    return out


def regime_limits(note):
    """(regime name, REGIMES entry) from a market note, or (None, None)."""
    if not note or note.get("kind") != "MARKET":
        return None, None
    name = (note.get("fields") or {}).get("regime")
    return (name, REGIMES.get(name)) if name in REGIMES else (None, None)


def save_auto_breadth(sb, mcfg, sd, base_df, bo_min_chg=2.0, bo_min_vol=1.5, strong_min_vol=3.5, max_reltight=0.8):
    """After a scan: store what the scan itself says about breadth next to the market note."""
    if sb is None or base_df is None or base_df.empty:
        return
    try:
        note = load_market_note(sb, mcfg, sd)
        if not note:
            return
        d = add_derived(base_df)
        chg, vol = d["_chg_percentclose"].fillna(0), d["_vol_ratio"].fillna(0)
        bo = (chg >= bo_min_chg) & (vol >= bo_min_vol)
        n = int(len(d))
        auto = {"scanned": n,
                "strong_bo": int((bo & (vol >= strong_min_vol)).sum()),
                "moderate_bo": int((bo & (vol < strong_min_vol)).sum()),
                "coiled": int(((d["_rel_tightness_today"] <= max_reltight) & chg.between(-1, 3)).sum()),
                "above_10ma": int((d["_10madist"] > 0).sum()),
                "above_20ma": int((d["_20madist"] > 0).sum()),
                "up_today": int((chg > 0).sum()),
                "down_today": int((chg < 0).sum()),
                "at": datetime.utcnow().isoformat() + "Z"}
        sb.table("market_journal").update({"auto": auto}).eq("id", note["id"]).execute()
    except Exception:
        pass


def _auto_line(auto):
    if not auto:
        return ""
    return (f"Scan breadth: {auto.get('strong_bo', 0)} strong + {auto.get('moderate_bo', 0)} moderate breakouts · "
            f"{auto.get('coiled', 0)} coiled · {auto.get('above_20ma', '–')} above 20MA · "
            f"{auto.get('up_today', '–')} up / {auto.get('down_today', '–')} down today (of {auto.get('scanned', 0)} scanned)")


def _note_summary(note):
    f = note.get("fields") or {}
    if note.get("kind") == "SKIP":
        return f"**Skipped** — {note.get('body', '')}"
    bits = [f"{k}: {f.get('trend_' + str(i))}" for i, k in enumerate(f.get("indices", [])) if f.get("trend_" + str(i))]
    b = f.get("breadth") or {}
    if any(b.get(k) is not None for k in BREADTH_KEYS):
        g = lambda k: "–" if b.get(k) is None else f"{b[k]:g}"
        bits.append(f"±4.5%: {g('up45')}/{g('down45')} · ±20% 5d: {g('up20')}/{g('down20')} · "
                    f"20DMA: {g('above20')}/{g('below20')} · 50DMA: {g('above50')}/{g('below50')}")
    else:
        for k, lab in [("above20", ">20DMA"), ("above50", ">50DMA"), ("adv_dec", "Adv/Dec"), ("hi_lo", "NH/NL")]:
            if f.get(k) not in (None, ""):
                bits.append(f"{lab} {f[k]}")
    if f.get("themes"):
        bits.append(f"Themes: {f['themes']}")
    return " · ".join(bits)


def _market_form(st, sb, mcfg, sd, existing=None, key="mkt", extra_known=()):
    f = (existing or {}).get("fields") or {}
    idx = INDEX_NAMES.get(mcfg["market"], ["Index 1", "Index 2"])
    with st.form(f"{key}_form", border=False):
        cols = st.columns(len(idx) + 1)
        trends = []
        for i, name in enumerate(idx):
            cur = f.get(f"trend_{i}")
            trends.append(cols[i].radio(f"{name} trend", TREND_OPTS, horizontal=True, key=f"{key}_t{i}",
                                        index=TREND_OPTS.index(cur) if cur in TREND_OPTS else None,
                                        help="Up = above a rising 10 & 20 EMA · Chop = sideways · Down = below them"))
        regime_names = list(REGIMES)
        regime = cols[-1].radio("Regime → exposure tonight", regime_names, key=f"{key}_reg",
                                index=regime_names.index(f["regime"]) if f.get("regime") in REGIMES else None,
                                help=" · ".join(f"{k}: max {v['max_gtts']} GTTs, {v['risk']}" for k, v in REGIMES.items()))
        st.markdown("**Market breadth — stock counts** (saved to the Breadth tab)")
        _iv = lambda x: int(x) if isinstance(x, (int, float)) and not pd.isna(x) else None
        try:
            _bd = load_breadth_day(sb, mcfg, sd) or {}
        except Exception:
            _bd = {}
        _prev = {"above20": f.get("above20"), "above50": f.get("above50"), **(f.get("breadth") or {}),
                 **{k: _bd.get(k) for k in BREADTH_KEYS if _bd.get(k) is not None}}
        breadth = {}
        for row in (BREADTH_COLS[:4], BREADTH_COLS[4:]):
            for c_, (k, lab) in zip(st.columns(4), row):
                breadth[k] = c_.number_input(lab, min_value=0, value=_iv(_prev.get(k)), step=1, key=f"{key}_b_{k}")
        themes = st.text_input("Leading themes / sectors", value=f.get("themes", ""), key=f"{key}_th",
                               placeholder="Defence, power, PSU banks")
        body = st.text_area(f"What the market is doing (at least {JOURNAL_MIN_CHARS} characters)",
                            value=(existing or {}).get("body", ""), height=140, key=f"{key}_body",
                            placeholder="Indices, breadth, what's working and failing, what would change your mind. "
                                        "CAPS or #SYMBOL links a stock, e.g. ICIL waiting for volume.")
        ok = st.form_submit_button("Save market note" if not existing else "Update market note", type="primary")
    if ok:
        missing = [n for n, v in zip(idx, trends) if not v]
        if missing or not regime:
            st.error("Pick the trend for " + ", ".join(missing or []) + (" and " if missing and not regime else "") +
                     ("the regime" if not regime else "") + ".")
        elif len(body.strip()) < JOURNAL_MIN_CHARS:
            st.error(f"Write a little more — {len(body.strip())}/{JOURNAL_MIN_CHARS} characters. The point is to think it through.")
        else:
            fields = {"indices": idx, "regime": regime, "breadth": breadth, "themes": themes.strip(),
                      **{f"trend_{i}": t for i, t in enumerate(trends)}}
            if any(v is not None for v in breadth.values()):
                try:
                    upsert_breadth(sb, mcfg, [{"session_date": sd, **breadth}])
                except Exception as e:
                    st.warning(f"Breadth not saved to the Breadth tab — run supabase_breadth.sql in Supabase. ({e})")
            try:
                known = {r["symbol"] for r in load_active_watchlist(sb, mcfg)}
            except Exception:
                known = set()
            known |= {str(x).upper() for x in extra_known}
            save_market_note(sb, mcfg, sd, "MARKET", body.strip(), fields, note_symbols(body, known), existing)
            st.rerun()


def render_market_gate(st, sb, mcfg, extra_known=()):
    """Market first. Returns True when today's market note (or a logged skip) exists — the scanner unlocks then."""
    if sb is None:
        st.error("Database not connected — the market note can't be checked, so the scanner stays open.")
        return True
    sd = journal_session(mcfg)
    try:
        note = load_market_note(sb, mcfg, sd)
    except Exception as e:
        st.error(f"Could not read the journal. Run supabase_journal.sql in Supabase first. ({e})")
        return True
    if note:
        name, lim = regime_limits(note)
        when = fmt_local(note.get("updated_at") or note.get("created_at"))
        if note.get("kind") == "SKIP":
            st.error(f"**Market note skipped for {sd}** ({when}) — reason: {note.get('body', '')}. Scanner unlocked.")
        else:
            st.success(f"**Market note done · {mcfg['market']} {sd}** · saved {when} AEST · Regime **{name}** → "
                       f"max **{lim['max_gtts']}** new GTTs, {lim['risk']}")
        if note.get("kind") == "SKIP":
            with st.expander("✍️ Write the market note now (replaces the skip) — trends, regime, breadth counts"):
                if note.get("auto"):
                    st.caption(_auto_line(note["auto"]))
                _market_form(st, sb, mcfg, sd, existing={**note, "fields": {}}, key="mkt_late", extra_known=extra_known)
        else:
            with st.expander("Today's market note — view / edit"):
                st.caption(_note_summary(note))
                if note.get("auto"):
                    st.caption(_auto_line(note["auto"]))
                st.markdown(note.get("body", ""))
                _market_form(st, sb, mcfg, sd, existing=note, key="mkt_edit", extra_known=extra_known)
        return True

    st.warning(f"**Market first** — write the {mcfg['market']} market note for **{sd}** to unlock the scanner.")
    try:
        prev = load_prev_market_note(sb, mcfg, sd)
    except Exception:
        prev = None
    if prev:
        st.caption(f"Last note ({prev.get('session_date')}): {_note_summary(prev)}")
        if prev.get("auto"):
            st.caption(_auto_line(prev["auto"]))
        if prev.get("body"):
            st.caption("“" + prev["body"][:400] + ("…”" if len(prev["body"]) > 400 else "”"))
    with st.container(border=True):
        _market_form(st, sb, mcfg, sd, key="mkt_new", extra_known=extra_known)
    with st.expander("Can't do it today? Skip (it's logged)"):
        with st.form("mkt_skip", border=False):
            why = st.text_input("Why are you skipping?", key="mkt_skip_why")
            if st.form_submit_button("Skip today and unlock the scanner"):
                if len(why.strip()) < 5:
                    st.error("Give a reason.")
                else:
                    save_market_note(sb, mcfg, sd, "SKIP", why.strip(), {})
                    st.rerun()
    return False


def render_quick_note(st, sb, mcfg, extra_known=()):
    """Notepad: one line, Enter, done. CAPS or #SYMBOL links the note to a stock."""
    if sb is None:
        return
    with st.form("quick_note", clear_on_submit=True, border=False):
        c1, c2 = st.columns([8, 1])
        txt = c1.text_input("Quick note", label_visibility="collapsed", key="qn_text",
                            placeholder="Quick note — e.g. ICIL want to buy, volume not coming in  (CAPS or #SYMBOL links it to the stock)")
        ok = c2.form_submit_button("Add note", use_container_width=True)
    if ok and txt.strip():
        try:
            known = {r["symbol"] for r in load_active_watchlist(sb, mcfg)} | {str(s).upper() for s in extra_known}
            syms = note_symbols(txt, known)
            add_note(sb, mcfg, txt, syms)
            st.toast("Note saved" + (f" · linked to {', '.join(syms)}" if syms else ""))
        except Exception as e:
            st.error(f"Note not saved: {e}")


def render_breadth_tab(st, sb, mcfg):
    st.subheader(f"Market breadth · {mcfg['market']}")
    if sb is None:
        st.error("Database not connected."); return
    try:
        df = load_breadth(sb, mcfg)
    except Exception as e:
        st.error(f"Could not read market_breadth. Run supabase_breadth.sql in the Supabase SQL Editor first. ({e})")
        return
    if df.empty:
        st.info("No breadth recorded yet. Enter today's counts in the market note, add a day below, or upload your history.")
    else:
        c1, c2 = st.columns([2, 5])
        n = c1.selectbox("Show", [20, 40, 60, 120, 250, "All"], index=2, key="br_n",
                         format_func=lambda x: f"Last {x} sessions" if x != "All" else "All")
        view = (df if n == "All" else df.head(int(n)))[["session_date"] + BREADTH_KEYS].copy()
        c2.caption(f"{len(df)} sessions recorded · {df['session_date'].min()} → {df['session_date'].max()} · "
                   "each column is coloured on its own range for the rows shown: green = strong breadth, red = weak "
                   "(for the down / below columns a high count is red).")
        view = view.rename(columns={"session_date": "Date"}).set_index("Date")
        st.dataframe(breadth_heatmap(view), use_container_width=True, height=min(38 * len(view) + 40, 900))
        st.download_button("Download breadth history (CSV)", view.rename(columns=dict(BREADTH_COLS)).to_csv().encode(),
                           file_name=f"market_breadth_{mcfg['market']}.csv", mime="text/csv")

    with st.expander("Add or correct a day"):
        with st.form("br_day", border=False):
            d = st.date_input("Session date", value=journal_session(mcfg), key="br_day_date")
            vals = {}
            for row in (BREADTH_COLS[:4], BREADTH_COLS[4:]):
                for c_, (k, lab) in zip(st.columns(4), row):
                    vals[k] = c_.number_input(lab, min_value=0, value=None, step=1, key=f"br_day_{k}")
            if st.form_submit_button("Save this day"):
                if all(v is None for v in vals.values()):
                    st.error("Enter at least one count.")
                else:
                    upsert_breadth(sb, mcfg, [{"session_date": d, **vals}])
                    st.success(f"Saved breadth for {d}."); st.rerun()

    with st.expander("Upload history (Excel or CSV)"):
        st.caption("One row per day: a date column plus the eight counts. Headers like your Excel sheet work "
                   "(e.g. 'Stocks up 4.5 % in last session', 'Stocks Below 20 DMA'). Dates are read day-first (5/05/2026).")
        up = st.file_uploader("Breadth file", type=["xls", "xlsx", "csv"], key="br_upload")
        if up is not None:
            try:
                new, unmatched = parse_breadth_file(up)
            except Exception as e:
                st.error(f"Could not read the file: {e}"); return
            missing = [lab for k, lab in BREADTH_COLS if new[k].isna().all()]
            st.markdown(f"**{len(new)} days found** · {new['session_date'].min()} → {new['session_date'].max()}")
            if missing:
                st.warning("No column found for: " + ", ".join(missing))
            if unmatched:
                st.caption("Ignored columns: " + ", ".join(unmatched))
            st.dataframe(new.head(10).rename(columns={"session_date": "Date", **dict(BREADTH_COLS)}),
                         hide_index=True, use_container_width=True)
            have = set(df["session_date"]) if not df.empty else set()
            overlap = int(new["session_date"].isin(have).sum())
            over = st.checkbox(f"Overwrite the {overlap} days already recorded", value=False, key="br_over",
                               disabled=overlap == 0)
            todo = new if over else new[~new["session_date"].isin(have)]
            if st.button(f"Upload {len(todo)} days", type="primary", disabled=len(todo) == 0, key="br_go"):
                n_ = upsert_breadth(sb, mcfg, todo.to_dict("records"), source="upload")
                st.success(f"Uploaded {n_} days."); st.rerun()


def render_journal_tab(st, sb, mcfg):
    st.subheader(f"Journal · {mcfg['market']}")
    if sb is None:
        st.error("Database not connected."); return
    c1, c2, c3 = st.columns([3, 2, 2])
    q = c1.text_input("Search", key="jr_q", placeholder="word or phrase")
    sym = c2.text_input("Stock", key="jr_sym", placeholder="e.g. ICIL").strip().upper()
    kinds = c3.multiselect("Show", ["MARKET", "NOTE", "SKIP"], default=["MARKET", "NOTE", "SKIP"], key="jr_k")
    try:
        rows = load_notes(sb, mcfg, limit=500)
    except Exception as e:
        st.error(f"Could not read the journal. Run supabase_journal.sql in Supabase first. ({e})"); return
    rows = [r for r in rows if r.get("kind") in kinds
            and (not q or q.lower() in (r.get("body") or "").lower() or q.lower() in str(r.get("fields") or "").lower())
            and (not sym or sym in (r.get("symbols") or []) or sym in (r.get("body") or "").upper())]
    st.caption(f"{len(rows)} entries · newest first · times in Sydney time")
    badge = {"MARKET": "🧭 Market", "NOTE": "📝 Note", "SKIP": "⏭ Skipped"}
    last_day = None
    for r in rows:
        day = r.get("session_date")
        if day != last_day:
            st.markdown(f"##### Session {day}")
            last_day = day
        head = f"**{fmt_local(r.get('created_at'))}** · {badge.get(r.get('kind'), r.get('kind'))}"
        if r.get("symbols"):
            head += " · " + ", ".join(f"`{s}`" for s in r["symbols"])
        if r.get("kind") == "MARKET":
            name, lim = regime_limits(r)
            head += f" · Regime **{name}**" if name else ""
        with st.container(border=True):
            st.markdown(head)
            if r.get("kind") in ("MARKET", "SKIP"):
                st.caption(_note_summary(r))
                if r.get("auto"):
                    st.caption(_auto_line(r["auto"]))
            st.markdown(r.get("body") or "")


# ════════════════════════════════════════════════════════════════════════════
# Streamlit panels
# ════════════════════════════════════════════════════════════════════════════
def render_quick_save(st, sb, selected, base_df, scan_mode, mcfg):
    """Right under the scanner table: save ticked rows (or typed symbols) in one click."""
    with st.container(border=True):
        st.markdown("**Save from the scanner** — tick rows in the table above, or type any other symbol you spotted")
        c1, c2 = st.columns([2, 3])
        typed = c1.text_input("Other symbols (comma separated)", key=f"qs_typed_{scan_mode}",
                              placeholder="e.g. HAL, BEL")
        syms = list(dict.fromkeys([str(s).upper() for s in (selected or []) if s] + _parse_symbols(typed)))
        c2.markdown(f"**{len(syms)} to save:** " + (", ".join(syms) if syms else "_none — tick rows above_"))
        if sb is None:
            st.error("Database not connected."); return
        sd = effective_session(base_df, mcfg, sb)[0]
        order = (["TOMORROW"] + SETUP_TYPES) if scan_mode == "Anticipation" else (SETUP_TYPES + ["TOMORROW"])
        labels = {"TOMORROW": "Add to tomorrow's list", **{t: f"Save as {t}" for t in SETUP_TYPES}}
        primary = "TOMORROW" if scan_mode == "Anticipation" else None
        for col, k in zip(st.columns(len(order)), order):
            kind = "primary" if (k == primary or (primary is None and k in SETUP_TYPES)) else "secondary"
            if col.button(labels[k], key=f"qs_{scan_mode}_{k}", type=kind, disabled=not syms, use_container_width=True):
                try:
                    if k == "TOMORROW":
                        n = add_to_tomorrow(sb, syms, base_df, sd, mcfg)
                        st.success(f"Added {n} to tomorrow's list ({sd}).")
                    else:
                        a, skip = save_symbols_to_watchlist(sb, syms, k, base_df, sd, mcfg,
                                                            "BREAKOUT" if scan_mode != "Anticipation" else "ANTICIPATION")
                        st.success(f"Saved {a} as {k}." + (f" Already saved: {', '.join(skip)}." if skip else ""))
                except Exception as ex:
                    st.error(f"Save failed: {ex}. Did you run supabase_migration.sql?")
def scan_data_date(base_df):
    """Newest trading date in the scan (each stock carries its own update time). Needs ≥5% of stocks on that date."""
    if base_df is None or base_df.empty or "Timestamp" not in base_df.columns:
        return None
    dates = pd.Series(parse_ts_dates(base_df["Timestamp"])).dropna()
    if len(dates) < max(3, 0.5 * len(base_df)):
        return None
    counts = dates.value_counts()
    ok = [d for d, n in counts.items() if n >= max(3, 0.05 * len(dates))]
    d = max(ok) if ok else counts.idxmax()
    return d if 2000 < d.year < 2100 else None


def not_updated(base_df, sd):
    """Symbols whose own timestamp is older than the session — MarketInOut hasn't refreshed them yet."""
    if base_df is None or base_df.empty or "Timestamp" not in base_df.columns or sd is None:
        return {}
    dd = parse_ts_dates(base_df["Timestamp"])
    out = {}
    for sym, x in zip(base_df["Symbol"], dd):
        if x is not None and not pd.isna(x) and x < sd:
            out[sym] = x
    return out


def stale_snapshot_date(sb, base_df, mcfg, clock):
    """If today's scan is identical to the last saved snapshot (same Last and Chg% for the same stocks),
    MarketInOut hasn't updated yet — return that snapshot's date."""
    if sb is None or base_df is None or base_df.empty:
        return None
    try:
        r = (sb.table("daily_snapshots").select("snap_date").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
             .lt("snap_date", clock.isoformat()).order("snap_date", desc=True).limit(1).execute().data)
        if not r:
            return None
        last = r[0]["snap_date"]
        rows = (sb.table("daily_snapshots").select("symbol,metrics").eq("user_id", mcfg["user_id"])
                .eq("market", mcfg["market"]).eq("snap_date", last).limit(3000).execute().data)
        prev = {x["symbol"]: x["metrics"] or {} for x in rows}
        now = base_df.drop_duplicates("Symbol").set_index("Symbol")
        same = total = 0
        for sym, m in prev.items():
            if sym not in now.index or "Last" not in m or "_chg_percentclose" not in m:
                continue
            total += 1
            a, b = _num(now.at[sym, "Last"]), _num(now.at[sym, "_chg_percentclose"])
            if abs(a - float(m["Last"])) < 1e-6 and abs(b - float(m["_chg_percentclose"])) < 1e-6:
                same += 1
        if total >= 5 and same / total >= 0.8:
            return pd.to_datetime(last).date()
    except Exception:
        return None
    return None


def effective_session(base_df, mcfg, sb=None):
    """(session date to record under, data date or None, clock session date)."""
    now = market_now(mcfg)
    clock = session_date(now, mcfg)
    dd = scan_data_date(base_df)
    if dd is None or dd >= clock:
        stale = stale_snapshot_date(sb, base_df, mcfg, clock)
        if stale:
            dd = stale
    return (dd or clock), dd, clock


def _header_time(st, mcfg, base_df=None, sb=None):
    now = market_now(mcfg)
    sd, dd, clock = effective_session(base_df, mcfg, sb)
    if dd and dd < clock:
        st.warning(f"The scan data is still from **{dd}** (same prices as that day's snapshot) — MarketInOut hasn't "
                   f"updated for {clock} yet. Anything you save now is recorded under {dd}. "
                   "Click Refresh Now in a while for today's numbers.")
    elif market_is_open(now, mcfg):
        st.info(f"{mcfg['market']} is open ({now:%H:%M}). Intraday numbers: volume ratio is only part of the day, "
                "so it reads low early on. Fine for adding breakouts you spot — the evening run records the close.")
    return sd


RULE_LABELS = {
    "min_liq": "New candidates: min avg value (cr)",
    "list_size": "New candidates to pre-tick",
    "gap_max_adr": "Skip a listed stock that ran more than N ADRs",
    "pullback_band": "Pullback distance to 10/20 MA (%)",
    "stale_days": "Suggest removing a saved stock after N days",
    "recent_bo_days": "Recent BO, now tight: broke out within N days",
    "recent_bo_max_rwd": "Recent BO, now tight: max ADRs from 10w",
    "lp_max_rwd": "10W Launch Pad: max ADRs from 10w", "lp_max_reltight": "10W Launch Pad: max rel tight",
    "lp_ma_min_adr": "10W Launch Pad: 10/20MA dist at least (ADRs, negative = below)",
    "lp_ma_max_adr": "10W Launch Pad: 10/20MA dist at most (ADRs)",
    "lp_max_chg_adr": "10W Launch Pad: max day's move (ADRs) — above this it has broken out",
    "lp_alert_vol": "10W Launch Pad: trade alert when vol x ≥ (up day)",
    "tier1_adr": "ADR tier P1: ADR at least", "tier2_adr": "ADR tier P2: ADR at least (below = P3)",
    "missing_days": "Suggest removing a saved stock missing from the scan for N trading days",
    "min_circuit": "NSE: suggest removing a saved stock whose price band falls below N%",
    "strong_min_vol": "Strong batch: min volume (x)",
    "ep_min_chg": "Suggest EP: min % up",
    "ep_min_vol": "Suggest EP: min volume (x)",
    "tight_max_reltight": "Suggest TIGHT_BO: max tightness before (NR4/ADR)",
    "wema_max_adr": "Suggest WEMA_BO: max ADRs from 10w EMA",
    "cont_max_days": "Suggest CONT: earlier breakout within N days",
}
SHARED_LABELS = {"min_adr": "Min ADR", "max_reltight": "Max rel tightness", "max_rwd": "Max ADRs from 10w",
                 "min_wktight": "Min weekly tight closes", "min_chg": "Min chg %", "max_chg": "Max chg %",
                 "bo_min_chg": "Breakout min chg %", "bo_min_vol": "Breakout min volume (x)"}


def _rules_editor(st, title, defaults, saved, key):
    cfg = {**defaults, **(saved or {})}
    with st.container(border=True):
        show = st.toggle(title, value=False, key=f"{key}_show")
        cols = st.columns(3) if show else []
        for i, (k, v) in enumerate(defaults.items() if show else []):
            with cols[i % 3]:
                if isinstance(v, int) and not isinstance(v, bool):
                    cfg[k] = int(st.number_input(RULE_LABELS.get(k, k), value=int(cfg[k]), step=1, key=f"{key}_{k}"))
                else:
                    cfg[k] = float(st.number_input(RULE_LABELS.get(k, k), value=float(cfg[k]), step=0.5, key=f"{key}_{k}"))
        if show:
            st.caption("Saved with 'Save filter settings'. Don't change these during the 8 weeks.")
    st.session_state[key] = cfg
    return cfg


def explain_candidate(lab, sym, cfg):
    """Rule-by-rule check of why a scanned stock is (or isn't) a new candidate on Tomorrow's list."""
    sym = str(sym).strip().upper()
    m = lab[lab["Symbol"].astype(str).str.upper() == sym]
    if m.empty:
        return None, f"**{sym}** isn't in today's scan at all — it didn't pass any MarketInOut screen (1M/3M/6M), so the list can't see it."
    r = m.iloc[0]
    def n(c):
        try:
            v = float(r.get(c)); return v if np.isfinite(v) else None
        except (TypeError, ValueError):
            return None
    sc, adr, liq, rt = n("Scan_Count"), n("Adr"), n("_avgvol_mln"), n("_rel_tightness_today")
    chg, rwd, wt = n("_chg_percentclose"), n("_rel_wk_dist"), n("W_TightCloses_10w")
    f = lambda v, p=2: "—" if v is None else f"{v:.{p}f}"
    rows = [
        ("ADR %", f(adr, 1), f"≥ {cfg['min_adr']:g}", adr is not None and adr >= cfg["min_adr"]),
        ("Avg vol (liquidity)", f(liq, 0), f"≥ {cfg['min_liq']:g}", liq is not None and liq >= cfg["min_liq"]),
        ("Rel tight (today's 3-day range ÷ ADR)", f(rt), f"≤ {cfg['max_reltight']:g}", rt is not None and rt <= cfg["max_reltight"]),
        ("Chg % today (not broken out yet)", f(chg, 1), f"{cfg['min_chg']:g} to {cfg['max_chg']:g}",
         chg is not None and cfg["min_chg"] <= chg <= cfg["max_chg"]),
        ("ADRs from 10w", f(rwd, 1), f"≤ {cfg['max_rwd']:g}", rwd is not None and rwd <= cfg["max_rwd"]),
        ("Weekly tight closes", f(wt, 0), f"≥ {cfg['min_wktight']:g}", wt is not None and wt >= cfg["min_wktight"]),
    ]
    dsb = n("_days_since_bo")
    rec = [
        ("Recent BO path: days since breakout", f(dsb, 0), f"1 to {cfg['recent_bo_days']:g}",
         dsb is not None and 1 <= dsb <= cfg["recent_bo_days"]),
        ("Recent BO path: ADRs from 10w", f(rwd, 1), f"≤ {cfg['recent_bo_max_rwd']:g}",
         rwd is not None and rwd <= cfg["recent_bo_max_rwd"]),
    ]
    tbl = pd.DataFrame(rows + rec, columns=["Rule", "Value", "Needs", "Pass"])
    lbl = str(r.get("Label", ""))
    if lbl == "CANDIDATE":
        msg = f"**{sym}** passes every rule — it's a New candidate (rank {f(n('Rank'), 0)})."
    elif lbl not in ("SCANNED", ""):
        msg = f"**{sym}** is already in the table as *{LABEL_NAMES.get(lbl, lbl)}*: {r.get('Reason', '')}"
    else:
        fails = [x[0] for x in rows if not x[3]]
        rfails = [x[0] for x in rows[0:4] + rec if not x[3]]   # recent-BO path skips 10w ≤ max and weekly closes
        msg = (f"**{sym}** is in the scan but fails — as a new candidate: " + "; ".join(fails) +
               " · as a recent breakout: " + "; ".join(rfails) +
               ". If the chart says otherwise, tick it in the scanner table and add it — it then shows here as *Added by you*.")
    return tbl, msg


def render_tomorrow_panel(st, sb, base_df, saved_prefs, mcfg, shared=None):
    """Anticipation mode: evening, once."""
    st.markdown("---")
    st.subheader("Tomorrow's list  ·  evening, once")
    if sb is None:
        st.error("Database not connected."); return
    sd = _header_time(st, mcfg, base_df, sb)
    shared = shared or {}
    own = {k: v for k, v in TOMORROW_DEFAULTS.items() if k not in shared}   # the rest come from the sidebar
    own.update(TIER_DEFAULTS.get(mcfg.get("market"), {}))
    cfg = {**_rules_editor(st, "More list rules (new candidates, skips, pullbacks, clean-up)", own,
                           saved_prefs.get("build_tomorrow"), "bt_cfg"), **shared}
    if shared:
        st.caption("From the sidebar: " + ", ".join(f"{SHARED_LABELS.get(k, k)} {v:g}" for k, v in shared.items()))
    cfg["circuit_check"] = mcfg.get("market") == "NSE"
    try:
        prev_date, ylist = load_last_list(sb, mcfg, before_date=sd)
        watch = load_active_watchlist(sb, mcfg)
    except Exception as e:
        st.error(f"Could not read the database. Did you run supabase_migration.sql? ({e})"); return
    st.caption(f"Session **{sd}** · compared with the list saved on **{prev_date or '—'}** ({len(ylist)} names) "
               f"and {len(watch)} saved breakouts.")

    lab, updates = classify_tomorrow(base_df, set(ylist), watch, cfg, sd)
    try:
        added_today = load_today_selected(sb, mcfg, sd)
    except Exception:
        added_today = set()
    if added_today:
        lab = lab.set_index("Symbol", drop=False)
        extra = [s for s in added_today if s not in lab.index]
        if extra:
            lab = pd.concat([lab, pd.DataFrame([{"Symbol": s, "Label": "ADDED", "Reason": "Added from the scanner",
                                                 "Scan_Count": 0} for s in extra]).set_index("Symbol", drop=False)])
        m = lab["Symbol"].isin(added_today)
        lab.loc[m & lab["Label"].isin(["SCANNED", "CHECK"]), "Reason"] = "Added from the scanner"
        lab.loc[m & (lab["Label"] == "SCANNED"), "Label"] = "ADDED"
        lab.loc[m, "Keep"] = True
        lab = lab.reset_index(drop=True)
    # Breakouts already reviewed in Post Breakout today (saved or left on Skip) don't need tagging again
    try:
        reviewed = {r["symbol"] for r in _snap_q(sb, mcfg, "BREAKOUT", sd).execute().data}
    except Exception:
        reviewed = set()
    if reviewed:
        done = (lab["Label"] == "2_SAVE") & lab["Symbol"].isin(reviewed)
        if done.any():
            lab.loc[done, "Label"] = "SCANNED"
            lab.loc[done, "Keep"] = False
            st.caption(f"{int(done.sum())} breakouts you already reviewed in Post Breakout today (and skipped) are hidden.")
    # Regime from today's market note caps how many go on tomorrow's list
    try:
        rname, rlim = regime_limits(load_market_note(sb, mcfg, journal_session(mcfg)))   # tonight's regime
    except Exception:
        rname, rlim = None, None
    if rlim is not None:
        cap = rlim["max_gtts"]
        keep_idx = [i for i in lab.index[lab["Keep"] == True] if lab.at[i, "Label"] != "ADDED"]  # noqa: E712
        dropped = keep_idx[cap:]
        if dropped:
            lab.loc[dropped, "Keep"] = False
        st.info(f"Regime **{rname}** (today's market note): max **{cap}** new GTTs, {rlim['risk']}."
                + (f" Pre-ticks capped at {cap} — {len(dropped)} more left unticked, lowest priority first." if dropped else ""))
    else:
        st.warning("No regime for this session — write the market note at the top of the page to cap tonight's list.")
    notes = latest_note_by_symbol(sb, mcfg)
    if notes:
        lab["Note"] = lab["Symbol"].map(notes)
    counts = lab["Label"].value_counts()
    for c, (k, n) in zip(st.columns(len(LABEL_NAMES)), LABEL_NAMES.items()):
        c.metric(n, int(counts.get(k, 0)))

    show = ["Keep", "CONT", "Tier", "Patterns", "Label", "Symbol", "Note", "Reason", "Tag", "Scan_Count", "_chg_percentclose", "_vol_ratio",
            "Adr", "_rel_tightness_today", "_nr4", "_avgvol_mln", "_rel_wk_dist", "_10madist", "_20madist", "Avg_RS", "Sector"]
    saved_rows = lab[lab["Saved"] == True] if "Saved" in lab.columns else lab.iloc[0:0]  # noqa: E712
    if len(saved_rows):
        grp = {}
        for _, r_ in saved_rows.iterrows():
            grp.setdefault(LABEL_NAMES.get(r_["Label"], r_["Label"]), []).append(r_["Symbol"])
        st.markdown(f"**Saved breakouts checked: {len(saved_rows)}** — " + " · ".join(
            f"{k} {len(v)}: {', '.join(v[:15])}{'…' if len(v) > 15 else ''}" for k, v in grp.items()))
    show = _with_circuit(show, mcfg)
    view = _circuit_num(lab[lab["Label"] != "SCANNED"][[c for c in show if c in lab.columns]].copy())
    view["Label"] = view["Label"].map(lambda x: LABEL_NAMES.get(x, x))
    view["Tier"] = adr_tier(view["Adr"], cfg) if "Adr" in view.columns else ""
    # Ticks are kept per symbol in session state, so filtering and sorting the table never loses them
    tk = f"bt_ticks_{mcfg.get('market')}_{sd}"
    ticks = st.session_state.setdefault(tk, {"Keep": {}, "CONT": {}})
    for c_ in ("Keep", "CONT"):
        view[c_] = [bool(ticks[c_].get(s_, bool(v_) if pd.notna(v_) else False))
                    for s_, v_ in zip(view["Symbol"], view[c_])]
    if "_days_since_bo" in lab.columns:
        _dsb = pd.to_numeric(lab["_days_since_bo"], errors="coerce")
        _nrec = int(_dsb.between(1, cfg["recent_bo_days"]).sum())
        st.caption(f"Recent BO check: {_nrec} stocks in the scan broke out in the last {cfg['recent_bo_days']:g} days; "
                   f"{int((lab['Label'] == 'RECENT_BO').sum())} are tight again and untagged (saved ones show as Saved & tight).")
    else:
        st.caption("Recent BO check: days-since-breakout isn't in this scan, so recent breakouts can't be found.")
    with st.expander("Why isn't a stock here?  ·  how stocks get on this list"):
        st.markdown(
            "Rows come from three places: **saved breakouts** (your watchlist — Saved & tight / not tight yet / Wait / Weak), "
            "**yesterday's list** (Buy signal / Wait / Weak), **Recent BO, now tight** — broke out in the last "
            f"{cfg['recent_bo_days']:g} days (tagged or not), tight again and ≤ {cfg['recent_bo_max_rwd']:g} ADR from 10w — "
            "and **New candidates** — stocks from today's scan that pass *every* rule below. "
            "Stocks you tick in the scanner and add show as *Added by you*.")
        _why = st.text_input("Ticker", key="why_not_sym", placeholder="e.g. NET")
        if _why.strip():
            _tbl, _msg = explain_candidate(lab, _why, cfg)
            st.markdown(_msg)
            if _tbl is not None:
                st.dataframe(_tbl.style.map(lambda v: "color:#28a745;font-weight:bold" if v is True else
                                            ("color:#dc3545;font-weight:bold" if v is False else ""), subset=["Pass"]),
                             hide_index=True, use_container_width=True)

    # ADR tier checkboxes: tick one or more to show only those rows (none ticked = all)
    lim = {"P1": f"ADR ≥ {cfg['tier1_adr']:g}", "P2": f"ADR {cfg['tier2_adr']:g}–{cfg['tier1_adr']:g}",
           "P3": f"ADR < {cfg['tier2_adr']:g}"}
    _pv = view["Patterns"].fillna("").astype(str) if "Patterns" in view.columns else pd.Series("", index=view.index)
    tcols = st.columns([1, 1, 1] + [1.6] * len(DATA_PATTERNS) + [2])
    pick = [t for c_, t in zip(tcols, ("P1", "P2", "P3"))
            if c_.checkbox(f"{t} ({int((view['Tier'] == t).sum())})", key=f"bt_tier_{t}", help=lim[t])]
    pats = [p["name"] for c_, (k, p) in zip(tcols[3:], DATA_PATTERNS.items())
            if c_.checkbox(f"{p['name']} ({int(_pv.str.contains(p['name'], regex=False).sum())})",
                           key=f"bt_pat_{k}", help=p["help"](cfg))]
    tcols[-1].caption(f"Tiers: P1 {lim['P1']} · P2 {lim['P2']} · P3 {lim['P3']}. Patterns narrow any tier. "
                      "None ticked = all. Limits are in More list rules.")
    if pick:
        view = view[view["Tier"].isin(pick)]
    for pn in pats:
        view = view[_pv.reindex(view.index).fillna("").str.contains(pn, regex=False)]
    view = sort_by_tightness(view)
    view = view.assign(_t=view["Tier"].replace("", "P9")).sort_values("_t", kind="stable") \
        .drop(columns="_t").reset_index(drop=True)

    gkey = "bt_grid_" + "_".join(pick or ["all"]) + "".join("_" + p.replace(" ", "") for p in pats)
    resp = _tomorrow_grid(view, cfg, gkey)
    out = view
    if resp is not None and resp.get("data") is not None and len(resp["data"]):
        out = resp["data"]
        for _, r_ in out.iterrows():
            for c_ in ("Keep", "CONT"):
                ticks[c_][r_["Symbol"]] = _truthy(r_.get(c_))
    st.caption("Sort, filter and tick in the table — the copy boxes and Save follow it. "
               "(If you see an Update button under the table, press it first.) Nothing is sent until you press Save.")
    if len(out):
        _all = out["Symbol"].dropna().tolist()
        _tic = [s_ for s_ in _all if ticks["Keep"].get(s_)]
        with st.container(border=True):
            st.markdown("**Copy to TradingView** — in the table's order; hover a list and click its copy icon")
            for _c, (_lab, _xs) in zip(st.columns(2), [("All in the table", _all), ("Ticked for tomorrow", _tic)]):
                _c.caption(f"{_lab} · {len(_xs)}")
                if _xs:
                    _c.code(",".join(tv_symbol(x_, mcfg) for x_ in _xs), language=None)
    removes = [u for u in updates if u["action"] == "remove"]
    ok = []                                   # removing saved stocks happens in Watchlist → Clean-up, never here
    try:
        n_clean = len(cleanup_candidates(sb, mcfg, watch, base_df, 1))
    except Exception:
        n_clean = 0
    if n_clean:
        st.caption(f"🧹 {n_clean} saved stocks are missing from the scan — review them in **Watchlist → Clean-up**. "
                   "Saving this list never removes anything.")
    lab["Keep"] = [bool(ticks["Keep"].get(s_, bool(v_) if pd.notna(v_) else False)) for s_, v_ in zip(lab["Symbol"], lab["Keep"])]
    lab["CONT"] = [bool(ticks["CONT"].get(s_, bool(v_) if pd.notna(v_) else False)) for s_, v_ in zip(lab["Symbol"], lab["CONT"])]
    n_tick = int(lab["Keep"].sum())
    submitted = st.button(f"Save tomorrow's list  ({sd})  ·  {n_tick} ticked", type="primary", key="bt_save")
    if st.session_state.get("bt_msg"):
        msg, codes = st.session_state.pop("bt_msg")
        st.success(msg)
        if codes:
            st.code(codes, language=None)
    if not submitted:
        return
    for u in removes:
        u["confirmed"] = u["symbol"] in ok

    keep = sort_by_tightness(lab[lab["Keep"] == True])["Symbol"].tolist()  # noqa: E712
    try:
        n, r = save_tomorrow(sb, lab, updates, sd, mcfg)
        cont = lab[lab["CONT"] == True]["Symbol"].tolist()  # noqa: E712
        c_added = save_symbols_to_watchlist(sb, cont, "CONTINUATION", base_df, sd, mcfg, "ANTICIPATION")[0] if cont else 0
        warn = " More than 15 names — the plan is the best 10 or so." if len(keep) > 15 else ""
        st.session_state["bt_msg"] = (f"Saved {n} names for tomorrow and a snapshot of {len(lab)} stocks. "
                                      f"Saved {c_added} as CONTINUATION.{warn}",
                                      ",".join(tv_symbol(s_, mcfg) for s_ in keep))
        st.rerun()
    except Exception as ex:
        st.error(f"Save failed: {ex}")


def _truthy(v):
    return v is True or (isinstance(v, (int, float, np.integer, np.bool_)) and not pd.isna(v) and bool(v)) \
        or str(v).strip().lower() == "true"


def _grid_safe(df):
    """NaN/inf → None and numpy scalars → Python, so AgGrid can serialise the frame."""
    df = df.replace([np.inf, -np.inf], np.nan).copy()
    for c in df.columns:
        if df[c].dtype == bool:
            continue
        if df[c].isna().any():
            df[c] = df[c].astype(object)
            df.loc[df[c].isna(), c] = None
    return df


_JS = {
    "vol": """function(p){const v=p.value;if(v===null||v===undefined||isNaN(v)||v<=0)return null;
        if(v>=3.5)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};
        if(v>=1.5)return{'backgroundColor':'#8ee68e','color':'black'};if(v>=1.0)return{'backgroundColor':'#d4edda','color':'black'};
        if(v<0.5)return{'backgroundColor':'#f8d7da','color':'#721c24'};return null}""",
    "relwk": """function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;
        if(v<1.0)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(v<2.0)return{'backgroundColor':'#8ee68e','color':'black'};
        if(v<3.0)return{'backgroundColor':'#d4edda','color':'black'};if(v<5.0)return{'backgroundColor':'#fff3cd','color':'#664d03'};
        return{'backgroundColor':'#f8d7da','color':'#721c24'}}""",
    "ma": """function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;const a=Math.abs(v);
        if(v<-6)return{'backgroundColor':'#f8d7da','color':'#721c24','fontWeight':'bold'};
        if(a<2)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(a<4)return{'backgroundColor':'#8ee68e','color':'black'};
        if(a<6)return{'backgroundColor':'#d4edda','color':'black'};return null}""",
    "tight": """function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;
        if(v<=0.5)return{'backgroundColor':'#28a745','color':'white','fontWeight':'bold'};if(v<=0.8)return{'backgroundColor':'#fff3cd','color':'#664d03','fontWeight':'bold'};
        return null}""",
    "chg": """function(p){const v=p.value;if(v===null||v===undefined||isNaN(v))return null;
        if(v>=5)return{'backgroundColor':'#b44cb4','color':'white'};if(v>=2)return{'backgroundColor':'#e6b3e6','color':'black'};
        if(v>0)return{'backgroundColor':'#ffe6ff','color':'black'};return null}""",
    "pat": """function(p){return p.value?{'backgroundColor':'#ffd8a8','color':'#7a3e00','fontWeight':'bold'}:null}""",
    "reason": """function(p){return (p.value&&String(p.value).startsWith('⚡'))?{'backgroundColor':'#fd7e14','color':'white','fontWeight':'bold'}:null}""",
    "tier": """function(p){const v=p.value;if(v==='P1')return{'backgroundColor':'#155724','color':'white','fontWeight':'bold'};
        if(v==='P2')return{'backgroundColor':'#8ee68e','color':'black','fontWeight':'bold'};if(v==='P3')return{'backgroundColor':'#e9ecef','color':'black'};return null}""",
    "label": """function(p){const m={'Saved & tight':['#28a745','white'],'Saved, not tight yet':['#d4edda','black'],
        'Added by you':['#e2d9f3','#3d2a73'],'1 · Buy signal':['#155724','white'],'3 · Wait':['#fff3cd','#664d03'],
        '5 · Weak (review)':['#f8d7da','#721c24'],'Check chart':['#ffe5d0','#8a4b08'],'Recent BO, now tight':['#cff4fc','#055160'],'Pattern match':['#ffd8a8','#7a3e00']};
        const c=m[p.value];return c?{'backgroundColor':c[0],'color':c[1],'fontWeight':'bold'}:null}""",
}


def _tomorrow_grid(view, cfg, key):
    """Tomorrow's list as an AgGrid (like the scanner table): sort/filter/tick, returned FILTERED_AND_SORTED on Update."""
    from st_aggrid import AgGrid, GridOptionsBuilder, JsCode, GridUpdateMode, DataReturnMode
    df = _grid_safe(view)
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(resizable=True, filterable=True, sortable=True, minWidth=60, flex=0)
    gb.configure_grid_options(enableBrowserTooltips=True)
    tick = dict(editable=True, cellDataType="boolean", cellRenderer="agCheckboxCellRenderer",
                cellEditor="agCheckboxCellEditor", minWidth=80, maxWidth=95, pinned="left")
    gb.configure_column("Keep", headerName="Tomorrow", headerTooltip="On tomorrow's list", **tick)
    gb.configure_column("CONT", headerName="Save CONT", headerTooltip="Also save to the watchlist as CONTINUATION", **tick)
    gb.configure_column("Symbol", pinned="left", minWidth=95, maxWidth=130)
    js = {k: JsCode(v) for k, v in _JS.items()}
    cols = {"Tier": ("Tier", "tier", 60), "Patterns": ("Pattern", "pat", 130), "Label": ("Situation", "label", 150), "Note": ("My note", None, 160),
            "Reason": ("Reason", "reason", 300), "Tag": ("Saved as", None, 150), "Scan_Count": ("Scans", None, 60),
            "_chg_percentclose": ("Chg %", "chg", 70), "_vol_ratio": ("Vol x", "vol", 65), "Adr": ("ADR", None, 60),
            "_circuit": ("Circuit %", None, 75), "_rel_tightness_today": ("Rel tight", "tight", 75),
            "_nr4": ("NR4 %", None, 70), "_rel_wk_dist": ("ADRs from 10w", "relwk", 95), "_avgvol_mln": ("Avg vol", None, 75),
            "_10madist": ("10MA dist", "ma", 80), "_20madist": ("20MA dist", "ma", 80), "Avg_RS": ("Avg RS", None, 70),
            "Sector": ("Sector", None, 140)}
    for c, (name, style, w) in cols.items():
        if c in df.columns:
            kw = {"headerName": name, "minWidth": w}
            if style:
                kw["cellStyle"] = js[style]
            if c in ("_chg_percentclose", "_vol_ratio", "Adr", "_rel_tightness_today", "_nr4", "_rel_wk_dist",
                     "_avgvol_mln", "_10madist", "_20madist", "Avg_RS", "_circuit", "Scan_Count"):
                kw["type"] = ["numericColumn"]
                kw["filter"] = "agNumberColumnFilter"
                kw["valueFormatter"] = JsCode("function(p){return (p.value===null||p.value===undefined)?'':"
                                              "(Math.round(p.value*100)/100).toString()}")
            gb.configure_column(c, **kw)
    return AgGrid(df, gridOptions=gb.build(), height=480, width="100%", key=key,
                  update_mode=GridUpdateMode.MANUAL, data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
                  allow_unsafe_jscode=True)


def render_breakout_panel(st, sb, base_df, saved_prefs, mcfg, shared=None):
    """Post Breakout mode: once, late in the session or after the close."""
    st.markdown("---")
    st.subheader("Tag breakouts  ·  once a day")
    st.caption(f"gtt_process {PROCESS_VERSION}")
    if sb is None:
        st.error("Database not connected."); return
    sd = _header_time(st, mcfg, base_df, sb)
    shared = shared or {}
    own = {k: v for k, v in BREAKOUT_DEFAULTS.items() if k not in shared}   # Min Chg% / Min Vol come from the sidebar
    cfg = {**_rules_editor(st, "Breakout tagging rules", own, saved_prefs.get("breakout_tags"), "bo_cfg"), **shared}
    if shared:
        st.caption("Breakout = up ≥ {:g}% on ≥ {:g}x volume (sidebar Volume Breakout Filters).".format(
            cfg["bo_min_chg"], cfg["bo_min_vol"]))
    try:
        _, ylist = load_last_list(sb, mcfg, before_date=sd)   # the list you traded this session
        watch = load_active_watchlist(sb, mcfg)
    except Exception as e:
        st.error(f"Could not read the database. Did you run supabase_migration.sql? ({e})"); return
    on_list = set(ylist)
    bo = find_breakouts(base_df, on_list, watch, cfg, today=sd)
    st.session_state["bo_list"] = bo[["Symbol", "Batch"]].copy()   # the Copy Symbols buttons use this same list
    old = not_updated(base_df, sd)
    if old:
        d0 = add_derived(base_df)
        left = d0[d0["Symbol"].isin(old.keys()) & (d0["_chg_percentclose"].fillna(0) >= cfg["bo_min_chg"])
                  & (d0["_vol_ratio"].fillna(0) >= cfg["bo_min_vol"])]["Symbol"].tolist()
        st.warning(f"{len(old)} stocks not updated by MarketInOut yet (still {min(old.values())} data) — left out until "
                   "they refresh." + (f" Includes old breakouts: {', '.join(left[:12])}{'…' if len(left) > 12 else ''}."
                                       if left else "") + " Click Refresh Now in a few minutes.")
    if st.session_state.get("bo_msg"):
        st.success(st.session_state.pop("bo_msg"))
    if bo.empty:
        st.caption(f"Session **{sd}**"); st.info("No breakouts."); return

    ns, nm = int((bo["Batch"] == "Strong").sum()), int((bo["Batch"] == "Moderate").sum())
    opts = {f"Strong BO (≥{cfg['strong_min_vol']:g}x) · {ns}": "Strong",
            f"Moderate BO ({cfg['bo_min_vol']:g}–{cfg['strong_min_vol']:g}x) · {nm}": "Moderate",
            f"All · {len(bo)}": "All"}
    pick = opts[st.radio("Work through", list(opts), horizontal=True, key="bo_batch")]
    part = bo if pick == "All" else bo[bo["Batch"] == pick]
    st.caption(f"Session **{sd}** · {len(part)} breakouts. Ticks are my suggestion from the scan — check each chart, "
               "then tick/untick EP, TIGHT_BO, WEMA_BO (a stock can have more than one) and save this batch.")
    if part.empty:
        st.info("Nothing in this batch."); return

    multi = _multi_ok(st)
    part = part.reset_index(drop=True)
    tagcols = ["Tags"] if multi else list(SETUP_TYPES)
    if multi:
        part["Tags"] = [[t for t in SETUP_TYPES if bool(r[t])] for _, r in part.iterrows()]
    notes = latest_note_by_symbol(sb, mcfg)
    if notes:
        part["Note"] = part["Symbol"].map(notes)
    show = ["Skip"] + tagcols + ["Rating", "Symbol", "Note", "Suggested", "Why", "In_Watchlist", "On_List", "_chg_percentclose",
                                 "_vol_ratio", "Adr", "_rel_tightness_prev", "_rel_wk_dist", "_avgvol_mln", "_10madist",
                                 "_20madist", "Scan_Count", "Avg_RS", "Sector"]
    show = _with_circuit(show, mcfg)
    view = _circuit_num(part[[c for c in show if c in part.columns]].copy())
    pre = {t: int(part[t].sum()) for t in SETUP_TYPES}
    st.caption("Pre-filled from the scan: " + ", ".join(f"{t} {c}" for t, c in pre.items() if c) +
               ". **Every row starts as Skip** — untick Skip for each stock you want to save, "
               "so an accidental Save changes nothing.")
    ekey = f"bo_editor_{pick}"
    cfgcols = {
        "Skip": st.column_config.CheckboxColumn("Skip", help="Don't save this stock, whatever is tagged"),
        "Rating": st.column_config.SelectboxColumn("Rating", options=RATINGS, required=True,
                                                   help="Your grade after the chart check: 3★, 4★, 5★"),
        "Suggested": st.column_config.TextColumn("My suggestion"),
        "Note": st.column_config.TextColumn("My note", width="medium", help="Your latest note on this stock"),
        "Why": st.column_config.TextColumn("Why", width="large"),
        "In_Watchlist": st.column_config.TextColumn("Already saved as"),
        "On_List": st.column_config.CheckboxColumn("Was on list"),
        "_chg_percentclose": st.column_config.NumberColumn("Chg %", format="%.1f"),
        "_vol_ratio": st.column_config.NumberColumn("Vol x", format="%.1f"),
        "_rel_tightness_prev": st.column_config.NumberColumn("Rel tight (prev)", format="%.2f"),
        "_rel_wk_dist": st.column_config.NumberColumn("ADRs from 10w", format="%.1f"),
        "_avgvol_mln": st.column_config.NumberColumn("Avg vol", format="%.0f", help=LIQ_HELP),
        "_10madist": st.column_config.NumberColumn("10MA %", format="%.1f"),
        "_20madist": st.column_config.NumberColumn("20MA %", format="%.1f"),
        "_circuit": st.column_config.NumberColumn("Circuit %", format="%.0f", help=CIRCUIT_HELP),
    }
    if multi:
        cfgcols["Tags"] = _tags_column(st, "Tags", "Pick one or more: EP, TIGHT_BO, WEMA_BO, ATH, CONTINUATION")
    else:
        cfgcols["CONTINUATION"] = st.column_config.CheckboxColumn("CONT")
    with st.form(f"bo_form_{pick}", border=False):
        edited = st.data_editor(style_table(view), hide_index=True, height=440, key=ekey,
                                disabled=[c for c in view.columns if c not in ["Skip"] + tagcols + ["Rating"]],
                                column_config=cfgcols)
        submitted = st.form_submit_button(f"Save {pick.lower()} batch to watchlist  ·  {sd}", type="primary")
    if submitted:
        if multi:
            picked = edited["Tags"].map(lambda l: list(l) if isinstance(l, (list, tuple)) else [])
            for t in SETUP_TYPES:
                part[t] = picked.map(lambda l, t=t: t in l).values
        else:
            for t in SETUP_TYPES:
                part[t] = edited[t].fillna(False).astype(bool).values
        part["Rating"] = edited["Rating"].fillna("—").values
        part["Skip"] = edited["Skip"].fillna(False).astype(bool).values
        try:
            a, t = save_breakouts(sb, part, sd, mcfg)
            live = part[~part["Skip"]]
            st.session_state["bo_msg"] = ((f"{pick} batch: nothing saved — every row was still on Skip. "
                                           "Untick Skip for the stocks you want to keep.") if bool(part["Skip"].all()) else
                                          f"{pick} batch: saved {a} tags to the watchlist "
                                          f"({int(live[SETUP_TYPES].any(axis=1).sum())} stocks, "
                                          f"{int(part['Skip'].sum())} skipped); {t} breakouts recorded.")
            st.session_state.pop(ekey, None)
            st.rerun()
        except Exception as ex:
            st.error(f"Save failed: {ex}")


def last_seen_map(sb, mcfg, symbols, base_df=None):
    """{symbol: 'YYYY-MM-DD'} — the last session each stock was in the scan (today's scan, then daily snapshots)."""
    symbols = [str(s).upper() for s in symbols]
    out = {}
    if base_df is not None and not base_df.empty and "Symbol" in base_df.columns:
        live = set(base_df["Symbol"].astype(str).str.upper())
        today = effective_session(base_df, mcfg)[0].isoformat()
        out.update({s: today for s in symbols if s in live})
    if sb is not None and symbols:
        try:
            rows = (sb.table("daily_snapshots").select("symbol,snap_date,metrics").eq("user_id", mcfg["user_id"])
                    .eq("market", mcfg["market"]).in_("symbol", symbols).order("snap_date", desc=True)
                    .limit(5000).execute().data)
            for r in rows:
                if not (r.get("metrics") or {}).get("Last"):   # saved as "missing from the scan" that day
                    continue
                d = str(r["snap_date"])[:10]
                if d > out.get(r["symbol"], ""):
                    out[r["symbol"]] = d
        except Exception:
            pass
    return out


def cleanup_candidates(sb, mcfg, watch, base_df=None, min_days=1):
    """Saved stocks that are missing from the scan. Rule switched on: not in the scan for ≥ min_days trading days."""
    if not watch:
        return []
    ref = effective_session(base_df, mcfg)[0] if base_df is not None and not base_df.empty else journal_session(mcfg)
    live = set(base_df["Symbol"].astype(str).str.upper()) if base_df is not None and not base_df.empty else None
    seen = last_seen_map(sb, mcfg, [r["symbol"] for r in watch], base_df)
    out = []
    for r in watch:
        sym = str(r["symbol"]).upper()
        if live is not None and sym in live:
            continue
        ls = max(seen.get(sym, ""), str(r.get("last_seen") or "")[:10])
        try:
            gone = int(np.busday_count(date.fromisoformat(ls), ref)) if ls else None
        except Exception:
            gone = None
        if gone is None:
            if live is None:
                continue                                # no scan loaded and no history: can't tell
            reason = "Not in today's scan — not seen since it was saved"
        elif gone >= min_days:
            reason = f"Not in the scan for {gone} trading day{'s' if gone != 1 else ''} — last seen {ls}"
        else:
            continue
        out.append({"id": r.get("id"), "Symbol": sym, "Tags": " + ".join(tags_of(r)), "Rating": rating_label(r.get("rating")),
                    "Saved on": str(r.get("trigger_date") or r.get("added_date") or "")[:10], "Last seen": ls or "—",
                    "Days missing": gone, "Reason": reason,
                    "Chart": "https://www.tradingview.com/chart/?symbol=" + tv_symbol(sym, mcfg)})
    return sorted(out, key=lambda x: -(x["Days missing"] if x["Days missing"] is not None else 999))


def render_cleanup(st, sb, base_df, mcfg, watch):
    with st.container(border=True):
        st.markdown("**Clean-up** · check each chart by its reason, then Apply")
        c1, c2 = st.columns([1, 3])
        nd = int(c1.number_input("Not in the scan for at least (trading days)", 1, 60, 1, key="wl_c_days"))
        c2.caption("Rule switched on: **not in the scan**. Every row starts as **Keep** — open the chart, switch the "
                   "broken ones to Remove, then Apply. Removed stocks stay in the history and can be saved again later.")
        if base_df is None or base_df.empty:
            st.info("Generate the scan first for an exact 'in today's scan' check — until then only the last-seen dates are used.")
        try:
            cands = cleanup_candidates(sb, mcfg, watch, base_df, nd)
        except Exception as e:
            st.error(f"Could not build the clean-up list: {e}"); return
        if not cands:
            st.caption("Nothing to clean up — every saved stock is in the scan.")
            return
        cdf = pd.DataFrame(cands)
        cdf.insert(0, "Action", "Keep")
        with st.form("wl_clean_form", border=False):
            ed = st.data_editor(cdf, hide_index=True, key="wl_clean_ed", height=min(420, 38 + 35 * len(cdf)),
                                disabled=[c for c in cdf.columns if c != "Action"],
                                column_config={"id": None,
                                               "Action": st.column_config.SelectboxColumn("Action", options=["Keep", "Remove"], required=True),
                                               "Days missing": st.column_config.NumberColumn("Days missing", format="%d"),
                                               "Reason": st.column_config.TextColumn("Reason", width="large"),
                                               "Chart": st.column_config.LinkColumn("Chart", display_text="open")})
            go_ = st.form_submit_button(f"Apply clean-up ({len(cdf)} listed)")
        if go_:
            rm = ed[ed["Action"] == "Remove"]
            for _, r in rm.iterrows():
                sb.rpc("remove_from_watchlist", {"p_id": int(r["id"]), "p_reason": "dropped out of scan",
                                                 "p_status": "removed"}).execute()
            st.session_state["wl_clean_msg"] = f"Removed {len(rm)}: {', '.join(rm['Symbol'])}" if len(rm) else "Nothing removed."
            st.session_state.pop("wl_clean_ed", None)
            st.rerun()
        if st.session_state.get("wl_clean_msg"):
            st.success(st.session_state.pop("wl_clean_msg"))


def render_watchlist_tab(st, sb, base_df, mcfg):
    """Working watchlist: generate lists, update entries, clean up, history."""
    st.subheader(f"Watchlist · {mcfg['market']}")
    st.caption(f"gtt_process {PROCESS_VERSION}")
    if sb is None:
        st.error("Database not connected."); return
    try:
        watch = load_active_watchlist(sb, mcfg)
        tdate, tlist = load_last_list(sb, mcfg)
    except Exception as e:
        st.error(f"Could not read the database. Did you run supabase_migration.sql? ({e})"); return
    wdf = pd.DataFrame(watch)
    if not wdf.empty:
        wdf["_tags"] = [tags_of(r) for r in watch]

    # ── 1. Generate lists ──
    st.markdown("#### Generate a list")
    counts = pd.Series([t for ts in wdf["_tags"] for t in ts]).value_counts().to_dict() if not wdf.empty else {}
    btns = [("TOMORROW", f"Tomorrow's list ({len(tlist)})")] + [(t, f"{t} ({counts.get(t, 0)})") for t in SETUP_TYPES]
    if counts.get("UNTAGGED"):
        btns.append(("UNTAGGED", f"UNTAGGED ({counts['UNTAGGED']})"))
    for c, (k, label) in zip(st.columns(len(btns)), btns):
        if c.button(label, key=f"wl_btn_{k}", use_container_width=True):
            st.session_state["wl_show"] = k
    min_r = st.radio("Minimum rating for breakout lists", ["Any", "3★+", "4★+", "5★"], horizontal=True, key="wl_minr")
    min_n = {"Any": 0, "3★+": 3, "4★+": 4, "5★": 5}[min_r]
    show = st.session_state.get("wl_show")
    if show:
        if show == "TOMORROW":
            syms, ex = tlist, {}
            st.caption(f"Tomorrow's list saved on {tdate or '—'} — tightest first (rel tightness on the day saved).")
        else:
            part = wdf[wdf["_tags"].map(lambda ts: show in ts)] if not wdf.empty else wdf
            if not part.empty and min_n:
                part = part[part["rating"].fillna(0) >= min_n]
            if not part.empty:
                part = part.assign(_r=part["rating"].fillna(0)).sort_values(["_r", "symbol"], ascending=[False, True])
            syms = part["symbol"].tolist() if not part.empty else []
            ex = dict(zip(part["symbol"], part["exchange"])) if not part.empty else {}
            st.caption(SETUP_NAMES.get(show, show))
        if syms:
            st.code(",".join(tv_symbol(s, mcfg, ex.get(s)) for s in syms), language=None)
            st.caption(f"{len(syms)} symbols — paste into TradingView.")
        else:
            st.info("Empty.")

    # ── 2. Active watchlist with today's data ──
    st.markdown("---"); st.markdown("#### Saved breakouts")
    if wdf.empty:
        st.info("Nothing saved yet. Tag breakouts in the scanner's Post Breakout mode.")
    else:
        live = add_derived(base_df).drop_duplicates("Symbol").set_index("Symbol") if base_df is not None and not base_df.empty else None
        wdf["Rating"] = wdf["rating"].map(rating_label) if "rating" in wdf.columns else "—"
        for c in ("last_trigger_date",):
            if c not in wdf.columns:
                wdf[c] = None
        multi = _multi_ok(st)
        v = wdf[["id", "symbol", "Rating", "trigger_date", "last_trigger_date", "trigger_close", "prev_close", "vol_ratio",
                 "last_seen", "last_status", "tags"]].copy()
        has_untagged = any("UNTAGGED" in ts for ts in wdf["_tags"])
        if multi:
            v.insert(2, "Tags", [list(ts) for ts in wdf["_tags"]])
            tagcols = ["Tags"]
        else:
            for t in SETUP_TYPES:
                v.insert(2 + SETUP_TYPES.index(t), t, wdf["_tags"].map(lambda ts, t=t: t in ts).values)
            if has_untagged:
                v.insert(2, "UNTAGGED", wdf["_tags"].map(lambda ts: "UNTAGGED" in ts).values)
            tagcols = list(SETUP_TYPES)
        v["age"] = v["last_trigger_date"].fillna(v["trigger_date"]).fillna(wdf["added_date"]).map(
            lambda s: (date.today() - pd.to_datetime(s).date()).days if s else None)
        if live is not None:
            for c, src in [("Last", "Last"), ("Chg %", "_chg_percentclose"), ("Vol x", "_vol_ratio"),
                           ("10MA %", "_10madist"), ("20MA %", "_20madist")]:
                v[c] = v["symbol"].map(live[src]) if src in live.columns else np.nan
            v["In scan"] = v["symbol"].isin(live.index)
        v["tags"] = v["tags"].map(lambda t: ", ".join(t) if isinstance(t, list) else "")
        _notes = latest_note_by_symbol(sb, mcfg)
        if _notes:
            v["My note"] = v["symbol"].map(_notes)
        v.insert(0, "Action", "keep")
        v = v.assign(_r=wdf["rating"].fillna(0).values).sort_values(["_r", "symbol"], ascending=[False, True]).drop(columns="_r")
        editable = ["Action", "Rating"] + tagcols
        cfgcols = {
            "id": None,
            "Action": st.column_config.SelectboxColumn("Action", options=["keep", "traded", "remove"], required=True),
            "UNTAGGED": st.column_config.CheckboxColumn("Untagged", help="Old saved stock — tick its real tag(s)"),
            "CONTINUATION": st.column_config.CheckboxColumn("CONT"),
            "last_trigger_date": "Latest BO",
            "Rating": st.column_config.SelectboxColumn("Rating", options=RATINGS, required=True),
            "trigger_date": "Breakout day", "trigger_close": "BO close", "prev_close": "Fail level",
            "vol_ratio": st.column_config.NumberColumn("BO vol x", format="%.1f"),
            "tags": "Notes",
        }
        if multi:
            cfgcols["Tags"] = _tags_column(st, "Tags", "Pick one or more", extra=("UNTAGGED",) if has_untagged else ())
        st.caption("One row per stock. Change tags, rating or Action, then press Apply — nothing is sent until then.")
        with st.form("wl_form", border=False):
            ed = st.data_editor(v, hide_index=True, height=420, key="wl_editor",
                                disabled=[c for c in v.columns if c not in editable], column_config=cfgcols)
            applied = st.form_submit_button("Apply changes")

        def tagset(r):
            if multi:
                l = r["Tags"] if isinstance(r["Tags"], (list, tuple)) else []
                return [t for t in SETUP_TYPES if t in l]
            return [t for t in SETUP_TYPES if bool(r[t])]

        if applied:
            orig = v.set_index("id")
            changes = [r for _, r in ed.iterrows()
                       if r["Action"] != "keep" or tagset(r) != tagset(orig.loc[r["id"]]) or r["Rating"] != orig.loc[r["id"], "Rating"]]
            errs = []
            for r in changes:
                try:
                    if r["Action"] in ("traded", "remove"):
                        sb.rpc("remove_from_watchlist", {"p_id": int(r["id"]), "p_reason": "manual" if r["Action"] == "remove" else "traded",
                                                         "p_status": "traded" if r["Action"] == "traded" else "removed"}).execute()
                        continue
                    if r["Rating"] != orig.loc[r["id"], "Rating"]:
                        sb.table("watchlist").update({"rating": rating_int(r["Rating"])}).eq("id", int(r["id"])).execute()
                    new_t, old_t = tagset(r), tagset(orig.loc[r["id"]])
                    if new_t != old_t:
                        if not new_t:
                            raise ValueError("no tag picked — pick at least one, or set Action to remove")
                        sb.table("watchlist").update({"setup_types": new_t}).eq("id", int(r["id"])).execute()
                        sb.table("watchlist_events").insert({"watchlist_id": int(r["id"]), "event": "retagged",
                                                             "detail": f"{'+'.join(old_t) or 'UNTAGGED'} → {'+'.join(new_t)}"}).execute()
                except Exception as ex:
                    errs.append(f"{r['symbol']}: {ex}")
            if errs:
                st.error("; ".join(errs))
            elif changes:
                st.session_state["wl_msg"] = f"Updated {len(changes)} stock(s)."
                st.session_state.pop("wl_editor", None)
                st.rerun()
            else:
                st.info("No changes.")
        if st.session_state.get("wl_msg"):
            st.success(st.session_state.pop("wl_msg"))

        # everything saved (best rating first), respecting the "Minimum rating" choice above
        allsyms = [s_ for s_, r_ in sorted(zip(wdf["symbol"], wdf["rating"].fillna(0)), key=lambda x: (-x[1], x[0]))
                   if r_ >= min_n]
        exmap = dict(zip(wdf["symbol"], wdf["exchange"]))
        if st.button(f"Copy all for TradingView ({len(allsyms)})", key="wl_copy_all", disabled=not allsyms,
                     help="All saved breakouts, best rating first" + (f", {min_r} only" if min_n else "")):
            st.session_state["wl_copy_all_on"] = True
        if st.session_state.get("wl_copy_all_on") and allsyms:
            st.code(",".join(tv_symbol(s_, mcfg, exmap.get(s_)) for s_ in allsyms), language=None)
            st.caption(f"{len(allsyms)} symbols — click the copy icon, then paste into a TradingView watchlist.")

    # ── 3. Add manually ──
    with st.container(border=True):
        st.markdown("**Add a stock manually**")
        c1, c2, c3 = st.columns([2, 2, 1])
        sym = c1.text_input("Symbols (comma separated)", key="wl_add_sym")
        tag = c2.selectbox("Save to", ["Tomorrow's list"] + SETUP_TYPES, key="wl_add_tag")
        c3.write(""); c3.write("")
        syms = _parse_symbols(sym)
        if c3.button("Add", key="wl_add_btn", disabled=not syms):
            sd = effective_session(base_df, mcfg, sb)[0]
            try:
                if tag == "Tomorrow's list":
                    add_to_tomorrow(sb, syms, base_df, sd, mcfg)
                else:
                    save_symbols_to_watchlist(sb, syms, tag, base_df, sd, mcfg, "MANUAL")
                st.success(f"Added {', '.join(syms)} to {tag}."); st.rerun()
            except Exception as ex:
                st.error(f"Save failed: {ex}")

    # ── 4. Clean-up: review saved stocks missing from the scan ──
    render_cleanup(st, sb, base_df, mcfg, watch)

    # ── 5. History ──
    with st.container(border=True):
        st.markdown("**History** · daily snapshots")
        try:
            dates = sorted({x["snap_date"] for x in sb.table("daily_snapshots").select("snap_date")
                            .eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
                            .order("snap_date", desc=True).limit(5000).execute().data}, reverse=True)
        except Exception:
            dates = []
        if not dates:
            st.caption("No snapshots yet.")
        else:
            c1, c2 = st.columns(2)
            pick = c1.selectbox("Date", dates, key="wl_h_date")
            scn = c2.selectbox("Scanner", ["ANTICIPATION", "BREAKOUT"], key="wl_h_scn")
            h = (sb.table("daily_snapshots").select("symbol,list,selected,scan_count,metrics")
                 .eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"]).eq("scanner", scn)
                 .eq("snap_date", pick).neq("list", "SCANNED").execute().data)
            if h:
                hd = pd.DataFrame(h)
                hd["note"] = hd["metrics"].map(lambda m: (m.get("reason") or m.get("why") or "") if isinstance(m, dict) else "")
                st.dataframe(hd.drop(columns="metrics").sort_values(["list", "symbol"]), hide_index=True)
            else:
                st.caption("Nothing labelled that day.")


# ════════════════════════════════════════════════════════════════════════════
# Entry rules — reference tab (the swing-trading guardrails)
# ════════════════════════════════════════════════════════════════════════════
PLAYBOOK_URL = "https://claude.ai/code/artifact/d4a3eba7-800e-46a9-8d83-1145b008255b"


def render_rules_tab(st, mcfg):
    st.subheader("Entry rules · swing trades only")
    st.error("**I am a swing trader.** Small timeframes decide *when* I enter. The daily chart decides *what* I buy, "
             "*whether* I hold, and *when* I sell. Once I'm filled, it's a swing trade.")

    st.markdown("""
#### Swing, not intraday
| Timeframe | Its only job |
| --- | --- |
| **Daily chart** | The setup, the tag, the rating, tomorrow's list, the stop after day 1, the exit (close below the 10/20 MA) |
| **1 / 5-minute chart** | The entry trigger on the day — nothing else |

- **The only intraday exit is my stop** (low of the day). I don't sell because a 5-minute candle looks weak.
- **No profit-taking on day 1 or 2.** The first partial sale comes around days 3–5.
- **No breakeven stop before that partial sale.** Normal day-2 pullbacks would shake me out.
- **No new ideas during the session.** If it wasn't on tomorrow's list and it isn't an EP, I save it for later.
- **After the fill, close the 1-minute chart.** Stop and alerts are set; the daily chart does the rest.
""")

    st.markdown("""
#### Before I press buy
1. It's **on tomorrow's list** (or it's an EP under my EP rule).
2. Price is **above the trigger**: the higher of the level (range / day-1 / tight-day / wick high) and the opening-range high.
3. **Volume is on pace** for well above normal, for the time of day.
4. **Stop = low of the day, within ~1 ADR** of my entry. Wider → I'm late: skip, or starter size.
5. **Size from the stop:** shares = (account × 1%) ÷ (entry − stop), within my max position %.
6. **No more than 2–3 new trades today**, and not 3 in the same sector.
7. **Market isn't weak** (index above its 10/20 MA).
""")

    st.markdown("""
#### The entries
| Entry | When | Trigger | Stop |
| --- | --- | --- | --- |
| **Day-1 breakout** | Tight range on my list breaks out | Higher of the range's highest high and the 1/5-min opening-range high | Low of the day |
| **Day-2 follow-through** | I missed day 1, and day 1 **closed strong** | Day-2 opening-range high, and above day 1's high if it opened below it | Day 2's low |
| **Day-3+ continuation** | Day 2 (or several days) went **tight** after the breakout — also flags and high tight flags | Break of the **latest tight day's high** | Low of the day, or the tight day's low |
| **After a wick day** | Big volume but a long upper wick | Break of the tight days under the wick, or of the wick high — **not** the day-2 opening-range high | Low of the day |
| **Pullback** | Saved breakout pulls back to the 10/20 MA and holds | Reclaim of the bounce day's high | Low of the day |
| **EP** (only if I've adopted the rule) | News gap on huge volume, not on my list | Opening-range high (first 30–60 min) | Low of the day |

**Skip day 2** if it gaps more than ~1 ADR above day 1's close. **No trade** if it falls back into the base or below day 1's low.
""")

    st.markdown("""
#### Never an entry
- Not on the list and not an EP → **save it**, buy its next setup.
- Stop more than ~1 ADR away, or gapped more than 1 ADR → **late**.
- Broke the level on thin volume.
- Day 1 closed weak, back near or inside the range → failed breakout.
- Adding to a loser, or **more than 1% risk because I really like it** or missed it before.
""")

    st.markdown("""
#### After the entry (daily chart)
1. **Entry day → day 5:** stop stays at the entry day's low. The close decides: a close back inside the range is a failed breakout — sell into the close or next open.
2. **Days 3–5, if it's working:** sell ⅓–½, move the rest to breakeven.
3. **Then:** trail with the 10-day MA (20 for slower movers). Sell on a **daily close** below it.
4. **Adding:** only at a new setup, sized from its own stop, only if the first position is in profit.

Most breakouts don't rip. Small losses are the cost; a few winners run for weeks.
""")
    st.caption(f"Full detail and worked examples: [Breakout Entry Playbook]({PLAYBOOK_URL})")