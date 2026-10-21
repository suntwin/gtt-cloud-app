"""
Sector Focus — the evening process in one tab, sector by sector:

  1. Sectors   pick 2–3 focus sectors (leadership = breakouts recorded in your evening batches)
  2. Tickers   work one sector at a time: every stock of that sector (today's scan, recent breakouts, watchlist)
               with the 8 rules as ✓ / ½ / ✗ and a score — flag the few worth a range
  3. Flagged   every flagged stock (all sectors, kept across days): rank, note, tick "Build range" → your 5–6

The 8 rules (volume first, tightness last):
  1 Liquid      avg traded value ≥ floor (NSE ₹50 cr · USA $30M)
  2 Volume      a breakout in the last 10 sessions on ≥ 2x volume or ≥ ₹100 cr / $50M traded
  3 Sector      in your focus sectors
  4 ADR         ≥ 4.5% NSE · ≥ 5% USA
  5 Pivot       within 1 ADR of the 1-month high and ≤ 15% off the 52-week high
  6 Tight       rel tight (NR4 ÷ ADR) ≤ 0.8
  7 10MA        −0.3 to +1 ADR from the 10-day MA
  8 Weekly      ≤ 2 ADR from the 10-week MA (½ for 2–3, ✗ above 3)
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd

import gtt_process as gp

RULES = [("R1", "Liquid"), ("R2", "Volume"), ("R3", "Sector"), ("R4", "ADR"),
         ("R5", "Pivot"), ("R6", "Tight"), ("R7", "10MA"), ("R8", "Weekly")]
CFG = {"min_liq": 50.0, "bo_days": 10, "bo_min_vol": 2.0, "bo_min_value": 100.0, "today_min_chg": 3.0,
       "min_adr": 4.5, "max_off1m_adr": 1.0, "max_off52": 15.0, "max_reltight": 0.8,
       "ma10_min_adr": -0.3, "ma10_max_adr": 1.0, "wk_full": 2.0, "wk_half": 3.0, "flag_days": 7, "target": 6}
MARKET_CFG = {"NSE": {"min_liq": 50.0, "bo_min_value": 100.0, "min_adr": 4.5},
              "USA": {"min_liq": 30.0, "bo_min_value": 50.0, "min_adr": 5.0}}
RULE_HELP = {
    "R1": "Liquid: avg traded value ≥ {min_liq:g}",
    "R2": "Volume came in: a breakout in the last {bo_days} sessions on ≥ {bo_min_vol:g}x volume or ≥ {bo_min_value:g} traded",
    "R3": "Sector: in your focus sectors",
    "R4": "ADR ≥ {min_adr:g}%",
    "R5": "Near the pivot: within {max_off1m_adr:g} ADR of the 1-month high and ≤ {max_off52:g}% off the 52-week high",
    "R6": "Tight: rel tight (NR4 ÷ ADR) ≤ {max_reltight:g}",
    "R7": "On the 10MA: {ma10_min_adr:g} to +{ma10_max_adr:g} ADR from the 10-day MA",
    "R8": "Weekly: ≤ {wk_full:g} ADR from the 10-week MA (½ for {wk_full:g}–{wk_half:g}, ✗ above)",
}
HIGHLIGHT = ["ADR", "Ti65", "Rel tight", "ADRs from 10w"]      # the four that matter most — highlighter colours
SHOW = ["Flag", "Symbol", "Score", "Setup"] + HIGHLIGHT + ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "Source", "BO",
        "BO vol x", "Off 1M (ADR)", "Off 52W %", "10MA (ADR)", "Avg vol", "Day vol", "Chg %", "Vol x", "Circuit %", "Flags", "Data"]
NUMERIC = ["Score", "ADR", "Ti65", "Rel tight", "ADRs from 10w", "BO vol x", "Off 1M (ADR)", "Off 52W %", "10MA (ADR)",
           "Avg vol", "Day vol", "Chg %", "Vol x", "Circuit %"]


def _num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else np.nan
    except Exception:
        return np.nan


LABELS = {"Flag": "⚑", "ADR": "🖍 ADR", "Ti65": "🖍 Ti65", "Rel tight": "🖍 Rel tight", "ADRs from 10w": "🖍 Rel Wk Dist",
          "R1": "R1 Liquid", "R2": "R2 Volume", "R3": "R3 Sector", "R4": "R4 ADR", "R5": "R5 Pivot", "R6": "R6 Tight",
          "R7": "R7 10MA", "R8": "R8 Weekly", "Entry": "Entry plan"}
CARD_NAMES = {"Last": "Last", "_chg_percentclose": "Chg %", "_vol_ratio": "Vol ratio", "dvol": "Day vol",
              "_nr4": "NR4 %", "_nr4_previous": "NR4 prev %", "_rel_tightness_prev": "Rel tight prev",
              "off_high_21": "Off 1M hi %", "off_high_126": "Off 6M hi %", "_10madist": "10MA %", "_20madist": "20MA %",
              "W_Dist10wMA": "Wk 10wMA %", "W_CloseChg_Pct": "Wk close chg %", "W_TightCloses_10w": "Wk tight closes",
              "W_InsideBars": "Wk inside bars", "W_PctOf10wkHigh": "Wk % of 10w high", "Avg_RS": "Avg RS",
              "RS_1M": "RS 1M", "RS_3M": "RS 3M", "RS_6M": "RS 6M", "Sector_Percentile": "Sector %ile",
              "_days_since_bo": "Days since BO", "_bo_dollar_vol_mln": "BO value", "_avg_vol_float_ratio": "Vol / float",
              "Industry": "Industry", "Sector": "Sector", "Entry": "Entry plan", "Pivot": "Pivot", "Stop guide": "Stop guide"}


CARD_FIRST = ["Score", "Setup", "🖍 ADR", "🖍 Ti65", "🖍 Rel tight", "🖍 Rel Wk Dist", "Day vol", "Avg vol", "NR4 %",
              "NR4 prev %", "Avg RS", "Sector %ile", "10MA %", "20MA %"]


def _tips(cfg):
    """Header hover text: the rule thresholds and what the key columns mean."""
    t = {r: RULE_HELP[r].format(**cfg) for r, _ in RULES}
    t.update({"ADR": "Average daily range %. Yellow ≥ 6 · pale ≥ 4.5 · red below",
              "Ti65": "Trend intensity (price ÷ 65-day avg). Yellow ≥ 1.25 · pale ≥ 1.1 · red < 1",
              "Rel tight": "Today's NR4 ÷ ADR — lower is tighter. Yellow ≤ 0.5 · pale ≤ 0.8 · red > 1.2",
              "ADRs from 10w": "Relative weekly distance: ADRs from the 10-week MA. Yellow ≤ 2 · pale 2–3 · red > 3",
              "Score": "Rules passed out of 8 (½ counts half)",
              "Setup": "1-min ORH ready = volume came + tight + within 1 ADR of the pivot · Leader flag on 10MA = extended "
                       "weekly but resting on the 10MA · Tight at pivot, no volume yet = anticipation",
              "Flag": "Tick to flag, then Save flags", "Pick": "Build the range + set the alert",
              "Pivot": "1-month high: starting line for the range", "Stop guide": "Widest stop: pivot − 1 ADR (use the LOD if tighter)",
              "Off 1M (ADR)": "Below the 1-month high, in ADRs",
              "Day vol": "Today's traded value (same units as Avg vol) — coloured by today ÷ average", "10MA (ADR)": "Distance from the 10-day MA, in ADRs",
              "Source": "Breakout = volume came in recently · P1/P2/P3 = anticipation tier (ADR)",
              "Data": "today = today's scan · dropped out = not in today's scan (no numbers shown)"})
    return t


def _card_extra(df, shown):
    """Every other attribute of each row for the click card (friendly names, skips internals and empties)."""
    keep = [c for c in df.columns if c not in shown and c not in ("Flag", "_bo_age", "_bo_chg", "Scan_Count", "Missing_Weekly",
                                                                  "Data_Date", "_off21_adr", "_ma10_adr")]
    ex = df[keep].copy()
    ex = ex.loc[:, [c for c in ex.columns if ex[c].notna().any()]]
    return ex.rename(columns={c: CARD_NAMES.get(c, c) for c in ex.columns})


def cfg_for(mcfg):
    return {**CFG, **MARKET_CFG.get(mcfg.get("market"), {})}


# ── flags (Supabase table `flags`) ─────────────────────────────────────────
def load_flags(sb, mcfg):
    rows = (sb.table("flags").select("*").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
            .execute().data or [])
    return {r["symbol"]: r for r in rows}


def add_flags(sb, mcfg, syms, sector=None, source=None, sectors=None):
    rows = [{"user_id": mcfg["user_id"], "market": mcfg["market"], "symbol": s,
             "sector": (sectors or {}).get(s, sector), "source": source, "flagged_at": date.today().isoformat()}
            for s in syms]
    if rows:
        sb.table("flags").upsert(rows, on_conflict="user_id,market,symbol").execute()


def remove_flags(sb, mcfg, syms):
    syms = list(syms)
    if syms:
        sb.table("flags").delete().eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"]).in_("symbol", syms).execute()


def update_flag(sb, mcfg, sym, **fields):
    fields["updated_at"] = pd.Timestamp.utcnow().isoformat()
    sb.table("flags").update(fields).eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"]).eq("symbol", sym).execute()


# ── data: every stock with the numbers the rules need ──────────────────────
def _universe(sb, base_df, mcfg, hist, watch):
    """One row per stock: today's scan + recent breakouts + watchlist, with Sector, live or last-saved numbers."""
    live = gp.add_derived(base_df).drop_duplicates("Symbol") if base_df is not None and not base_df.empty else pd.DataFrame()
    if not live.empty:
        live["Symbol"] = live["Symbol"].astype(str).str.upper()
        live["Data"] = "today"
    syms = set(live["Symbol"]) if not live.empty else set()
    sec = {}
    if hist is not None and not hist.empty:
        for r in hist.sort_values("date").itertuples():
            if isinstance(r.Sector, str) and r.Sector != "Unknown":
                sec[str(r.Symbol).upper()] = r.Sector
    for w in watch or []:
        if isinstance(w.get("sector"), str) and w["sector"]:
            sec[str(w["symbol"]).upper()] = w["sector"]
    extra = sorted((set(sec) | {str(w["symbol"]).upper() for w in (watch or [])}) - syms)
    rows = []
    if extra and sb is not None:
        snap = gp._last_snapshots(sb, mcfg, extra)
        for s in extra:
            d_, m = snap.get(s, ("", {}))
            r = {k: v for k, v in (m or {}).items() if k != "setups"}
            r["Symbol"] = s
            r["Sector"] = sec.get(s) or r.get("Sector")
            r["Data"] = f"as of {pd.to_datetime(d_):%d-%b}" if d_ else "not in scan"
            rows.append(r)
    u = pd.concat([live, pd.DataFrame(rows)], ignore_index=True) if rows else live.copy()
    if u.empty:
        return u
    if "Sector" not in u.columns:
        u["Sector"] = np.nan
    u["Sector"] = u["Sector"].where(u["Sector"].notna() & (u["Sector"] != "Unknown"), u["Symbol"].map(sec))
    for c in ("Adr", "Ti65", "_avgvol_mln", "_rel_tightness_today", "off_high_21", "off_high_252", "_10madist", "_rel_wk_dist",
              "_chg_percentclose", "_vol_ratio", "_circuit", "_days_since_bo", "_bo_dollar_vol_mln", "W_Dist10wMA", "_nr4"):
        u[c] = pd.to_numeric(u[c], errors="coerce") if c in u.columns else np.nan
    adr = u["Adr"].replace(0, np.nan)
    u["_rel_tightness_today"] = u["_rel_tightness_today"].fillna((u["_nr4"] / adr).round(2))
    u["_rel_wk_dist"] = u["_rel_wk_dist"].fillna((u["W_Dist10wMA"].abs() / adr).round(2))
    u["_off21_adr"] = (u["off_high_21"].abs() / adr).round(2)
    u["_ma10_adr"] = (u["_10madist"] / adr).round(2)
    if mcfg.get("market") == "NSE" and sb is not None:
        try:
            import nse_bands
            bands = nse_bands.load_bands(sb)
            u["_circuit"] = u["Symbol"].map(bands).fillna(u["_circuit"])
        except Exception:
            pass
    return u


