"""
Crypto Focus — one page for Daily and Hourly crypto (replaces GTT Crypto Daily + GTT Crypto Hourly Scanner).

Same flow as the stock Sector Focus, without sectors:
  scan (3 MarketInOut performance screens + 1 weekly/daily screen)  →  7 rules ✓/½/✗ + Score + Setup
  →  ⚑ flag on the row (Save)  →  Flagged: Pick / Rank / Note / Unflag (Save)  →  copy to TradingView (table order)

Only coins in the current scan are shown. Flags live in the Supabase `flags` table (market CRYPTO_D / CRYPTO_H).
Crypto rules of the road: the daily candle opens 00:00 UTC (11 am AEDT); entry = the 1-min engulfing breakout candle
on volume, stop at THAT candle's low (not the low of the day).
"""
from io import StringIO

import numpy as np
import pandas as pd
import requests

import gtt_process as gp
import focus_flow as ff

SCREENS = {
    "Daily": {"scans": {"1M": "https://api.marketinout.com/run/screen?key=304125ceaf8c4948",
                        "3M": "https://api.marketinout.com/run/screen?key=17e949fd9a4148ab",
                        "6M": "https://api.marketinout.com/run/screen?key=1caa51e5c0ab4e60"},
              "extra": "https://api.marketinout.com/run/screen?key=dfffbaf35d5d4646"},
    "Hourly": {"scans": {"22h": "https://api.marketinout.com/run/screen?key=9fdcdedbcee248d8",
                         "68h": "https://api.marketinout.com/run/screen?key=30ae06ca7155498e",
                         "126h": "https://api.marketinout.com/run/screen?key=e42d24a9aabc4a7e"},
               "extra": "https://api.marketinout.com/run/screen?key=9a2a66e365b7429c"},
}
COLUMNS = {
    "Daily": ['Symbol', 'Last', 'Timestamp', '_chg_percentclose', 'cap', 'dvol', '_avgvol_mln', '_bo_engulfing_cndl',
              '_circuit', '_days_since_bo', '_bo_dollar_vol_mln', '_rvol_to_float', 'Adr', 'Ti65',
              '_avg_vol_float_ratio', '_20madist', '_10wmadist', '_10madist', '_nr4', '_nr4_previous', '_rs',
              '_period_perf', '_insideday'],
    "Hourly": ['Symbol', 'Last', 'Timestamp', '_chg_percentclose_hourly', '_avgvol_mln_hourly', 'cap', 'Adr', 'Ti65',
               '_20madist', '_10madist', '_period_perf', '_wtc', '_nr4', '_nr4_previous', 'dvolhourly', 'dvoldaily'],
}
for _c in COLUMNS.values():          # higher-low / MA-hold columns, same 4 formulas as the stock screens (optional)
    _c += list(gp.MA_HOLD_COLS)
EXTRA_COLUMNS = {
    "Daily": ['Wema10', 'Dist_wema10_pct', 'Weeklyclose_chg_pct', 'Tightcloses_of4', 'Insidebar_thiswk', 'Insidebars_of8',
              'Weeklycontraction', 'Pricevs2yrlow_ratio', 'Pctof10wkhigh', 'Weeklyvolratio', 'Weeklyrsi'],
    "Hourly": ['Eema20', 'Eema10', 'Dist_dema10_pct', 'Dist_dema20_pct', 'Dailyclose_chg_pct', 'Dailytightcloses_of4',
               'Insidebar_thiswk', 'Insidebar_thisday', 'Insidebars_of8', 'Dailycontraction', 'Weeklyrsi', 'Dailyrsi'],
}
CFG = {
    "Daily": {"min_liq": 20.0, "bo_days": 10, "bo_min_value": 50.0, "bo_min_vol": 2.0, "today_min_chg": 3.0,
              "min_adr": 5.0, "max_off_adr": 1.0, "max_reltight": 0.8, "ma_min": -0.3, "ma_max": 1.0,
              "wk_full": 2.0, "wk_half": 3.0, "target": 5},
    "Hourly": {"min_liq": 1.0, "bo_min_vol": 2.0, "day_min_vol": 1.5, "min_adr": 5.0, "max_reltight": 0.8,
               "ma_min": -0.3, "ma_max": 1.0, "dma_max": 1.5, "min_tight_closes": 2, "target": 5},
}
CFG_LABELS = {"min_liq": "Min avg $ volume ($M)", "bo_days": "Breakout within (days)", "bo_min_value": "Breakout day $ value ($M)",
              "bo_min_vol": "Breakout volume (x avg)", "today_min_chg": "Today's breakout: min % up",
              "day_min_vol": "Day volume vs normal (x)", "min_adr": "Min ADR %", "max_off_adr": "Max below 10-week high (ADRs)",
              "max_reltight": "Max rel tight (NR4 ÷ ADR)", "ma_min": "10MA floor (ADRs)", "ma_max": "10MA ceiling (ADRs)",
              "dma_max": "Daily 10EMA ceiling (ADRs)", "wk_full": "Weekly: full size up to (ADRs from 10w)",
              "wk_half": "Weekly: half size up to", "min_tight_closes": "Daily tight closes (of 4)", "target": "Picks to aim for"}