def _breakouts(u, hist, sd, cfg):
    """Latest volume breakout per stock in the last bo_days sessions: (label, vol x, value, age)."""
    out = {}
    if hist is not None and not hist.empty:
        sessions = sorted(hist["date"].unique(), reverse=True)
        pos = {d: i for i, d in enumerate(sessions)}
        off0 = 0 if sessions and sessions[0] == str(sd) else 1
        h = hist.sort_values("date", ascending=False).drop_duplicates("Symbol")
        for r in h.itertuples():
            age = pos[r.date] + off0 if r.date != str(sd) else 0
            val = _num(r.vol) * _num(r.avgvol)
            out[str(r.Symbol).upper()] = (f"{pd.to_datetime(r.date):%d-%b}", _num(r.vol), val, age, _num(r.chg))
    for r in u.itertuples():
        s = r.Symbol
        dsb, val = _num(getattr(r, "_days_since_bo", np.nan)), _num(getattr(r, "_bo_dollar_vol_mln", np.nan))
        if np.isfinite(dsb) and dsb <= cfg["bo_days"] and (s not in out or dsb < out[s][3]):
            prev = out.get(s, (None, np.nan, np.nan, np.nan, np.nan))
            out[s] = (f"{int(dsb)}d ago", prev[1], val, dsb, prev[4])
        chg, vr = _num(getattr(r, "_chg_percentclose", np.nan)), _num(getattr(r, "_vol_ratio", np.nan))
        if r.Data == "today" and chg >= cfg["today_min_chg"] and vr >= cfg["bo_min_vol"]:
            out[s] = ("today", vr, vr * _num(getattr(r, "_avgvol_mln", np.nan)), 0, chg)
    return out


def score(u, cfg, focus, bos, tiers=None):
    """Adds R1..R8 (✓ / ½ / ✗ / · unknown), Score and display columns."""
    v = u.copy()

    def mark(ok, known):
        return np.where(~known, "·", np.where(ok, "✓", "✗"))

    liq = v["_avgvol_mln"]
    v["R1"] = mark(liq >= cfg["min_liq"], liq.notna())
    bo = v["Symbol"].map(bos)
    age = bo.map(lambda x: x[3] if isinstance(x, tuple) else np.nan)
    bvol = bo.map(lambda x: x[1] if isinstance(x, tuple) else np.nan)
    bval = bo.map(lambda x: x[2] if isinstance(x, tuple) else np.nan)
    v["R2"] = np.where((age <= cfg["bo_days"]) & ((bvol >= cfg["bo_min_vol"]) | (bval >= cfg["bo_min_value"])), "✓", "✗")
    v["R3"] = np.where(v["Sector"].isin(focus), "✓", "✗")
    v["R4"] = mark(v["Adr"] >= cfg["min_adr"], v["Adr"].notna())
    o52 = v["off_high_252"].abs()
    v["R5"] = mark((v["_off21_adr"] <= cfg["max_off1m_adr"]) & ((o52 <= cfg["max_off52"]) | o52.isna()), v["_off21_adr"].notna())
    v["R6"] = mark(v["_rel_tightness_today"] <= cfg["max_reltight"], v["_rel_tightness_today"].notna())
    v["R7"] = mark(v["_ma10_adr"].between(cfg["ma10_min_adr"], cfg["ma10_max_adr"]), v["_ma10_adr"].notna())
    wk = v["_rel_wk_dist"]
    v["R8"] = np.where(wk.isna(), "·", np.where(wk <= cfg["wk_full"], "✓", np.where(wk <= cfg["wk_half"], "½", "✗")))
    pts = sum((v[r] == "✓").astype(float) + 0.5 * (v[r] == "½") for r, _ in RULES)
    v["Score"] = pts
    v["BO"] = bo.map(lambda x: x[0] if isinstance(x, tuple) else "")
    v["_bo_age"] = age
    v["_bo_chg"] = bo.map(lambda x: x[4] if isinstance(x, tuple) and len(x) > 4 else np.nan)
    v["BO vol x"] = bvol.round(1)
    src = []
    tiers = tiers or {}
    for s, b in zip(v["Symbol"], v["BO"]):
        parts = []
        if b:
            parts.append("Breakout")
        if tiers.get(s):
            parts.append(tiers[s])
        src.append(" · ".join(parts))
    v["Source"] = src
    v = v.rename(columns={"Adr": "ADR", "_rel_tightness_today": "Rel tight", "_off21_adr": "Off 1M (ADR)",
                          "off_high_252": "Off 52W %", "_ma10_adr": "10MA (ADR)", "_rel_wk_dist": "ADRs from 10w",
                          "_avgvol_mln": "Avg vol", "dvol": "Day vol", "_chg_percentclose": "Chg %", "_vol_ratio": "Vol x", "_circuit": "Circuit %"})
    for c in NUMERIC:
        if c in v.columns:
            v[c] = pd.to_numeric(v[c], errors="coerce")
    return v.sort_values(["Score", "Rel tight"], ascending=[False, True], na_position="last")