RULES = {
    "Daily": [("R1", "Liquid"), ("R2", "Volume"), ("R3", "ADR"), ("R4", "Pivot"), ("R5", "Tight"), ("R6", "10MA"), ("R7", "Weekly")],
    "Hourly": [("R1", "Liquid"), ("R2", "Volume"), ("R3", "ADR"), ("R4", "Daily trend"), ("R5", "Tight"), ("R6", "Hourly 10MA"),
               ("R7", "Daily closes")],
}
RULE_HELP = {
    "Daily": {"R1": "Liquid: avg daily $ volume ≥ {min_liq:g}M",
              "R2": "Volume came in: a breakout in the last {bo_days} days worth ≥ ${bo_min_value:g}M, or today ≥ {today_min_chg:g}% on ≥ {bo_min_vol:g}x volume",
              "R3": "ADR ≥ {min_adr:g}%",
              "R4": "Near the pivot: within {max_off_adr:g} ADR of the 10-week high",
              "R5": "Tight: NR4 ÷ ADR ≤ {max_reltight:g}",
              "R6": "On the 10MA: {ma_min:g} to +{ma_max:g} ADR from the 10-day MA",
              "R7": "Weekly: ≤ {wk_full:g} ADR from the 10-week MA (½ up to {wk_half:g})"},
    "Hourly": {"R1": "Liquid: avg hourly $ volume ≥ {min_liq:g}M",
               "R2": "Volume: this hour ≥ {bo_min_vol:g}x the hourly average, or today's volume ≥ {day_min_vol:g}x normal",
               "R3": "ADR ≥ {min_adr:g}%",
               "R4": "Daily trend: {ma_min:g} to +{dma_max:g} ADR from the daily 10EMA",
               "R5": "Tight: hourly NR4 ÷ ADR ≤ {max_reltight:g}",
               "R6": "On the hourly 10MA: {ma_min:g} to +{ma_max:g} ADR",
               "R7": "Daily closes bunched: ≥ {min_tight_closes} tight closes of the last 4 (½ for one less)"},
}
HIGHLIGHT = ["ADR", "Ti65", "Rel tight", "Rel Wk Dist"]
HOURLY_RANGE = 0.25      # a typical hourly candle ≈ ¼ of the daily range (ADR) — MA hold's yardstick on the hourly page


def mcfg_for(tf):
    return {"market": f"CRYPTO_{tf[0]}", "exchange": "BINANCE", "user_id": "crypto_user", "tz": "UTC", "tv_prefix": True}


def _num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else np.nan
    except Exception:
        return np.nan