# ── commentary: setup, entry plan and warnings (what the Shortlister used to say) ──
def commentary(v, cfg, mkt):
    """Adds Setup, Entry, Flags, Pivot, To pivot %, Stop guide. Works on the frame score() returns."""
    v = v.copy()
    last = pd.to_numeric(v.get("Last"), errors="coerce") if "Last" in v.columns else pd.Series(np.nan, index=v.index)
    off = pd.to_numeric(v.get("off_high_21"), errors="coerce").abs() if "off_high_21" in v.columns else pd.Series(np.nan, index=v.index)
    adr = v["ADR"]
    piv = (last / (1 - off / 100)).where(off < 100)
    v["Pivot"] = piv.round(2)
    v["To pivot %"] = ((piv / last - 1) * 100).round(1)
    v["Stop guide"] = (piv * (1 - adr / 100)).round(2)
    setups, entries, flags = [], [], []
    for r in v.to_dict("records"):
        ok = {k: r.get(k) == "✓" for k, _ in RULES}
        age, rwd, ma10 = _num(r.get("_bo_age")), _num(r.get("ADRs from 10w")), _num(r.get("10MA (ADR)"))
        ti, rt, o1 = _num(r.get("Ti65")), _num(r.get("Rel tight")), _num(r.get("Off 1M (ADR)"))
        leader = (np.isfinite(rwd) and rwd > cfg["wk_half"] and np.isfinite(ma10) and cfg["ma10_min_adr"] <= ma10 <= cfg["ma10_max_adr"]
                  and np.isfinite(ti) and ti >= 1.1 and ok["R6"] and np.isfinite(o1) and o1 <= 2)
        orh = "Alert at the pivot → 1-min ORH on the break, first 30–45 min, Rel vol ≥ 2"
        if age == 0:
            s_, e_ = "Broke out today", ("Day 2: 1-min ORH above today's high, first 30–45 min, Rel vol ≥ 2 "
                                         "(skip if it opens > 1 ADR up or today left a big wick)")
        elif ok["R2"] and ok["R5"] and ok["R6"]:
            s_, e_ = "1-min ORH ready", orh
        elif leader:
            s_, e_ = "Leader flag on 10MA", orh + " · half size, pivot must clear any climax high (3rd+ step)"
        elif ok["R5"] and ok["R6"]:
            s_, e_ = "Tight at pivot, no volume yet", orh + " · needs a volume breakout (≥ 2x)"
        elif ok["R2"] and not ok["R6"]:
            s_, e_ = "Volume came, not tight yet", "Wait for a tight day or an hourly flag near the high"
        elif np.isfinite(o1) and o1 <= 2:
            s_, e_ = "Near the pivot", "Wait: needs tighter action and/or volume"
        else:
            s_, e_ = "Not ready", "Leave it — far from the pivot"
        f = []
        if r.get("EP") or (np.isfinite(_num(r.get("_bo_chg"))) and _num(r.get("_bo_chg")) >= 8 and _num(r.get("BO vol x")) >= 4):
            f.append("EP: check the news")
        bv = _num(r.get("BO vol x"))
        if not r.get("BO"):
            f.append("No recent volume BO")
        elif np.isfinite(bv) and bv < cfg["bo_min_vol"]:
            f.append(f"Volume {bv:.1f}x (< {cfg['bo_min_vol']:g}x)")
        o52 = _num(r.get("Off 52W %"))
        if np.isfinite(o52) and abs(o52) <= 1:
            f.append("At 52W high")
        if np.isfinite(rt) and rt <= 0.5 and age != 0:
            f.append("Narrow day")
        if r.get("R1") == "✗":
            f.append("Thin stock")
        if np.isfinite(rwd) and rwd > 4:
            f.append(f"{rwd:.1f} ADR from 10w: count steps / climax high")
        circ = _num(r.get("Circuit %"))
        if mkt == "NSE" and np.isfinite(circ) and 0 < circ <= 5:
            f.append(f"Circuit {circ:g}%: stop may not fill")
        setups.append(s_); entries.append(e_); flags.append(" · ".join(f))
    v["Setup"], v["Entry"], v["Flags"] = setups, entries, flags
    return v


# ── styling ────────────────────────────────────────────────────────────────
def _css(bg, fg="black", bold=False):
    return f"background-color:{bg};color:{fg}" + (";font-weight:bold" if bold else "")


def _c_rule(x):
    return {"✓": _css("#28a745", "white", True), "½": _css("#fff3cd", "#664d03", True),
            "✗": _css("#f8d7da", "#721c24"), "·": "color:#9aa7ba"}.get(str(x), "")


def _c_score(x):
    x = _num(x)
    if not np.isfinite(x):
        return ""
    return _css("#155724", "white", True) if x >= 7 else _css("#28a745", "white", True) if x >= 6 else \
        _css("#8ee68e") if x >= 5 else _css("#d4edda") if x >= 4 else ""


def _c_data(x):
    s = str(x)
    return "color:#28a745" if s == "today" else ("color:#dc3545;font-weight:bold" if s == "dropped out" else "color:#9aa7ba")


def _hl(kind):
    """Highlighter colours for the four key columns: bright yellow = great, pale = fine, red = poor."""
    return lambda v: gp._card_colour(kind, v, {})


def _c_adr_dist(v, good=0.5, ok=1.0, fair=2.0):
    v = _num(v)
    if not np.isfinite(v):
        return ""
    a = abs(v)
    return _css("#28a745", "white", True) if a <= good else _css("#8ee68e") if a <= ok else \
        _css("#d4edda") if a <= fair else _css("#fff3cd", "#664d03")


def _c_ma10(v):
    v = _num(v)
    if not np.isfinite(v):
        return ""
    if v < -0.3:
        return _css("#f8d7da", "#721c24")
    return _c_adr_dist(v)


def style(v, liq_floor=50.0):
    fmt = {"Score": "{:.1f}", "BO vol x": "{:.1f}", "ADR": "{:.1f}", "Ti65": "{:.2f}", "Rel tight": "{:.2f}",
           "Off 1M (ADR)": "{:.1f}", "Off 52W %": "{:.1f}", "10MA (ADR)": "{:.1f}", "ADRs from 10w": "{:.1f}",
           "Avg vol": "{:.0f}", "Day vol": "{:.0f}", "Chg %": "{:+.1f}", "Vol x": "{:.1f}", "Circuit %": "{:.0f}",
           "Pivot": "{:g}", "To pivot %": "{:.1f}", "Stop guide": "{:g}"}
    sty = v.style.format({k: f for k, f in fmt.items() if k in v.columns}, na_rep="–")
    rc = [r for r, _ in RULES if r in v.columns]
    if rc:
        sty = sty.map(_c_rule, subset=rc)
    for c, f in (("Score", _c_score), ("Data", _c_data), ("Circuit %", gp._c_circuit),
                 ("ADR", _hl("adr")), ("Ti65", _hl("ti65")), ("Rel tight", _hl("tight")), ("ADRs from 10w", _hl("relwk")),
                 ("BO vol x", gp._c_vol), ("Vol x", gp._c_vol), ("Chg %", gp._c_chg),
                 ("Off 1M (ADR)", _c_adr_dist), ("Off 52W %", gp._c_offhigh_long), ("10MA (ADR)", _c_ma10)):
        if c in v.columns:
            sty = sty.map(f, subset=[c])
    if "Avg vol" in v.columns:
        sty = sty.apply(lambda col: gp._liq_styles(col, liq_floor), subset=["Avg vol"])
    if "Day vol" in v.columns and "Avg vol" in v.columns:
        av = pd.to_numeric(v["Avg vol"], errors="coerce").tolist()
        sty = sty.apply(lambda col: [gp._c_vol(_num(x) / a) if np.isfinite(_num(a)) and _num(a) else ""
                                     for x, a in zip(col, av)], subset=["Day vol"])
    if "Setup" in v.columns:
        sty = sty.map(_c_setup, subset=["Setup"])
    if "Flags" in v.columns:
        sty = sty.map(_c_flags, subset=["Flags"])
    if "To pivot %" in v.columns and "ADR" in v.columns:
        a_ = v["ADR"].tolist()
        sty = sty.apply(lambda col: [_c_adr_dist(_num(x) / a if np.isfinite(_num(a)) and _num(a) else np.nan)
                                     for x, a in zip(col, a_)], subset=["To pivot %"])
    return sty


def _c_setup(x):
    return {"1-min ORH ready": _css("#155724", "white", True), "Broke out today": _css("#8c008c", "white", True),
            "Leader flag on 10MA": _css("#cff4fc", "#055160", True), "Tight at pivot, no volume yet": _css("#d4edda"),
            "Volume came, not tight yet": _css("#fff3cd", "#664d03"), "Near the pivot": _css("#e2e3e5", "#41464b"),
            "Not ready": "color:#9aa7ba", "Dropped out of the scan": _css("#f8d7da", "#721c24", True)}.get(str(x), "")


def _c_flags(x):
    t = str(x)
    if "EP" in t:
        return _css("#e2d9f3", "#3d2a73", True)
    if "Not in today's scan" in t:
        return _css("#f8d7da", "#721c24", True)
    if "Thin" in t or "< 2x" in t or "No recent" in t or "Circuit" in t or "count steps" in t:
        return _css("#fff3cd", "#664d03")
    if "52W high" in t or "Narrow day" in t:
        return _css("#d4edda")
    return ""


def _colcfg(st, cfg):
    cc = {r: st.column_config.TextColumn(r, width="small", help=f"{name}: " + RULE_HELP[r].format(**cfg))
          for r, name in RULES}
    cc.update({
        "ADR": st.column_config.NumberColumn("🖍 ADR", help="Average daily range %. Yellow ≥ 6 · pale ≥ 4.5 · red below", width="small"),
        "Ti65": st.column_config.NumberColumn("🖍 Ti65", help="Trend intensity (price ÷ 65-day avg). Yellow ≥ 1.25 · pale ≥ 1.1 · red < 1", width="small"),
        "Rel tight": st.column_config.NumberColumn("🖍 Rel tight", help="Today's NR4 ÷ ADR. Yellow ≤ 0.5 · pale ≤ 0.8 · red > 1.2", width="small"),
        "ADRs from 10w": st.column_config.NumberColumn("🖍 Rel Wk Dist", help="ADRs from the 10-week MA. Yellow ≤ 2 · pale 2–3 · red > 3", width="small"),
        "Setup": st.column_config.TextColumn("Setup", width="medium",
                                             help="1-min ORH ready = volume came + tight + within 1 ADR of the pivot · Leader flag on 10MA = "
                                                  "extended weekly but resting on the 10MA · Tight at pivot, no volume yet = anticipation"),
        "Entry": st.column_config.TextColumn("Entry plan", width="large"),
        "Flags": st.column_config.TextColumn("Flags", width="medium"),
        "Pivot": st.column_config.NumberColumn("Pivot", help="1-month high: starting line for the range", width="small"),
        "To pivot %": st.column_config.NumberColumn("To pivot %", width="small"),
        "Stop guide": st.column_config.NumberColumn("Stop guide", help="Widest stop: pivot − 1 ADR. Use the LOD if tighter", width="small"),
        "Flag": st.column_config.CheckboxColumn("⚑", help="Flag for the shortlist (saved when you press Save flags)", width="small"),
        "Symbol": st.column_config.TextColumn("Symbol", width="small"),
        "Score": st.column_config.NumberColumn("Score", help="Rules passed out of 8 (½ counts half)", width="small"),
        "Source": st.column_config.TextColumn("Source", help="Breakout = volume came in recently · P1/P2/P3 = anticipation tier (ADR)"),
        "BO": st.column_config.TextColumn("BO", help="Last volume breakout", width="small"),
        "Data": st.column_config.TextColumn("Data", width="small",
                                            help="today = today's scan · as of = not in today's scan, last saved numbers"),
    })
    return cc


# ── tab ────────────────────────────────────────────────────────────────────
def _rerun(st):
    """Rerun only this tab when running as a fragment (much faster), else the page."""
    try:
        st.rerun(scope="fragment")
    except Exception:
        st.rerun()


def _prepared(st, sb, base_df, mcfg, cfg):
    """Heavy, slow-changing inputs, computed once per scan (kept in the session): breakout history, sector
    leadership, the stock universe with last-saved numbers, breakouts and tiers."""
    mkt = mcfg.get("market")
    sd, _, _ = gp.effective_session(base_df, mcfg, sb)
    key = (id(base_df), len(base_df), str(sd))
    memo = st.session_state.get(f"sf_memo_{mkt}")
    if memo and memo["key"] == key:
        return memo
    try:
        hist = gp.load_breakout_history(sb, mcfg)
    except Exception:
        hist = pd.DataFrame()
    try:
        watch = gp.load_active_watchlist(sb, mcfg)
    except Exception:
        watch = []
    t, _ = gp.sector_leadership(hist, base_df, "Sector")
    u = _universe(sb, base_df, mcfg, hist, watch)
    tiers, bos = {}, {}
    if not u.empty:
        tl = gp.adr_tier(u["Adr"], gp.TIER_DEFAULTS.get(mkt, {"tier1_adr": 7.0, "tier2_adr": 5.0}))
        tiers = {s: t_ for s, t_, d_ in zip(u["Symbol"], tl, u["Data"]) if t_ and d_ == "today"}
        bos = _breakouts(u, hist, sd, cfg)
    memo = {"key": key, "sd": sd, "t": t, "u": u, "tiers": tiers, "bos": bos, "scored": {}}
    st.session_state[f"sf_memo_{mkt}"] = memo
    return memo