# ── data ───────────────────────────────────────────────────────────────────
def _get(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    return r.text if r.status_code == 200 and r.text.strip() else None


def _parse_scan(text, cols):
    df = pd.read_csv(StringIO(text), sep="|", header=None)
    n = len(df.columns)
    df.columns = cols[:n] if n <= len(cols) else cols + [f"Extra_{i}" for i in range(len(cols), n)]
    df["Symbol"] = df["Symbol"].astype(str).str.upper().str.strip()
    for c in df.columns:
        if c not in ("Symbol", "Timestamp"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _parse_extra(text, cols):
    rows = []
    for line in text.strip().split("\n"):
        f = line.strip().split("|")
        if len(f) >= len(cols) + 2:
            rows.append([f[0], f[1]] + f[-len(cols):])
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["Symbol", "Last"] + cols)
    df["Symbol"] = df["Symbol"].astype(str).str.upper().str.strip()
    for c in ["Last"] + cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.drop(columns=["Last"])


def load_scan(st, tf):
    """Fetch the 3 performance screens + the weekly/daily screen, merge to one row per coin (cached 5 minutes)."""
    @st.cache_data(ttl=300, show_spinner=False)
    def _fetch(url):
        return _get(url)
    sc = SCREENS[tf]
    parts, counts = [], {}
    for name, url in sc["scans"].items():
        try:
            txt = _fetch(url)
            d = _parse_scan(txt, COLUMNS[tf]) if txt else None
        except Exception:
            d = None
        counts[name] = 0 if d is None else len(d)
        if d is not None and not d.empty:
            d[f"In {name}"] = 1
            if "_rs" in d.columns:
                d[f"RS {name}"] = d["_rs"]
            parts.append(d)
    if not parts:
        return pd.DataFrame(), counts
    allp = pd.concat(parts, ignore_index=True)
    marks = [c for c in allp.columns if c.startswith("In ") or c.startswith("RS ")]
    base = allp.drop_duplicates("Symbol", keep="first").drop(columns=marks)   # first screen's values (1M / 22h first)
    base = base.merge(allp.groupby("Symbol")[marks].max().reset_index(), on="Symbol", how="left")
    base["Scans"] = base[[c for c in base.columns if c.startswith("In ")]].fillna(0).sum(axis=1).astype(int)
    try:
        txt = _fetch(sc["extra"])
        ex = _parse_extra(txt, EXTRA_COLUMNS[tf]) if txt else None
    except Exception:
        ex = None
    counts["Weekly" if tf == "Daily" else "Daily/Weekly"] = 0 if ex is None else len(ex)
    if ex is not None:
        base = base.merge(ex, on="Symbol", how="left")
    return base.dropna(subset=["Last"]).reset_index(drop=True), counts


# ── rules ──────────────────────────────────────────────────────────────────
def _mark(ok, known):
    return np.where(~known, "·", np.where(ok, "✓", "✗"))


def score(df, tf, cfg):
    d = df.copy()
    adr = pd.to_numeric(d.get("Adr"), errors="coerce").replace(0, np.nan)
    d["ADR"] = adr
    d["Rel tight"] = (pd.to_numeric(d.get("_nr4"), errors="coerce") / adr).round(2)
    d["10MA (ADR)"] = (pd.to_numeric(d.get("_10madist"), errors="coerce") / adr).round(2)
    if tf == "Daily":
        liq = pd.to_numeric(d.get("_avgvol_mln"), errors="coerce")
        d["Avg vol"] = liq
        d["Vol x"] = (pd.to_numeric(d.get("dvol"), errors="coerce") / liq.replace(0, np.nan)).round(2)
        d["Chg %"] = pd.to_numeric(d.get("_chg_percentclose"), errors="coerce")
        wk = pd.to_numeric(d.get("Dist_wema10_pct"), errors="coerce")
        wk = wk.fillna(pd.to_numeric(d.get("_10wmadist"), errors="coerce"))
        d["Rel Wk Dist"] = (wk.abs() / adr).round(2)
        off = 100 - pd.to_numeric(d.get("Pctof10wkhigh"), errors="coerce")
        d["Off 10w hi %"] = off.round(1)
        d["Off 10w (ADR)"] = (off.abs() / adr).round(2)
        dsb, bov = pd.to_numeric(d.get("_days_since_bo"), errors="coerce"), pd.to_numeric(d.get("_bo_dollar_vol_mln"), errors="coerce")
        today = (d["Chg %"] >= cfg["today_min_chg"]) & (d["Vol x"] >= cfg["bo_min_vol"])
        recent = dsb.between(0, cfg["bo_days"]) & (bov >= cfg["bo_min_value"])
        d["BO"] = np.where(today, "today", np.where(recent, dsb.map(lambda x: f"{int(x)}d ago" if np.isfinite(x) else ""), ""))
        d["BO value"] = bov
        d["R1"] = _mark(liq >= cfg["min_liq"], liq.notna())
        d["R2"] = np.where(today | recent, "✓", "✗")
        d["R3"] = _mark(adr >= cfg["min_adr"], adr.notna())
        d["R4"] = _mark(d["Off 10w (ADR)"] <= cfg["max_off_adr"], d["Off 10w (ADR)"].notna())
        d["R5"] = _mark(d["Rel tight"] <= cfg["max_reltight"], d["Rel tight"].notna())
        d["R6"] = _mark(d["10MA (ADR)"].between(cfg["ma_min"], cfg["ma_max"]), d["10MA (ADR)"].notna())
        rw = d["Rel Wk Dist"]
        d["R7"] = np.where(rw.isna(), "·", np.where(rw <= cfg["wk_full"], "✓", np.where(rw <= cfg["wk_half"], "½", "✗")))
        d["_bo_today"] = today
    else:
        liq = pd.to_numeric(d.get("_avgvol_mln_hourly"), errors="coerce")
        d["Avg vol"] = liq
        d["Vol x"] = (pd.to_numeric(d.get("dvolhourly"), errors="coerce") / liq.replace(0, np.nan)).round(2)
        d["Day vol x"] = (pd.to_numeric(d.get("dvoldaily"), errors="coerce") / (24 * liq).replace(0, np.nan)).round(2)
        d["Chg %"] = pd.to_numeric(d.get("_chg_percentclose_hourly"), errors="coerce")
        dma = pd.to_numeric(d.get("Dist_dema10_pct"), errors="coerce")
        d["D10EMA (ADR)"] = (dma / adr).round(2)
        d["Rel Wk Dist"] = np.nan
        tc = pd.to_numeric(d.get("Dailytightcloses_of4"), errors="coerce")
        d["Tight closes"] = tc
        d["BO"] = np.where(d["Vol x"] >= cfg["bo_min_vol"], "this hour", np.where(d["Day vol x"] >= cfg["day_min_vol"], "today", ""))
        d["R1"] = _mark(liq >= cfg["min_liq"], liq.notna())
        d["R2"] = np.where((d["Vol x"] >= cfg["bo_min_vol"]) | (d["Day vol x"] >= cfg["day_min_vol"]), "✓", "✗")
        d["R3"] = _mark(adr >= cfg["min_adr"], adr.notna())
        d["R4"] = _mark(d["D10EMA (ADR)"].between(cfg["ma_min"], cfg["dma_max"]), d["D10EMA (ADR)"].notna())
        d["R5"] = _mark(d["Rel tight"] <= cfg["max_reltight"], d["Rel tight"].notna())
        d["R6"] = _mark(d["10MA (ADR)"].between(cfg["ma_min"], cfg["ma_max"]), d["10MA (ADR)"].notna())
        d["R7"] = np.where(tc.isna(), "·", np.where(tc >= cfg["min_tight_closes"], "✓",
                                                       np.where(tc >= cfg["min_tight_closes"] - 1, "½", "✗")))
        d["_bo_today"] = d["Vol x"] >= cfg["bo_min_vol"]
    d["Day vol"] = pd.to_numeric(d.get("dvol" if tf == "Daily" else "dvoldaily"), errors="coerce")
    # Hourly: the MA-hold formulas run on hourly candles (3-hour lows, hourly 10/20MA), so measure them in an HOURLY
    # range, not the daily ADR — otherwise the tolerances are ~4x too loose and almost every coin reads "HL on 10MA".
    unit = HOURLY_RANGE if tf == "Hourly" else 1.0
    d["MA hold"] = [gp.ma_hold_label(r.get("_hl_pct"), r.get("_low_10ma"), r.get("_low_20ma"), r.get("_hold_ma"),
                                     _num(r.get("ADR")) * unit, r.get("_20madist"), r.get("_10madist"), r.get("Rel tight"))
                    for r in d.to_dict("records")]
    hold = pd.to_numeric(d["_hold_ma"], errors="coerce") if "_hold_ma" in d.columns else pd.Series(np.nan, index=d.index)
    d["Above 10MA (of 5)"] = (hold % 10).where(hold.notna())
    d["Above 20MA (of 5)"] = (hold // 10).where(hold.notna())
    rc = [r for r, _ in RULES[tf]]
    d["Score"] = sum((d[r] == "✓").astype(float) + 0.5 * (d[r] == "½") for r in rc)
    d = _commentary(d, tf)
    return d.sort_values(["Score", "Rel tight"], ascending=[False, True], na_position="last").reset_index(drop=True)


ORH = ("Alert at the pivot → after the daily open (00:00 UTC = 11 am AEDT) wait for the 1-min engulfing breakout candle "
       "on volume; stop at that candle's low (not the low of the day)")


def _commentary(d, tf):
    setups, entries, flags = [], [], []
    for r in d.to_dict("records"):
        ok = {k: r.get(k) == "✓" for k in ("R1", "R2", "R3", "R4", "R5", "R6", "R7")}
        f = []
        if bool(r.get("_bo_today")):
            s_ = "Breakout now" if tf == "Hourly" else "Broke out today"
            e_ = ("Next session: 1-min engulfing candle above today's high after 11 am AEDT; stop at that candle's low "
                  "(skip if it opens > 1 ADR up)") if tf == "Daily" else "Volume is in: take the next 1-min engulfing break of this hour's high; stop at that candle's low"
        elif ok["R2"] and ok["R4"] and ok["R5"]:
            s_, e_ = "Breakout candle ready", ORH
        elif ok["R4"] and ok["R5"]:
            s_, e_ = "Tight at pivot, no volume yet", ORH + " · needs volume (≥ 2x)"
        elif ok["R2"] and not ok["R5"]:
            s_, e_ = "Volume came, not tight yet", "Wait for a tight day / hourly flag near the high"
        elif ok["R5"]:
            s_, e_ = "Tight, away from the pivot", "Wait: needs to work back to the high"
        else:
            s_, e_ = "Not ready", "Leave it"
        if r.get("R1") == "✗":
            f.append("Thin coin")
        rw = _num(r.get("Rel Wk Dist"))
        if np.isfinite(rw) and rw > 4:
            f.append(f"{rw:.1f} ADR from 10w: late / climax risk")
        rt = _num(r.get("Rel tight"))
        if np.isfinite(rt) and rt <= 0.5:
            f.append("Narrow range")
        if _num(r.get("Scans")) >= 3:
            f.append("In all 3 screens")
        setups.append(s_); entries.append(e_); flags.append(" · ".join(f))
    d["Setup"], d["Entry"], d["Flags"] = setups, entries, flags
    return d


def _c_setup(x):
    return {"Breakout candle ready": ff._css("#155724", "white", True), "Broke out today": ff._css("#8c008c", "white", True),
            "Breakout now": ff._css("#8c008c", "white", True), "Tight at pivot, no volume yet": ff._css("#d4edda"),
            "Volume came, not tight yet": ff._css("#fff3cd", "#664d03"), "Tight, away from the pivot": ff._css("#e2e3e5", "#41464b"),
            "Not ready": "color:#9aa7ba"}.get(str(x), "")


def _c_mahold(x):
    return (ff._css("#155724", "white", True) if x == "HL on 10 & 20MA" else ff._css("#28a745", "white", True) if x == "HL on 10MA"
            else ff._css("#8ee68e") if x == "HL on 20MA" else ff._css("#cff4fc", "#055160") if str(x).startswith("Bounced")
            else ff._css("#fff3cd", "#664d03") if x == "Lost MA" else "")


def style(v, tf, cfg):
    fmt = {"Score": "{:.1f}", "ADR": "{:.1f}", "Ti65": "{:.2f}", "Rel tight": "{:.2f}", "Rel Wk Dist": "{:.1f}",
           "10MA (ADR)": "{:.1f}", "Off 10w (ADR)": "{:.1f}", "Off 10w hi %": "{:.1f}", "D10EMA (ADR)": "{:.1f}",
           "Avg vol": "{:.1f}", "Day vol": "{:.1f}", "Vol x": "{:.1f}", "Day vol x": "{:.1f}", "Chg %": "{:+.1f}", "BO value": "{:.0f}",
           "Last": "{:g}", "Tight closes": "{:g}"}
    sty = v.style.format({k: f for k, f in fmt.items() if k in v.columns}, na_rep="–")
    rc = [r for r, _ in RULES[tf] if r in v.columns]
    if rc:
        sty = sty.map(ff._c_rule, subset=rc)
    for c, f in (("Score", ff._c_score), ("ADR", ff._hl("adr")), ("Ti65", ff._hl("ti65")), ("Rel tight", ff._hl("tight")),
                 ("Rel Wk Dist", ff._hl("relwk")), ("Vol x", gp._c_vol), ("Day vol x", gp._c_vol), ("Chg %", gp._c_chg),
                 ("10MA (ADR)", ff._c_ma10), ("D10EMA (ADR)", ff._c_ma10), ("Off 10w (ADR)", ff._c_adr_dist),
                 ("Setup", _c_setup), ("MA hold", _c_mahold), ("Flags", ff._c_flags)):
        if c in v.columns:
            sty = sty.map(f, subset=[c])
    if "Avg vol" in v.columns:
        sty = sty.apply(lambda col: gp._liq_styles(col, cfg["min_liq"]), subset=["Avg vol"])
    return sty


def _tips(tf, cfg):
    t = {r: RULE_HELP[tf][r].format(**cfg) for r, _ in RULES[tf]}
    t.update({"ADR": "Average daily range %. Yellow ≥ 6 · pale ≥ 4.5 · red below",
              "Ti65": "Trend intensity. Yellow ≥ 1.25 · pale ≥ 1.1 · red < 1",
              "Rel tight": "NR4 ÷ ADR — lower is tighter. Yellow ≤ 0.5 · pale ≤ 0.8 · red > 1.2",
              "Rel Wk Dist": "ADRs from the 10-week MA. Yellow ≤ 2 · pale 2–3 · red > 3",
              "Score": "Rules passed out of 7 (½ counts half)",
              "Setup": "Breakout candle ready = volume came + tight + at the pivot",
              "Off 10w (ADR)": "Below the 10-week high in ADRs (crypto pivot proxy — MarketInOut gives no 1-month high here)",
              "Vol x": "Today's volume ÷ average" if tf == "Daily" else "This hour's volume ÷ hourly average",
              "Day vol x": "Today's volume ÷ (24 × hourly average)", "Scans": "How many of the 3 performance screens it's in",
              "Flag": "Tick to flag, then Save flags", "Pick": "Draw the range + set the alert",
              "Day vol": "Today's $ volume (same units as Avg vol)",
              "MA hold": ff._tips(ff.cfg_for({"market": "USA"})).get("MA hold", "") +
                         (" On the hourly page the MAs are the HOURLY 10/20MA." if tf == "Hourly" else "")})
    return t


LABELS = {"Flag": "⚑", "ADR": "🖍 ADR", "Ti65": "🖍 Ti65", "Rel tight": "🖍 Rel tight", "Rel Wk Dist": "🖍 Rel Wk Dist",
          "Entry": "Entry plan"}
CARD_FIRST = ["Score", "Setup", "MA hold", "🖍 ADR", "🖍 Ti65", "🖍 Rel tight", "🖍 Rel Wk Dist", "Day vol", "Avg vol", "NR4 %",
              "NR4 prev %", "10MA %", "20MA %"]
CARD_NAMES = {"dvol": "Day vol", "dvolhourly": "Hour vol", "dvoldaily": "Day vol", "_nr4": "NR4 %", "_nr4_previous": "NR4 prev %",
              "_10madist": "10MA %", "_20madist": "20MA %", "_10wmadist": "10wMA %", "_period_perf": "Period perf %",
              "_days_since_bo": "Days since BO", "_bo_dollar_vol_mln": "BO $ value", "_insideday": "Inside day",
              "_wtc": "Weekly tight closes", "_rs": "RS", "cap": "Market cap", "Timestamp": "Data time"}
SHOW = {"Daily": ["Flag", "Symbol", "Score", "Setup", "MA hold"] + HIGHLIGHT + ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "BO", "BO value",
                  "Off 10w (ADR)", "Off 10w hi %", "10MA (ADR)", "Avg vol", "Day vol", "Chg %", "Vol x", "Scans", "Flags", "Last"],
        "Hourly": ["Flag", "Symbol", "Score", "Setup", "MA hold", "ADR", "Ti65", "Rel tight", "R1", "R2", "R3", "R4", "R5", "R6", "R7", "BO",
                   "D10EMA (ADR)", "10MA (ADR)", "Tight closes", "Avg vol", "Day vol", "Chg %", "Vol x", "Day vol x", "Scans", "Flags", "Last"]}
NUMERIC = ["Score", "ADR", "Ti65", "Rel tight", "Rel Wk Dist", "10MA (ADR)", "Off 10w (ADR)", "Off 10w hi %", "D10EMA (ADR)",
           "Avg vol", "Day vol", "Vol x", "Day vol x", "Chg %", "BO value", "Last", "Tight closes", "Scans"]


def _extra(df, shown):
    skip = set(shown) | {"_bo_today", "Flag"}
    ex = df[[c for c in df.columns if c not in skip and not str(c).startswith("In ")]].copy()
    ex = ex.loc[:, [c for c in ex.columns if ex[c].notna().any()]]
    return ex.rename(columns={c: CARD_NAMES.get(c, c) for c in ex.columns})


# ── page ───────────────────────────────────────────────────────────────────
def render_crypto_page(st, sb):
    st.title("Crypto Focus")
    st.caption("One page for daily and hourly crypto. Scan → 7 rules → ⚑ flag on the row → pick 3–5 → copy to TradingView. "
               "Daily candle opens 00:00 UTC (11 am AEDT). Entry: the 1-min engulfing breakout candle on volume, "
               "stop at that candle's low.")
    tf = st.radio("Timeframe", ["Daily", "Hourly"], horizontal=True, key="cr_tf")
    mcfg = mcfg_for(tf)
    own = dict(CFG[tf])
    with st.expander("Rules (thresholds)"):
        cols = st.columns(4)
        for i, (k, v) in enumerate(own.items()):
            own[k] = cols[i % 4].number_input(CFG_LABELS.get(k, k), value=float(v), key=f"cr_cfg_{tf}_{k}",
                                              step=0.1 if isinstance(v, float) else 1.0)
    cfg = own
    c1, c2 = st.columns([1, 4])
    if c1.button("Refresh scan", key=f"cr_refresh_{tf}"):
        st.cache_data.clear()
        st.session_state.pop(f"cr_memo_{tf}", None)
    memo = st.session_state.get(f"cr_memo_{tf}")
    if memo is None:
        with st.spinner(f"Loading the {tf.lower()} crypto screens…"):
            base, counts = load_scan(st, tf)
        memo = {"base": base, "counts": counts, "at": pd.Timestamp.utcnow()}
        st.session_state[f"cr_memo_{tf}"] = memo
    base, counts = memo["base"], memo["counts"]
    c2.caption("Rows per MarketInOut screen: " + " · ".join(f"{k} {v}" for k, v in counts.items())
               + f" · loaded {memo['at']:%H:%M} UTC")
    if base.empty:
        st.warning("No data from MarketInOut — try Refresh scan."); return
    st.session_state[f"gtt_base_df_{mcfg['market']}"] = base
    t1, t2 = st.tabs(["Crypto Focus", "Alert Management"])
    with t1:
        _focus(st, sb, tf, mcfg, cfg, base)
    with t2:
        try:
            import alerts
            alerts.render_alerts_tab(st, sb, base, mcfg)
        except Exception as e:
            st.caption(f"Alert Management unavailable ({e}).")


_FRAG = None


def _focus(st, sb, tf, mcfg, cfg, base):
    """Runs as a fragment: sorting, saving flags and picks rerun only this part (fast)."""
    global _FRAG
    frag = getattr(st, "fragment", None)
    if frag is None:
        return _focus_body(st, sb, tf, mcfg, cfg, base)
    if _FRAG is None:
        _FRAG = frag(_focus_body)
    return _FRAG(st, sb, tf, mcfg, cfg, base)


def _focus_body(st, sb, tf, mcfg, cfg, base):
    _focus_main(st, sb, tf, mcfg, cfg, base)
    if sb is None or f"cr_scored_{tf}" not in st.session_state:
        return
    st.markdown("---")                     # ── Step 3: learning loop (same as Sector Focus step 5) ──
    try:
        import focus_review
        live = st.session_state[f"cr_scored_{tf}"][1]
        flags = st.session_state.get(f"cr_flags_{tf}", {})
        sd = pd.Timestamp.utcnow().date()          # crypto session = UTC day (daily candle opens 00:00 UTC)
        focus_review.render_review(st, sb, mcfg, sd, live, flags, step="3", base=base, shown_min=5,
                                   ready=("Breakout candle ready", "Tight at pivot, no volume yet", "Broke out today", "Breakout now"))
    except Exception as e:
        st.caption(f"Review unavailable ({e}).")


def _focus_main(st, sb, tf, mcfg, cfg, base):
    mkt = mcfg["market"]
    try:
        flags = ff.load_flags(sb, mcfg) if sb is not None else {}
    except Exception as e:
        st.error(f"Run supabase_flags.sql once in Supabase. ({e})"); return
    key = (tf, len(base), tuple(sorted(cfg.items())))
    sk = st.session_state.get(f"cr_scored_{tf}")
    if not sk or sk[0] != key:
        sk = (key, score(base, tf, cfg))
        st.session_state[f"cr_scored_{tf}"] = sk
    v = sk[1].copy()
    for c in NUMERIC:
        if c in v.columns:
            v[c] = pd.to_numeric(v[c], errors="coerce")
    v["Flag"] = v["Symbol"].isin(flags)
    st.session_state[f"cr_flags_{tf}"] = flags
    picked = [s for s, r in flags.items() if r.get("build_range")]
    st.session_state[f"sl_lists_{mkt}"] = {"FOCUS": picked, "FLAGGED": [s for s in flags if s not in picked]}

    st.markdown("#### 1 · Go through the scan and flag the best few")
    c1, c2 = st.columns([1, 3])
    min_score = c1.slider("Min score", 0.0, 7.0, 4.0, 0.5, key=f"cr_min_{tf}", help="Flagged coins always show")
    c2.caption("Rules: " + " · ".join(f"**{r}** {n}" for r, n in RULES[tf]) +
               ". ✓ pass · ½ half · ✗ fail · · no data. Hover a rule header for its threshold. Volume first — tightness is the entry.")
    part = v[(v["Score"] >= min_score) | v["Flag"]].reset_index(drop=True)
    if part.empty:
        st.info("Nothing at that score."); return
    cols = [c for c in SHOW[tf] if c in part.columns]
    gv = part[cols].copy()
    gv["Flag"] = gv["Flag"].astype(bool)
    was = set(part.loc[part["Flag"], "Symbol"])
    ed, save = gp.card_grid(st, gv, key=f"cr_grid_{tf}", sty=style(gv, tf, cfg), extra=_extra(part, cols), labels=LABELS,
                            tips=_tips(tf, cfg), card_first=CARD_FIRST, edit={"Flag": "bool"}, submit="Save flags",
                            height=min(32 * len(part) + 70, 600),
                            widths={"Symbol": 120, "Score": 58, "Setup": 180, "Flags": 200, "BO": 80})
    st.caption(f"{len(part)} of {len(v)} coins shown (score ≥ {min_score:g}). **Click a ticker** for all its data · tick ⚑ on the "
               "rows you like, then **Save flags** (nothing reloads while you tick or sort).")
    okey = f"cr_order_{tf}"
    if save and ed is not None and "Symbol" in ed.columns:
        st.session_state[okey] = list(ed["Symbol"])
        now = set(ed.loc[ed["Flag"].fillna(False).astype(bool), "Symbol"])
        if now != was:
            ff.add_flags(sb, mcfg, sorted(now - was), source=tf)
            ff.remove_flags(sb, mcfg, was - now)
            st.toast(f"Flagged {len(now - was)}, unflagged {len(was - now)}.")
            ff._rerun(st)
    order = [x for x in st.session_state.get(okey, []) if x in set(part["Symbol"])] or list(part["Symbol"])
    gp.copy_area(st, f"{len(order)} coins in the table's order (sort the table, press Save to refresh) — for TradingView",
                 ",".join(gp.tv_symbol(x, mcfg) for x in order), key=f"cr_copy_scan_{tf}")

    st.markdown("---")
    st.markdown("#### 2 · Flagged — pick your 3–5")
    with st.expander("⚑ Flag coins by hand"):
        txt = st.text_input("Symbols, comma separated", key=f"cr_manual_{tf}", placeholder="e.g. INJUSDT, BINANCE:TIAUSDT")
        if st.button("Flag them", key=f"cr_manual_btn_{tf}") and txt.strip():
            syms = [s.strip().upper().split(":")[-1].replace(".P", "") for s in txt.replace("\n", ",").split(",") if s.strip()]
            ff.add_flags(sb, mcfg, syms, source="manual")
            st.toast(f"Flagged {len(syms)}."); ff._rerun(st)
    if not flags:
        st.caption("Nothing flagged yet."); return
    fl = pd.DataFrame(flags.values())
    d = fl.merge(v.drop(columns=["Flag"]), left_on="symbol", right_on="Symbol", how="left")
    d["Symbol"] = d["symbol"]
    gone = d["Score"].isna()
    if gone.any():                                         # not in the current scan → never show old numbers
        d.loc[gone, "Setup"] = "Dropped out of the scan"
        d.loc[gone, "Flags"] = "Not in the current scan"
        d.loc[gone, "Entry"] = "No longer passes the screens — check the chart; unflag unless you hold it"
    d["Pick"] = d["build_range"].fillna(False).astype(bool)
    d["Rank"] = pd.to_numeric(d["rank"], errors="coerce")
    d["Note"] = d["note"].fillna("")
    d["Unflag"] = False
    d = d.sort_values(["Pick", "Rank", "Score"], ascending=[False, True, False], na_position="last").reset_index(drop=True)
    show = ["Pick", "Rank", "Symbol", "Setup", "Entry", "Flags", "Score"] + [c for c in SHOW[tf] if c not in
            ("Flag", "Symbol", "Score", "Setup", "Flags")] + ["Note", "Unflag"]
    show = [c for c in show if c in d.columns]
    n_pick = int(d["Pick"].sum())
    st.caption(f"**{len(d)} flagged · {n_pick} picked** (aim for {int(cfg['target'])}). Tick **Pick** = draw the range and set the "
               "alert. Rank 1 = best.")
    if n_pick > cfg["target"]:
        st.warning(f"{n_pick} picked — cut to {int(cfg['target'])} or fewer.")
    gv = d[show].copy()
    for c in ("Pick", "Unflag"):
        gv[c] = gv[c].astype(bool)
    ed, saved = gp.card_grid(st, gv, key=f"cr_fgrid_{tf}", sty=style(gv, tf, cfg), extra=_extra(d, show),
                             labels=LABELS, tips=_tips(tf, cfg), card_first=CARD_FIRST,
                             edit={"Pick": "bool", "Rank": "number", "Note": "text", "Unflag": "bool"}, submit="Save shortlist",
                             height=min(32 * len(d) + 70, 480), widths={"Symbol": 120, "Score": 58, "Setup": 180, "Entry": 300, "Flags": 200})
    st.caption("**Click a ticker** for all its data · tick Pick / Unflag, type a Rank or Note on the row, then **Save shortlist**.")
    if saved and ed is not None and "Symbol" in ed.columns:
        st.session_state[f"cr_forder_{tf}"] = list(ed["Symbol"])
        orig, changed = d.set_index("Symbol"), False
        for r in ed.to_dict("records"):
            s = r["Symbol"]
            if s not in orig.index:
                continue
            if r.get("Unflag"):
                ff.remove_flags(sb, mcfg, [s]); changed = True; continue
            o, ch = orig.loc[s], {}
            rk = _num(r.get("Rank"))
            if bool(r.get("Pick")) != bool(o["Pick"]):
                ch["build_range"] = bool(r.get("Pick"))
            if (rk if np.isfinite(rk) else None) != (o["Rank"] if np.isfinite(_num(o["Rank"])) else None):
                ch["rank"] = int(rk) if np.isfinite(rk) else None
            if str(r.get("Note") or "") != str(o["Note"] or ""):
                ch["note"] = str(r.get("Note") or "")
            if ch:
                ff.update_flag(sb, mcfg, s, **ch); changed = True
        if changed:
            st.toast("Saved."); ff._rerun(st)
    picks = d[d["Pick"]].sort_values("Rank", na_position="last")["Symbol"].tolist()
    if picks:
        gp.copy_area(st, f"Picked {len(picks)} — draw ranges and set alerts (TradingView)",
                     ",".join(gp.tv_symbol(s, mcfg) for s in picks), key=f"cr_copy_pick_{tf}")
    order = [x for x in st.session_state.get(f"cr_forder_{tf}", []) if x in set(d["Symbol"])]
    order += [x for x in d["Symbol"] if x not in order]
    gp.copy_area(st, f"All {len(order)} flagged, in the table's order (sort, press Save to refresh) — for TradingView",
                 ",".join(gp.tv_symbol(x, mcfg) for x in order), key=f"cr_copy_all_{tf}")