def render_sector_focus_tab(st, sb, base_df, mcfg):
    """Runs as a fragment where Streamlit supports it: flag clicks and sector switches rerun only this tab."""
    frag = getattr(st, "fragment", None)
    if frag is None:
        return _render_sector_focus(st, sb, base_df, mcfg)
    global _FRAG
    if _FRAG is None:
        _FRAG = frag(_render_sector_focus)
    return _FRAG(st, sb, base_df, mcfg)


_FRAG = None


def _render_sector_focus(st, sb, base_df, mcfg):
    mkt = mcfg.get("market")
    cfg = cfg_for(mcfg)
    st.subheader("Sector Focus · sector → tickers → flagged → 5–6 names")
    if base_df is None or getattr(base_df, "empty", True):
        st.info("Generate data first (GTT Scanner tab)."); return
    if sb is None:
        st.error("Database not connected."); return
    try:
        flags = load_flags(sb, mcfg)
    except Exception as e:
        st.error(f"Run supabase_flags.sql once in Supabase (creates the flags table). ({e})"); return
    memo = _prepared(st, sb, base_df, mcfg, cfg)
    sd, t, u = memo["sd"], memo["t"], memo["u"]

    # ── Step 1: sectors ──
    st.markdown("#### 1 · Pick your focus sectors")
    if t.empty:
        st.info("No breakouts recorded yet — sectors appear after a few evening scans."); return
    healthy = t[(t["Trend"] != "↓") & (pd.to_numeric(t["Holding %"], errors="coerce").fillna(0) >= 60)]
    default = list(healthy["Sector"].head(3))
    st.caption("Leading = most breakouts recently, trend ↑ or →, Holding % ≥ 60 (breakouts still above their breakout day). "
               "Last 5/10/20 count breakouts, Stocks counts different names.")
    c1, c2 = st.columns([3, 2])
    with c1:
        st.dataframe(gp.style_leadership(t.head(12)), hide_index=True, use_container_width=True,
                     height=min(36 * min(len(t), 12) + 40, 470))
    with c2:
        key = f"sf_focus_{mkt}"
        if key not in st.session_state:
            st.session_state[key] = default
        focus = st.multiselect("Focus sectors (2–3)", list(t["Sector"]), key=key)
        if len(focus) > 3:
            st.warning("More than 3 sectors dilutes the focus.")
        st.caption("Suggested: " + ", ".join(default) if default else "No sector is clearly leading — be very selective.")

    if u.empty:
        st.info("No stocks to show."); return
    fk = tuple(sorted(focus))
    if fk not in memo["scored"]:                    # scoring + commentary once per focus-sector set
        memo["scored"][fk] = commentary(score(u, cfg, focus, memo["bos"], memo["tiers"]), cfg, mkt)
    v = memo["scored"][fk].copy()
    v["Flag"] = v["Symbol"].isin(flags)
    # Only today's scan is tradeable. Anything that dropped out of the scan keeps NO numbers (stale numbers such as an
    # old EP day's +8.7% are dangerous): it only shows up in Flagged, marked "Dropped out of the scan".
    stale = v["Data"] != "today"
    if stale.any():
        keep = {"Symbol", "Sector", "Industry", "Flag", "Data"} | {r for r, _ in RULES}
        blank = [c for c in v.columns if c not in keep]
        v[blank] = v[blank].astype(object)
        v.loc[stale, blank] = np.nan
        for c in NUMERIC + ["Pivot", "To pivot %", "Stop guide", "Last", "off_high_21"]:
            if c in v.columns:
                v[c] = pd.to_numeric(v[c], errors="coerce")
        v.loc[stale, [r for r, _ in RULES]] = "·"
        v.loc[stale, "Setup"] = "Dropped out of the scan"
        v.loc[stale, "Entry"] = "No longer passes the screens — check the chart; unflag unless you hold it"
        v.loc[stale, "Flags"] = "Not in today's scan"
        v.loc[stale, "Data"] = "dropped out"
    # Alert Management uses these for its priorities (Shortlister is retired)
    picked = [s_ for s_, r_ in flags.items() if r_.get("build_range")]
    st.session_state[f"sl_lists_{mkt}"] = {"FOCUS": picked, "FLAGGED": [s_ for s_ in flags if s_ not in picked]}

    # ── Step 2: one sector at a time ──
    st.markdown("#### 2 · Go through each sector and flag the best few")
    live = v[~stale]
    c1, c2 = st.columns([1, 3])
    min_score = c1.slider("Min score", 0.0, 8.0, 4.0, 0.5, key=f"sf_min_{mkt}",
                          help="Hide stocks passing fewer rules. Flagged stocks always show.")
    c2.caption("Rules: " + " · ".join(f"**{r}** {n}" for r, n in RULES) +
               ". ✓ pass · ½ half · ✗ fail · · no data. Hover a rule header for its threshold. "
               "Volume (R2) and sector (R3) first — tightness (R6) is only the entry.")
    if not focus:
        st.info("Pick at least one focus sector above.")
    else:
        counts = {x: int((live["Sector"] == x).sum()) for x in focus}
        flagged_in = {x: int(((v["Sector"] == x) & v["Flag"]).sum()) for x in focus}
        g = st.radio("Sector", focus, horizontal=True, key=f"sf_sector_{mkt}")      # static labels keep the selection stable
        st.caption(" · ".join(f"**{x}** {counts.get(x, 0)} stocks, ⚑{flagged_in.get(x, 0)}" for x in focus))
        part = live[(live["Sector"] == g) & ((live["Score"] >= min_score) | live["Flag"])]
        gone = v[stale & (v["Sector"] == g)]
        if not gone.empty:
            st.caption(f"Not shown — dropped out of today's scan (recent breakouts / watchlist names that no longer pass the "
                       f"screens): {', '.join(gone['Symbol'])}" + (" · ⚑ flagged ones stay in Flagged, marked dropped out"
                                                                   if gone["Flag"].any() else ""))
        if part.empty:
            st.info("Nothing in this sector at that score.")
        else:
            _flag_grid(st, sb, mcfg, cfg, part, key=f"sf_grid_{mkt}_{g}_{sd}", submit=f"Save flags for {g}",
                       okey=f"sf_order_{mkt}_{g}", copy_label=g, copy_key=f"sf_copy_sec_{mkt}_{g}", sector=g,
                       note=f"{len(part)} of {counts.get(g, 0)} stocks in {g} shown (score ≥ {min_score:g}).")

    # ── Step 3: last stop — the best setups in every other sector ──
    st.markdown("---")
    st.markdown("#### 3 · Last stop — best setups outside your focus sectors")
    st.caption("Only when the focus sectors give you nothing. Every other sector in today's scan, best first. "
               "R3 (sector) fails here by definition, so 7 is the top score. Be pickier than in your focus sectors.")
    c1, c2, c3 = st.columns([1, 1, 2])
    o_min = c1.slider("Min score", 0.0, 8.0, 6.0, 0.5, key=f"sf_omin_{mkt}")
    o_top = c2.number_input("Show top", 5, 100, 25, 5, key=f"sf_otop_{mkt}")
    o_setups = c3.multiselect("Setups", SETUP_ORDER, default=SETUP_ORDER[:4], key=f"sf_osetup_{mkt}",
                              help="Leave the ready-to-trade setups ticked; add others if you want a wider look")
    others = live[~live["Sector"].isin(focus)]
    op = others[((others["Score"] >= o_min) & (others["Setup"].isin(o_setups) if o_setups else True)) | others["Flag"]]
    op = op.assign(_so=op["Setup"].map({x: i for i, x in enumerate(SETUP_ORDER)}).fillna(99)) \
           .sort_values(["Flag", "Score", "_so", "Rel tight"], ascending=[False, False, True, True], na_position="last") \
           .drop(columns="_so").head(int(o_top) + int(op["Flag"].sum()))
    if op.empty:
        st.info("Nothing outside your focus sectors at that score / setup.")
    else:
        by = op.groupby("Sector").size().sort_values(ascending=False)
        st.caption("By sector: " + " · ".join(f"{k} {n}" for k, n in by.items()))
        _flag_grid(st, sb, mcfg, cfg, op, key=f"sf_ogrid_{mkt}_{sd}", submit="Save flags (other sectors)",
                   okey=f"sf_oorder_{mkt}", copy_label="Other sectors", copy_key=f"sf_copy_other_{mkt}", sector=None,
                   note=f"{len(op)} stocks from {len(by)} other sectors (score ≥ {o_min:g}).", show_sector=True)

    # ── Step 4: flagged ──
    st.markdown("---")
    render_flagged(st, sb, mcfg, v, flags, cfg)


SETUP_ORDER = ["1-min ORH ready", "Leader flag on 10MA", "Broke out today", "Tight at pivot, no volume yet",
               "Volume came, not tight yet", "Near the pivot", "Not ready"]


def _flag_grid(st, sb, mcfg, cfg, part, key, submit, okey, copy_label, copy_key, sector=None, note="", show_sector=False):
    """One flaggable grid (⚑ on the row, saved with the form button) + a copy box in the table's sort order."""
    mkt = mcfg.get("market")
    cols = [c for c in SHOW if c in part.columns and (c != "Circuit %" or mkt == "NSE")]
    if show_sector and "Sector" in part.columns:
        cols.insert(cols.index("Setup") + 1, "Sector")
    pv = part.reset_index(drop=True)
    gv = pv[cols].copy()
    gv["Flag"] = gv["Flag"].astype(bool)
    was = set(part.loc[part["Flag"], "Symbol"])
    ed, save = gp.card_grid(st, gv, key=key, sty=style(gv, cfg["min_liq"]), extra=_card_extra(pv, cols), labels=LABELS,
                            tips=_tips(cfg), card_first=CARD_FIRST, edit={"Flag": "bool"}, submit=submit,
                            height=min(32 * len(pv) + 70, 560),
                            widths={"Symbol": 110, "Score": 58, "Setup": 170, "Sector": 150, "Flags": 200, "Source": 110})
    st.caption(f"{note} **Click a ticker** for all its data · tick ⚑ on the rows you like, then **{submit}** "
               "(nothing reloads while you tick or sort).")
    if save and ed is not None and "Symbol" in ed.columns:
        st.session_state[okey] = list(ed["Symbol"])                  # the grid's sort order at Save
        now = set(ed.loc[ed["Flag"].fillna(False).astype(bool), "Symbol"])
        if now != was:
            secmap = dict(zip(pv["Symbol"], pv["Sector"])) if "Sector" in pv.columns else {}
            add_flags(sb, mcfg, sorted(now - was), sector=sector, source=sector or "other sectors", sectors=None if sector else secmap)
            remove_flags(sb, mcfg, was - now)
            st.toast(f"Flagged {len(now - was)}, unflagged {len(was - now)}.")
            _rerun(st)
    order = [x for x in st.session_state.get(okey, []) if x in set(pv["Symbol"])] or list(pv["Symbol"])
    gp.copy_area(st, f"{copy_label}: {len(order)} tickers in the table's order (sort the table, press Save to refresh) — for TradingView",
                 ",".join(gp.tv_symbol(x, mcfg) for x in order), key=copy_key)


def render_flagged(st, sb, mcfg, v, flags, cfg):
    mkt = mcfg.get("market")
    st.markdown("#### 4 · Flagged — pick your 5–6")
    with st.expander("⚑ Flag tickers by hand (from any screen or copy box)"):
        txt = st.text_input("Symbols, comma separated", key=f"sf_manual_{mkt}", placeholder="e.g. SUNTV, NSE:TIPSMUSIC")
        if st.button("Flag them", key=f"sf_manual_btn_{mkt}") and txt.strip():
            syms = [s.strip().upper().split(":")[-1] for s in txt.replace("\n", ",").split(",") if s.strip()]
            secmap = dict(zip(v["Symbol"], v["Sector"]))
            add_flags(sb, mcfg, syms, source="manual", sectors=secmap)
            st.toast(f"Flagged {len(syms)}."); _rerun(st)
    with st.expander("🔍 Check any ticker — its 8 rules, setup and full card"):
        q = st.text_input("Symbol", key=f"sf_check_{mkt}", placeholder="e.g. ILMN").strip().upper().split(":")[-1]
        if q:
            row = v[v["Symbol"] == q]
            if row.empty:
                st.caption(f"{q} isn't in today's scan, recent breakouts or your watchlist.")
            else:
                cols = [c for c in ["Symbol", "Sector", "Score", "Setup"] + HIGHLIGHT + [r for r, _ in RULES] +
                        ["Flags", "Entry"] if c in row.columns]
                st.dataframe(style(row[cols].reset_index(drop=True), cfg["min_liq"]), hide_index=True,
                             use_container_width=True, column_config=_colcfg(st, cfg))
            gp.show_stock_card(st, sb, mcfg, st.session_state.get("gtt_base_df"), q)
    if not flags:
        st.caption("Nothing flagged yet."); return
    cutoff = date.today() - timedelta(days=cfg["flag_days"])
    fl = pd.DataFrame(flags.values())
    fl["flagged_at"] = pd.to_datetime(fl["flagged_at"]).dt.date
    old = fl[(fl["flagged_at"] < cutoff) & (~fl["build_range"].fillna(False))]
    if not old.empty:
        remove_flags(sb, mcfg, old["symbol"])            # flags fade after a week unless picked
        fl = fl[~fl["symbol"].isin(old["symbol"])]
        st.caption(f"Unflagged {len(old)} older than {cfg['flag_days']} days that weren't picked: {', '.join(old['symbol'])}.")
    d = fl.merge(v.drop(columns=["Flag"]), left_on="symbol", right_on="Symbol", how="left")
    d["Symbol"] = d["symbol"]
    d["Sector"] = d["Sector"].fillna(d["sector"])
    d["Flagged"] = pd.to_datetime(d["flagged_at"]).dt.strftime("%d-%b")
    d["Pick"] = d["build_range"].fillna(False).astype(bool)
    d["Rank"] = pd.to_numeric(d["rank"], errors="coerce")
    d["Note"] = d["note"].fillna("")
    d["Unflag"] = False
    d = d.sort_values(["Pick", "Rank", "Score"], ascending=[False, True, False], na_position="last")
    show = ["Pick", "Rank", "Symbol", "Setup", "Entry", "Pivot", "To pivot %", "Stop guide", "Flags", "Sector", "Score"] + \
           HIGHLIGHT + [r for r, _ in RULES] + ["BO", "BO vol x", "Off 1M (ADR)", "Off 52W %", "10MA (ADR)", "Avg vol", "Day vol", "Chg %", "Vol x"] + (["Circuit %"] if mkt == "NSE" else []) + ["Data", "Flagged", "Note", "Unflag"]
    show = [c for c in show if c in d.columns]
    n_pick = int(d["Pick"].sum())
    st.caption(f"**{len(d)} flagged · {n_pick} picked** (aim for {cfg['target']}). Tick **Pick** = draw the range and set "
               f"the alert. Rank 1 = best. Unflagged automatically after {cfg['flag_days']} days unless picked.")
    if n_pick > cfg["target"]:
        st.warning(f"{n_pick} picked — cut to {cfg['target']} or fewer. Be very selective.")
    cc = _colcfg(st, cfg)
    cc.update({"Pick": st.column_config.CheckboxColumn("Pick", help="Build the range + alert", width="small"),
               "Rank": st.column_config.NumberColumn("Rank", min_value=1, max_value=99, step=1, width="small"),
               "Note": st.column_config.TextColumn("Note", width="medium"),
               "Unflag": st.column_config.CheckboxColumn("Unflag", width="small"),
               "Flagged": st.column_config.TextColumn("Flagged", width="small")})
    dv = d.reset_index(drop=True)
    gv = dv[show].copy()
    for c in ("Pick", "Unflag"):
        gv[c] = gv[c].fillna(False).astype(bool)
    ed, saved = gp.card_grid(st, gv, key=f"sf_fgrid_{mkt}", sty=style(gv, cfg["min_liq"]),
                             extra=_card_extra(dv, show), labels={**LABELS, "Entry": "Entry plan"}, tips=_tips(cfg),
                             card_first=CARD_FIRST, edit={"Pick": "bool", "Rank": "number", "Note": "text", "Unflag": "bool"},
                             submit="Save shortlist", height=min(32 * len(dv) + 70, 520),
                             widths={"Symbol": 110, "Score": 58, "Setup": 170, "Entry": 300, "Flags": 200})
    st.caption("**Click a ticker** for all its data · tick Pick / Unflag, type a Rank or Note on the row, then **Save shortlist**.")
    changed = False
    if saved and ed is not None and "Symbol" in ed.columns:
        st.session_state[f"sf_forder_{mkt}"] = list(ed["Symbol"])     # the grid's sort order at Save
        orig = d.set_index("Symbol")
        for r in ed.to_dict("records"):
            s = r["Symbol"]
            if s not in orig.index:
                continue
            if r.get("Unflag"):
                remove_flags(sb, mcfg, [s]); changed = True; continue
            o = orig.loc[s]
            rk = _num(r.get("Rank"))
            ch = {}
            if bool(r.get("Pick")) != bool(o["Pick"]):
                ch["build_range"] = bool(r.get("Pick"))
            if (rk if np.isfinite(rk) else None) != (o["Rank"] if np.isfinite(_num(o["Rank"])) else None):
                ch["rank"] = int(rk) if np.isfinite(rk) else None
            if str(r.get("Note") or "") != str(o["Note"] or ""):
                ch["note"] = str(r.get("Note") or "")
            if ch:
                update_flag(sb, mcfg, s, **ch); changed = True
        if changed:
            st.toast("Saved."); _rerun(st)
    picks = d[d["Pick"]].sort_values("Rank", na_position="last")["Symbol"].tolist()
    if picks:
        gp.copy_area(st, f"Picked {len(picks)} — draw ranges and set alerts (TradingView)",
                     ",".join(gp.tv_symbol(s, mcfg) for s in picks), key=f"sf_copy_pick_{mkt}")
    order = [x for x in st.session_state.get(f"sf_forder_{mkt}", []) if x in set(d["Symbol"])]
    order += [x for x in d["Symbol"] if x not in order]
    gp.copy_area(st, f"All {len(order)} flagged, in the table's order (sort the table, press Save to refresh) — for TradingView",
                 ",".join(gp.tv_symbol(x, mcfg) for x in order), key=f"sf_copy_all_{mkt}")
