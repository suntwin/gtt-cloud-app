"""Shortlister tab: two rule-based setup lists you rank by eye.

1. 3-Week Tight Closing (3WTC): weekly closes bunched together near the range high, ready to break out.
2. High Tight Flag (HTF): high-momentum names (Ti65, high ADR) in a super-tight flag, 1-3 ADR from the 10-week
   line - or a leader's flag sitting on its 10MA (post-EP / continuation step), where the 10-week distance is
   allowed to be larger.

Rows are pre-filled from today's scan; you untick (remove), rank and comment. Saved per session in
daily_snapshots with scanner = 'SHORTLIST' (run supabase_shortlist.sql once).
"""
import numpy as np
import pandas as pd

import gtt_process as gp

SETUPS = {"ACTION": "Action: big-volume breakouts", "RECENT": "Breakout → tight at pivot", "3WTC": "3-Week Tight Closing", "HTF": "High Tight Flag"}

SHORTLIST_DEFAULTS = {
    # Action: big-volume (imbalance) breakouts, today and the last few sessions
    "act_min_vol": 2.0,           # breakout-day volume ÷ average, at least
    "act_min_value": 100.0,       # breakout-day traded value (₹ cr / $M), at least
    "act_min_chg": 3.0,           # breakout-day change %, at least
    "act_days": 3,                # also show breakouts from this many recent sessions, still holding
    # Breakout -> tight at pivot: a volume breakout 1-20 sessions ago, now tight just under the high
    "rec_max_age": 20,            # sessions since the breakout, at most
    "rec_max_reltight": 0.8,      # rel tightness today, at most
    "rec_max_off1m_adr": 1.0,     # below the 1-month high (the pivot), in ADRs, at most
    # shared
    "min_liq": 50.0,              # avg daily traded value floor (NSE ₹ cr / USA $M)
    "ma_min_adr": -0.3,           # 10MA and 20MA distance floor, in ADRs (small dips allowed)
    "late_rwd": 4.0,              # above this many ADRs from the 10w line: flag 'check step no. / climax high'
    # 3-Week Tight Closing
    "wtc_min_tight": 2,           # weekly tight closes (MIO weekly screen) at least
    "wtc_max_wkchg_adr": 0.5,     # this week's close change, in ADRs, at most
    "wtc_max_off1m_adr": 1.5,     # distance below the 1-month high (the pivot), in ADRs
    "wtc_min_rwd": 0.5, "wtc_max_rwd": 2.0,   # ADRs from the 10w line
    "wtc_min_adr": 3.0,
    # High Tight Flag
    "htf_min_ti65": 1.1,
    "htf_min_adr": 5.0,
    "htf_max_reltight": 0.8,
    "htf_min_rwd": 1.0, "htf_max_rwd": 3.0,
    "htf_flag_max_10ma_adr": 1.0, # above max_rwd: a leader flag counts if within this many ADRs above the 10MA
    "htf_max_off1m_adr": 2.0,
}
MARKET_OVERRIDES = {"NSE": {"min_liq": 50.0, "htf_min_adr": 5.0, "act_min_value": 100.0},
                    "USA": {"min_liq": 30.0, "htf_min_adr": 6.0, "act_min_value": 50.0}}
RULE_LABELS = {
    "act_min_vol": "Action: min breakout volume (x avg)", "act_min_value": "Action: min breakout-day value (₹ cr / $M)",
    "act_min_chg": "Action: min breakout-day change %", "act_days": "Action: recent sessions to include",
    "rec_max_age": "BO→tight: max sessions since breakout", "rec_max_reltight": "BO→tight: max rel tightness",
    "rec_max_off1m_adr": "BO→tight: max below 1M high (ADRs)",
    "min_liq": "Min avg traded value (₹ cr / $M)", "ma_min_adr": "10/20MA floor (ADRs)", "late_rwd": "Flag 'check steps' above (ADRs from 10w)",
    "wtc_min_tight": "3WTC: min weekly tight closes", "wtc_max_wkchg_adr": "3WTC: max week's close change (ADRs)",
    "wtc_max_off1m_adr": "3WTC: max below 1M high (ADRs)", "wtc_min_rwd": "3WTC: min ADRs from 10w",
    "wtc_max_rwd": "3WTC: max ADRs from 10w", "wtc_min_adr": "3WTC: min ADR",
    "htf_min_ti65": "HTF: min Ti65", "htf_min_adr": "HTF: min ADR", "htf_max_reltight": "HTF: max rel tightness",
    "htf_min_rwd": "HTF: min ADRs from 10w", "htf_max_rwd": "HTF: max ADRs from 10w",
    "htf_flag_max_10ma_adr": "HTF leader flag: max ADRs above 10MA", "htf_max_off1m_adr": "HTF: max below 1M high (ADRs)",
}
gp.RULE_LABELS.update(RULE_LABELS)

EDIT_COLS = ["Keep", "Rank", "Alert set", "Comment", "Weekly comment"]


def _n(d, c):
    return pd.to_numeric(d[c], errors="coerce") if c in d.columns else pd.Series(np.nan, index=d.index)


def _pullback(m10):
    if not np.isfinite(m10):
        return "—"
    if m10 < 0:
        return "Below 10MA"
    if m10 <= 1:
        return "Sideways on 10MA"
    if m10 <= 2:
        return "Above 10MA"
    return "Extended"


def shortlist_masks(d, cfg):
    """Boolean masks (3WTC, HTF, leader-flag) on a frame that already went through add_derived + _prep."""
    rwd, off, rt = d["_rel_wk_dist"], d["_off21_adr"], d["_rel_tightness_today"]
    adr, liq, ti, wt = _n(d, "Adr"), _n(d, "_avgvol_mln"), _n(d, "Ti65"), _n(d, "W_TightCloses_10w")
    above10w = _n(d, "W_Dist10wMA") > 0
    ma_ok = (d["_ma10_adr"] >= cfg["ma_min_adr"]) & (d["_ma20_adr"] >= cfg["ma_min_adr"])
    common = (liq >= cfg["min_liq"]) & above10w & ma_ok
    wtc = (common & (wt >= cfg["wtc_min_tight"]) & (d["_wkchg_adr"] <= cfg["wtc_max_wkchg_adr"])
           & (off <= cfg["wtc_max_off1m_adr"]) & rwd.between(cfg["wtc_min_rwd"], cfg["wtc_max_rwd"])
           & (adr >= cfg["wtc_min_adr"]))
    htf_base = (common & (ti >= cfg["htf_min_ti65"]) & (adr >= cfg["htf_min_adr"])
                & (rt <= cfg["htf_max_reltight"]) & (off <= cfg["htf_max_off1m_adr"]))
    leader = (rwd > cfg["htf_max_rwd"]) & (d["_ma10_adr"] <= cfg["htf_flag_max_10ma_adr"])
    htf = htf_base & (rwd.between(cfg["htf_min_rwd"], cfg["htf_max_rwd"]) | leader)
    return wtc.fillna(False), htf.fillna(False), (htf_base & leader).fillna(False)


def _prep(base_df):
    d = gp.add_derived(base_df).drop_duplicates("Symbol").reset_index(drop=True)
    adr = _n(d, "Adr").replace(0, np.nan)
    d["_ma10_adr"] = (_n(d, "_10madist") / adr).round(2)
    d["_ma20_adr"] = (_n(d, "_20madist") / adr).round(2)
    d["_wkchg_adr"] = (_n(d, "W_CloseChg_Pct").abs() / adr).round(2)
    if "_off21_adr" not in d.columns:
        d["_off21_adr"] = np.nan
    return d


def build_shortlists(base_df, cfg, holding=None, tags=None, saved=None):
    """{setup: DataFrame} ready for the editor. saved = {symbol: {setup: {rank, comment, wk, keep}}}."""
    holding, tags, saved = holding or {}, tags or {}, saved or {}
    d = _prep(base_df)
    wtc, htf, leader = shortlist_masks(d, cfg)
    out = {}
    for key, mask in (("3WTC", wtc), ("HTF", htf)):
        v = d[mask].copy()
        flags = []
        for i, r in v.iterrows():
            f = []
            if key == "HTF" and bool(leader.get(i, False)):
                f.append("Leader flag (on 10MA)")
            if np.isfinite(r["_rel_wk_dist"]) and r["_rel_wk_dist"] > cfg["late_rwd"]:
                f.append(f"{r['_rel_wk_dist']:.1f} ADR from 10w: check step no. / climax high")
            chg, vr, a = _num(r.get("_chg_percentclose")), _num(r.get("_vol_ratio")), _num(r.get("Adr"))
            if np.isfinite(chg) and np.isfinite(a) and a > 0 and chg >= a and np.isfinite(vr) and vr >= 1.5:
                f.append("Broke out today")
            flags.append(" · ".join(f))
        v["Flags"] = flags
        v["Pullback"] = v["_ma10_adr"].apply(lambda x: _pullback(float(x)) if pd.notna(x) else "—")
        v["Grp hold %"] = v["Sector"].map(holding) if "Sector" in v.columns else np.nan
        v["Saved"] = v["Symbol"].map(tags).fillna("")
        if "Sector" not in v.columns:
            v["Sector"] = ""
        mine = [saved.get(s, {}).get(key, {}) for s in v["Symbol"]]
        v["Keep"] = [bool(m.get("keep", True)) for m in mine]
        v["Rank"] = pd.to_numeric(pd.Series([m.get("rank") for m in mine], index=v.index), errors="coerce")
        v["Comment"] = [m.get("comment", "") for m in mine]
        v["Weekly comment"] = [m.get("wk", "") for m in mine]
        v["_r"] = pd.to_numeric(v["Rank"], errors="coerce")
        v = v.sort_values(["Keep", "_r", "_rel_tightness_today"], ascending=[False, True, True],
                          na_position="last").drop(columns="_r")
        out[key] = _add_plan(v.reset_index(drop=True), key, None, saved)
    return out


def _num(x):
    try:
        x = float(x)
        return x if np.isfinite(x) else np.nan
    except (TypeError, ValueError):
        return np.nan


def _add_plan(v, setup, sd, saved):
    """Pivot (1-month high as a starting line), distance, the widest allowed stop (pivot − 1 ADR), entry style."""
    if v.empty:
        v["Pivot"] = v["To pivot %"] = v["Stop guide"] = v["Entry"] = []
        v["Alert set"] = []
        return v
    last, off, adr = _n(v, "Last"), _n(v, "off_high_21").abs(), _n(v, "Adr")
    piv = (last / (1 - off / 100)).where(off < 100)
    v["Pivot"] = piv.round(2)
    v["To pivot %"] = ((piv / last - 1) * 100).round(1)
    v["Stop guide"] = (piv * (1 - adr / 100)).round(2)
    ent = []
    for _, r in v.iterrows():
        if setup == "RECENT":
            ent.append("Volume came, now tight under the high → alert at pivot, 1-min ORH on the break, Rel vol ≥ 2")
        elif setup != "ACTION":
            ent.append("Alert at pivot → 1-min ORH on the break day, Rel vol ≥ 2")
        elif r.get("BO date") == str(sd):
            ent.append("Day 2+: wait for a 15-min / 1-hour flag, buy its break (or 5-min ORH); no chasing the wick")
        elif "Tight now" in str(r.get("Flags", "")):
            ent.append("Tight day after the breakout: ORH above its high")
        else:
            ent.append("Wait for a tight day / hourly flag")
    v["Entry"] = ent
    v["Alert set"] = [bool(saved.get(sym, {}).get(setup, {}).get("alert", False)) for sym in v["Symbol"]]
    return v


def _base_label(rp):
    rp = _num(rp)
    if not np.isfinite(rp):
        return "—"
    return "Tight base" if rp <= 0.8 else ("Base" if rp <= 1.2 else "Loose")


def build_action(base_df, cfg, sd, hist=None, holding=None, tags=None, saved=None):
    """Big-volume breakouts: today's from the scan, plus recent ones (recorded breakouts) still holding."""
    holding, tags, saved = holding or {}, tags or {}, saved or {}
    d = _prep(base_df)
    ep_chg, ep_vol = gp.BREAKOUT_DEFAULTS["ep_min_chg"], gp.BREAKOUT_DEFAULTS["ep_min_vol"]
    chg, vol, dv = _n(d, "_chg_percentclose"), _n(d, "_vol_ratio"), _n(d, "dvol")
    today = d[(chg >= cfg["act_min_chg"]) & (vol >= cfg["act_min_vol"]) & (dv >= cfg["act_min_value"])].copy()
    today["BO date"] = str(sd)
    today["BO chg %"], today["BO vol x"], today["BO value"] = chg[today.index], vol[today.index], dv[today.index]
    today["Since BO %"] = 0.0
    today["Base before"] = today["_rel_tightness_prev"].map(_base_label)
    rows = [today]
    if hist is not None and not hist.empty:
        h = hist[hist["date"] < str(sd)].copy()
        sessions = sorted(h["date"].unique(), reverse=True)[: int(cfg["act_days"])]
        h = h[h["date"].isin(sessions)]
        h["value"] = h["vol"] * h["avgvol"]
        h = h[(h["vol"] >= cfg["act_min_vol"]) & (h["chg"] >= cfg["act_min_chg"])
              & (h["value"].isna() | (h["value"] >= cfg["act_min_value"]))]
        h = h.sort_values("date", ascending=False).drop_duplicates("Symbol")
        h = h[~h["Symbol"].isin(today["Symbol"])]
        live = d.set_index("Symbol")
        h = h[h["Symbol"].isin(live.index)]
        if not h.empty:
            past = live.loc[h["Symbol"]].reset_index()
            past["BO date"] = h["date"].values
            past["BO chg %"], past["BO vol x"], past["BO value"] = h["chg"].values, h["vol"].values, h["value"].values
            now = pd.to_numeric(past["Last"], errors="coerce").values
            past["Since BO %"] = np.round((now / h["bo_close"].values - 1) * 100, 1)
            past = past[now >= h["prev_close"].values]          # still holding above the pre-breakout close
            past["Base before"] = [_base_label(x) for x in h.loc[h["Symbol"].isin(past["Symbol"]), "relprev"]] \
                if len(past) else []
            rows.append(past)
    v = pd.concat(rows, ignore_index=True) if len(rows) > 1 else today.reset_index(drop=True)
    if v.empty:
        return v
    v["EP"] = np.where((v["BO chg %"] >= ep_chg) & (v["BO vol x"] >= ep_vol), "EP", "")
    flags = []
    for _, r in v.iterrows():
        f = []
        if r["BO date"] == str(sd):
            f.append("Today")
        if np.isfinite(_num(r.get("_avgvol_mln"))) and _num(r.get("_avgvol_mln")) < cfg["min_liq"]:
            f.append("Thin stock: needs big breakout days")
        if np.isfinite(_num(r.get("_rel_tightness_today"))) and _num(r.get("_rel_tightness_today")) <= cfg["htf_max_reltight"] \
                and r["BO date"] != str(sd):
            f.append("Tight now: entry spot")
        flags.append(" · ".join(f))
    v["Flags"] = flags
    v["Pullback"] = v["_ma10_adr"].apply(lambda x: _pullback(float(x)) if pd.notna(x) else "—")
    v["Grp hold %"] = v["Sector"].map(holding) if "Sector" in v.columns else np.nan
    if "Sector" not in v.columns:
        v["Sector"] = ""
    v["Saved"] = v["Symbol"].map(tags).fillna("")
    mine = [saved.get(s, {}).get("ACTION", {}) for s in v["Symbol"]]
    v["Keep"] = [bool(m.get("keep", True)) for m in mine]
    v["Rank"] = pd.to_numeric(pd.Series([m.get("rank") for m in mine], index=v.index), errors="coerce")
    v["Comment"] = [m.get("comment", "") for m in mine]
    v["Weekly comment"] = [m.get("wk", "") for m in mine]
    v["_r"] = v["Rank"]
    v["_ep"] = v["EP"] == "EP"
    v = v.sort_values(["Keep", "_r", "BO date", "_ep", "BO vol x"], ascending=[False, True, False, False, False],
                      na_position="last").drop(columns=["_r", "_ep"])
    return _add_plan(v.reset_index(drop=True), "ACTION", sd, saved)


AGE_BUCKETS = [(3, "≤ 3 days"), (5, "≤ 1 week"), (10, "≤ 2 weeks"), (20, "≤ 4 weeks"), (10 ** 6, "older")]


def _bucket(age):
    a = _num(age)
    if not np.isfinite(a):
        return "—", 9
    for i, (lim, name) in enumerate(AGE_BUCKETS):
        if a <= lim:
            return name, i
    return "older", len(AGE_BUCKETS)


def build_recent(base_df, cfg, sd, hist=None, holding=None, tags=None, watch_dates=None, saved=None):
    """A volume breakout 1..rec_max_age sessions ago (scan's days-since-BO with breakout value, a recorded
    breakout on volume, or one you tagged), now tight just under the 1-month high. Newest first."""
    holding, tags, watch_dates, saved = holding or {}, tags or {}, watch_dates or {}, saved or {}
    d = _prep(base_df)
    if d.empty:
        return d
    age = pd.Series(np.nan, index=d.index)
    src = pd.Series("", index=d.index, dtype=object)
    bo_val, bo_vol = _n(d, "_bo_dollar_vol_mln"), pd.Series(np.nan, index=d.index)
    dsb = _n(d, "_days_since_bo")
    m = dsb.between(1, cfg["rec_max_age"]) & (bo_val >= cfg["act_min_value"])
    age[m], src[m] = dsb[m], "scan"
    if hist is not None and not hist.empty:
        sessions = sorted(hist["date"].unique(), reverse=True)
        pos = {dt: i for i, dt in enumerate(sessions)}
        h = hist[(hist["date"] < str(sd)) & (hist["vol"] >= cfg["act_min_vol"])].sort_values("date", ascending=False) \
            .drop_duplicates("Symbol")
        off0 = 0 if sessions and sessions[0] == str(sd) else 1     # sessions between the breakout and tonight
        hmap = {r.Symbol: (pos[r.date] + off0, r.vol, r.vol * r.avgvol, r.prev_close) for r in h.itertuples()}
        for i, sym in d["Symbol"].items():
            if sym in hmap:
                a, v, val, _ = hmap[sym]
                if a <= cfg["rec_max_age"] and (not np.isfinite(age[i]) or a < age[i]):
                    age[i] = a
                bo_vol[i] = v
                if not np.isfinite(bo_val[i]) and np.isfinite(_num(val)):
                    bo_val[i] = val
                src[i] = (src[i] + " + recorded").strip(" +")
    for i, sym in d["Symbol"].items():
        td = watch_dates.get(sym)
        if td:
            try:
                a = int(np.busday_count(pd.Timestamp(td).date(), sd))
            except Exception:
                continue
            if 1 <= a <= cfg["rec_max_age"]:
                if not np.isfinite(age[i]):
                    age[i] = a
                src[i] = (src[i] + " + tagged").strip(" +")
    rt, off = d["_rel_tightness_today"], d["_off21_adr"]
    ma_ok = d["_ma10_adr"] >= cfg["ma_min_adr"]
    keep = age.notna() & (age >= 1) & (rt <= cfg["rec_max_reltight"]) & (off <= cfg["rec_max_off1m_adr"]) & ma_ok
    v = d[keep.fillna(False)].copy()
    if v.empty:
        return _add_plan(v, "RECENT", sd, saved)
    v["Age"] = age[v.index]
    v["Source"] = src[v.index]
    v["BO value"] = bo_val[v.index]
    v["BO vol x"] = bo_vol[v.index]
    bk = [_bucket(a) for a in v["Age"]]
    v["Age bucket"], v["_b"] = [b[0] for b in bk], [b[1] for b in bk]
    flags = []
    for _, r in v.iterrows():
        f = []
        if np.isfinite(_num(r.get("_avgvol_mln"))) and _num(r.get("_avgvol_mln")) < cfg["min_liq"]:
            f.append("Thin stock")
        if np.isfinite(_num(r.get("_rel_wk_dist"))) and _num(r.get("_rel_wk_dist")) > cfg["late_rwd"]:
            f.append(f"{_num(r.get('_rel_wk_dist')):.1f} ADR from 10w: check step no.")
        flags.append(" · ".join(f))
    v["Flags"] = flags
    v["Pullback"] = v["_ma10_adr"].apply(lambda x: _pullback(float(x)) if pd.notna(x) else "—")
    if "Sector" not in v.columns:
        v["Sector"] = ""
    v["Grp hold %"] = v["Sector"].map(holding)
    v["Saved"] = v["Symbol"].map(tags).fillna("")
    mine = [saved.get(s, {}).get("RECENT", {}) for s in v["Symbol"]]
    v["Keep"] = [bool(m.get("keep", True)) for m in mine]
    v["Rank"] = pd.to_numeric(pd.Series([m.get("rank") for m in mine], index=v.index), errors="coerce")
    v["Comment"] = [m.get("comment", "") for m in mine]
    v["Weekly comment"] = [m.get("wk", "") for m in mine]
    v = v.sort_values(["Keep", "Rank", "_b", "_rel_tightness_today", "Age"], ascending=[False, True, True, True, True],
                      na_position="last").drop(columns="_b")
    return _add_plan(v.reset_index(drop=True), "RECENT", sd, saved)


RECENT_SHOW = ["Keep", "Rank", "Alert set", "Symbol", "Pivot", "To pivot %", "Stop guide", "Entry", "Age bucket", "Age",
               "BO value", "BO vol x", "Source", "Saved", "Sector", "Grp hold %", "Adr", "Ti65", "_rel_tightness_today",
               "_ma10_adr", "Pullback", "_rel_wk_dist", "_avgvol_mln", "Flags", "Comment", "Weekly comment"]


ACTION_SHOW = ["Keep", "Rank", "Alert set", "Symbol", "Pivot", "To pivot %", "Stop guide", "Entry", "EP", "BO date", "BO chg %", "BO vol x", "BO value", "Base before",
               "Since BO %", "Sector", "Grp hold %", "Saved", "Adr", "Ti65", "_rel_wk_dist", "_ma10_adr", "Pullback",
               "_rel_tightness_today", "_avgvol_mln", "Flags", "Comment", "Weekly comment"]


SHOW = ["Keep", "Rank", "Alert set", "Symbol", "Pivot", "To pivot %", "Stop guide", "Entry", "Sector", "Grp hold %", "Saved", "_rel_wk_dist", "_ma10_adr", "Pullback",
        "Ti65", "Adr", "_rel_tightness_today", "off_high_21", "W_TightCloses_10w", "_avgvol_mln", "_vol_ratio",
        "_chg_percentclose", "Flags", "Comment", "Weekly comment"]


def _column_config(st, mcfg):
    cc, liq = st.column_config, ("₹ cr" if mcfg.get("market") == "NSE" else "$M")
    return {
        "Keep": cc.CheckboxColumn("Keep", help="Untick to remove it from tonight's shortlist", width="small"),
        "Rank": cc.NumberColumn("Rank", min_value=1, max_value=99, step=1, format="%d", width="small",
                                help="Your rank after looking at the chart (1 = best)"),
        "Symbol": cc.TextColumn("Symbol", width="small"),
        "Sector": cc.TextColumn("Sector", width="medium"),
        "Grp hold %": cc.NumberColumn("Grp hold %", format="%.0f", width="small",
                                      help="Group's breakouts still holding (Leadership). 60%+ = healthy"),
        "Saved": cc.TextColumn("Saved", width="small", help="Your watchlist tags"),
        "_rel_wk_dist": cc.NumberColumn("Weekly (ADR)", format="%.1f", width="small", help="ADRs from the 10-week line"),
        "_ma10_adr": cc.NumberColumn("10MA (ADR)", format="%.1f", width="small", help="ADRs above (+) / below (−) the 10-day MA"),
        "Pullback": cc.TextColumn("Pullback", width="small"),
        "Ti65": cc.NumberColumn("Ti65", format="%.2f", width="small"),
        "Adr": cc.NumberColumn("ADR %", format="%.1f", width="small"),
        "_rel_tightness_today": cc.NumberColumn("Rel tight", format="%.2f", width="small"),
        "off_high_21": cc.NumberColumn("% to pivot", format="%.1f", width="small", help="Below the 1-month high, in %"),
        "W_TightCloses_10w": cc.NumberColumn("Wk tight cl.", format="%d", width="small"),
        "_avgvol_mln": cc.NumberColumn(f"Avg vol ({liq})", format="%.0f", width="small"),
        "_vol_ratio": cc.NumberColumn("Vol x", format="%.1f", width="small"),
        "_chg_percentclose": cc.NumberColumn("Chg %", format="%.1f", width="small"),
        "Flags": cc.TextColumn("Flags", width="medium"),
        "Comment": cc.TextColumn("Comment", width="medium"),
        "Weekly comment": cc.TextColumn("Weekly comment", width="medium"),
        "Alert set": cc.CheckboxColumn("Alert set", width="small", help="Tick once the range line and alert are set in TradingView"),
        "Pivot": cc.NumberColumn("Pivot", format="%.2f", width="small",
                                 help="Starting line = the 1-month high. Adjust on the chart (cluster of highs, not a one-day spike)"),
        "To pivot %": cc.NumberColumn("To pivot %", format="%.1f", width="small"),
        "Stop guide": cc.NumberColumn("Stop guide", format="%.2f", width="small",
                                      help="Pivot − 1 ADR: the lowest your low-of-day stop may be for an entry at the pivot"),
        "Entry": cc.TextColumn("Entry", width="medium"),
        "Age bucket": cc.TextColumn("Since BO", width="small", help="Newest breakouts first"),
        "Age": cc.NumberColumn("Sessions", format="%d", width="small", help="Sessions since the breakout"),
        "Source": cc.TextColumn("Source", width="small", help="scan = MarketInOut days-since-BO; recorded = auto-saved breakout; tagged = your watchlist"),
        "EP": cc.TextColumn("EP", width="small", help=f"Breakout ≥ {gp.BREAKOUT_DEFAULTS['ep_min_chg']:g}% on ≥ {gp.BREAKOUT_DEFAULTS['ep_min_vol']:g}x volume — check the news"),
        "BO date": cc.TextColumn("BO date", width="small"),
        "BO chg %": cc.NumberColumn("BO chg %", format="%.1f", width="small"),
        "BO vol x": cc.NumberColumn("BO vol x", format="%.1f", width="small", help="Breakout-day volume ÷ average"),
        "BO value": cc.NumberColumn(f"BO value ({liq})", format="%.0f", width="small", help="Traded value on the breakout day"),
        "Base before": cc.TextColumn("Base before", width="small", help="Rel tightness the day before the breakout"),
        "Since BO %": cc.NumberColumn("Since BO %", format="%.1f", width="small"),
    }


def _holding_map(sb, mcfg, base_df):
    try:
        t, _ = gp.sector_leadership(gp.load_breakout_history(sb, mcfg), base_df)
        return dict(zip(t["Sector"], t["Holding %"])) if not t.empty else {}
    except Exception:
        return {}


def _tags_map(sb, mcfg):
    try:
        return {r["symbol"]: "✓ " + " + ".join(gp.tags_of(r)) if gp.tags_of(r) else "✓"
                for r in gp.load_active_watchlist(sb, mcfg)}
    except Exception:
        return {}


def load_saved(sb, mcfg, sd):
    rows = (sb.table("daily_snapshots").select("symbol,metrics").eq("user_id", mcfg["user_id"])
            .eq("market", mcfg["market"]).eq("scanner", "SHORTLIST").eq("snap_date", sd.isoformat())
            .execute().data)
    return {r["symbol"]: (r.get("metrics") or {}).get("setups", {}) for r in rows}


def save_shortlist(sb, mcfg, sd, edited, base):
    """edited = {setup: edited editor frame}; one row per symbol, its setups merged in metrics['setups']."""
    per = {}
    for key, v in edited.items():
        for _, r in v.iterrows():
            rk = _num(r.get("Rank"))
            per.setdefault(r["Symbol"], {})[key] = {
                "keep": bool(r.get("Keep")), "rank": int(rk) if np.isfinite(rk) else None,
                "alert": bool(r.get("Alert set")) if pd.notna(r.get("Alert set")) else False,
                "comment": str(r.get("Comment") or ""), "wk": str(r.get("Weekly comment") or "")}
    b = base.set_index("Symbol") if base is not None and not base.empty else None
    rows = []
    for sym, setups in per.items():
        m = gp._clean_metrics(b.loc[sym], gp.SNAPSHOT_METRICS) if b is not None and sym in b.index else {}
        m["setups"] = setups
        best = min([s["rank"] for s in setups.values() if s["rank"]] or [None], key=lambda x: (x is None, x))
        if best:
            m["rank"] = best
        rows.append({"user_id": mcfg["user_id"], "market": mcfg["market"], "exchange": mcfg["exchange"],
                     "scanner": "SHORTLIST", "snap_date": sd.isoformat(), "symbol": sym,
                     "list": "+".join(sorted(setups)), "selected": any(s["keep"] for s in setups.values()),
                     "scan_count": 0, "metrics": m})
    gp.upsert_snapshots(sb, rows)
    return len(rows)


def render_shortlister_tab(st, sb, base_df, saved_prefs, mcfg):
    st.subheader("Shortlister  ·  volume first, then the tight setups")
    st.caption("Pre-filled from today's scan. Flip each chart, untick what you'd skip, rank the rest (1 = best) "
               "and add a comment. Your top 5–7 get range lines and alerts.")
    if base_df is None or getattr(base_df, "empty", True):
        st.info("Generate data first (GTT Scanner tab)."); return
    own = {**SHORTLIST_DEFAULTS, **MARKET_OVERRIDES.get(mcfg.get("market"), {})}
    cfg = gp._rules_editor(st, "Shortlist rules", own, (saved_prefs or {}).get("shortlist"), "sl_cfg")
    sd, _, _ = gp.effective_session(base_df, mcfg, sb)
    saved, can_save = {}, sb is not None
    if sb is not None:
        try:
            saved = load_saved(sb, mcfg, sd)
        except Exception:
            saved = {}
    hist = None
    if sb is not None:
        try:
            hist = gp.load_breakout_history(sb, mcfg)
        except Exception:
            hist = None
    holding = {}
    if hist is not None and not hist.empty:
        try:
            t, _ = gp.sector_leadership(hist, base_df)
            holding = dict(zip(t["Sector"], t["Holding %"])) if not t.empty else {}
        except Exception:
            holding = {}
    watch = []
    if sb is not None:
        try:
            watch = gp.load_active_watchlist(sb, mcfg)
        except Exception:
            watch = []
    tags = {r["symbol"]: ("✓ " + " + ".join(gp.tags_of(r))) if gp.tags_of(r) else "✓" for r in watch}
    watch_dates = {r["symbol"]: str(r.get("last_trigger_date") or r.get("trigger_date") or r.get("added_date") or "")[:10]
                   for r in watch}
    lists = build_shortlists(base_df, cfg, holding, tags, saved)
    lists = {"ACTION": build_action(base_df, cfg, sd, hist, holding, tags, saved),
             "RECENT": build_recent(base_df, cfg, sd, hist, holding, tags, watch_dates, saved), **lists}
    if "W_TightCloses_10w" not in base_df.columns or base_df["W_TightCloses_10w"].isna().all():
        st.warning("Weekly scan data is missing, so the 3-Week Tight Closing list can't be built today.")
    st.caption(f"Session **{sd}** · {len(lists['ACTION'])} big-volume breakouts · {len(lists['RECENT'])} breakout → tight · {len(lists['3WTC'])} on 3-Week Tight Closing · {len(lists['HTF'])} on High Tight Flag"
               + (" · ranks saved earlier tonight are loaded" if saved else ""))
    cc = _column_config(st, mcfg)
    edited = {}
    tabs = st.tabs([f"{SETUPS[k]} ({len(lists[k])})" for k in ("ACTION", "RECENT", "3WTC", "HTF")])
    helptext = {
        "ACTION": f"Volume is the setup, tightness is the entry. Breakouts ≥ {cfg['act_min_chg']:g}% on ≥ {cfg['act_min_vol']:g}x "
                  f"volume and ≥ {cfg['act_min_value']:g} traded, today and the last {int(cfg['act_days'])} sessions (still holding). "
                  "Today's: alert at the breakout-day high for a day-2 ORH. Older: wait for the tight day / hourly flag. "
                  "EP = check the news.",
        "RECENT": f"A breakout on volume 1–{int(cfg['rec_max_age'])} sessions ago (MarketInOut days-since-BO with breakout value ≥ "
                  f"{cfg['act_min_value']:g}, a recorded breakout ≥ {cfg['act_min_vol']:g}x, or one you tagged), now tight "
                  f"(rel tight ≤ {cfg['rec_max_reltight']:g}) within {cfg['rec_max_off1m_adr']:g} ADR of the 1-month high. "
                  "Newest first: ≤ 3 days, ≤ 1 week, ≤ 2 weeks, ≤ 4 weeks.",
        "3WTC": "Weekly closes bunched together (MIO weekly tight closes + this week's close change in ADRs), within "
                f"{cfg['wtc_max_off1m_adr']:g} ADR of the 1-month high, {cfg['wtc_min_rwd']:g}–{cfg['wtc_max_rwd']:g} ADR "
                "from the 10w line. Check the weekly chart: the last 3 closes should sit in a tight band.",
        "HTF": f"Ti65 ≥ {cfg['htf_min_ti65']:g}, ADR ≥ {cfg['htf_min_adr']:g}%, rel tight ≤ {cfg['htf_max_reltight']:g}, "
               f"{cfg['htf_min_rwd']:g}–{cfg['htf_max_rwd']:g} ADR from 10w — or a leader flag within "
               f"{cfg['htf_flag_max_10ma_adr']:g} ADR of the 10MA. Far from the 10w line: count the steps — "
               "3rd+ step = pivot must clear any climax high, half size.",
    }
    for tab, key in zip(tabs, ("ACTION", "RECENT", "3WTC", "HTF")):
        with tab:
            st.caption(helptext[key])
            v = lists[key]
            if v.empty:
                st.info("Nothing matches tonight."); edited[key] = v; continue
            cols = [c for c in {"ACTION": ACTION_SHOW, "RECENT": RECENT_SHOW}.get(key, SHOW) if c in v.columns]
            edited[key] = st.data_editor(
                v[cols].replace([np.inf, -np.inf], np.nan), column_config=cc, hide_index=True, use_container_width=True,
                disabled=[c for c in cols if c not in EDIT_COLS], key=f"sl_{mcfg['market']}_{key}_{sd}")
    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("Save shortlist", type="primary", disabled=not can_save, key="sl_save"):
            try:
                n = save_shortlist(sb, mcfg, sd, {k: e for k, e in edited.items() if not e.empty}, _prep(base_df))
                st.success(f"Saved {n} names for {sd}.")
            except Exception as e:
                st.error("Could not save. Run supabase_shortlist.sql once in Supabase "
                         f"(it allows scanner = 'SHORTLIST'). ({e})")
    with c2:
        ranked = []
        for key, e in edited.items():
            if e is None or e.empty:
                continue
            k = e[e["Keep"] == True].copy()  # noqa: E712
            k["_r"] = pd.to_numeric(k["Rank"], errors="coerce")
            ranked += list(k.sort_values("_r", na_position="last")[["Symbol", "_r"]].itertuples(index=False))
        seen, syms = set(), []
        for s, r in sorted(ranked, key=lambda x: (not np.isfinite(_num(x[1])), _num(x[1]) if np.isfinite(_num(x[1])) else 0)):
            if s not in seen:
                seen.add(s); syms.append(s)
        if syms:
            st.text_area("Kept names, best rank first (for TradingView)",
                         ",".join(gp.tv_symbol(s, mcfg) for s in syms), height=70, key="sl_copy")
    st.markdown("---")
    render_why_not(st, base_df, cfg, mcfg, sb)


# ── "Why isn't it on the list?" — rule-by-rule check for one stock ──
def _fmt(x, nd=2, suffix=""):
    return "—" if not np.isfinite(_num(x)) else f"{_num(x):.{nd}f}{suffix}"


def rule_checks(r, setup, cfg, liq_unit=""):
    """Rows of {Rule, Needs, Actual, Pass, Watch for} for one prepped scan row and one setup."""
    g = lambda c: _num(r.get(c))
    rwd, m10, m20, off = g("_rel_wk_dist"), g("_ma10_adr"), g("_ma20_adr"), g("_off21_adr")
    adr, liq, w10 = g("Adr"), g("_avgvol_mln"), g("W_Dist10wMA")
    out = []

    def add(rule, needs, actual, ok, watch):
        out.append({"Rule": rule, "Needs": needs, "Actual": actual,
                    "Pass": "—" if ok is None else ("✅" if ok else "❌"), "Watch for": "" if ok else watch})

    def chk(v, cond):
        return None if not np.isfinite(v) else bool(cond(v))

    add("Liquidity", f"avg traded value ≥ {cfg['min_liq']:g} {liq_unit}", _fmt(liq, 0, f" {liq_unit}"),
        chk(liq, lambda v: v >= cfg["min_liq"]), "Too thin: breakouts fade. Only if the breakout day itself trades big (≥ 2x and ≥ 2–3x ADV).")
    add("Above the 10-week line", "10w distance > 0", _fmt(w10, 1, "%"), chk(w10, lambda v: v > 0),
        "Below the 10w line: no uptrend yet. Wait for it to reclaim and hold.")
    add("10MA / 20MA", f"both ≥ {cfg['ma_min_adr']:g} ADR", f"{_fmt(m10, 1)} / {_fmt(m20, 1)} ADR",
        None if not (np.isfinite(m10) and np.isfinite(m20)) else (m10 >= cfg["ma_min_adr"] and m20 >= cfg["ma_min_adr"]),
        "Below its short MAs: let it reclaim the 10MA and 20MA first.")
    if setup == "3WTC":
        wt, wk = g("W_TightCloses_10w"), g("_wkchg_adr")
        add("Weekly tight closes", f"≥ {cfg['wtc_min_tight']:g}", _fmt(wt, 0), chk(wt, lambda v: v >= cfg["wtc_min_tight"]),
            "Weekly closes still loose: give it another quiet week or two.")
        add("This week's close change", f"≤ {cfg['wtc_max_wkchg_adr']:g} ADR", _fmt(wk, 2, " ADR"),
            chk(wk, lambda v: v <= cfg["wtc_max_wkchg_adr"]),
            "Big weekly move: wait for a week that closes near last week's close.")
        add("Near the pivot (1M high)", f"≤ {cfg['wtc_max_off1m_adr']:g} ADR below", _fmt(off, 2, " ADR"),
            chk(off, lambda v: v <= cfg["wtc_max_off1m_adr"]), "Too far below the high: needs to work back up to the range top.")
        add("ADRs from the 10w line", f"{cfg['wtc_min_rwd']:g}–{cfg['wtc_max_rwd']:g}", _fmt(rwd, 2),
            chk(rwd, lambda v: cfg["wtc_min_rwd"] <= v <= cfg["wtc_max_rwd"]),
            "Too close: no prior move yet." if np.isfinite(rwd) and rwd < cfg["wtc_min_rwd"]
            else "Extended: wait for the 10w line to catch up (sideways weeks).")
        add("ADR", f"≥ {cfg['wtc_min_adr']:g}%", _fmt(adr, 1, "%"), chk(adr, lambda v: v >= cfg["wtc_min_adr"]),
            "Low volatility: moves will be small.")
    else:
        ti, rt = g("Ti65"), g("_rel_tightness_today")
        add("Momentum (Ti65)", f"≥ {cfg['htf_min_ti65']:g}", _fmt(ti, 2), chk(ti, lambda v: v >= cfg["htf_min_ti65"]),
            "Not enough momentum for an HTF: maybe a 3-week tight closing instead.")
        add("ADR", f"≥ {cfg['htf_min_adr']:g}%", _fmt(adr, 1, "%"), chk(adr, lambda v: v >= cfg["htf_min_adr"]),
            "Not volatile enough for an HTF.")
        add("Rel tightness", f"≤ {cfg['htf_max_reltight']:g}", _fmt(rt, 2), chk(rt, lambda v: v <= cfg["htf_max_reltight"]),
            "Not tight yet: watch for 2–3 narrow/inside days (NR4 under 0.8 ADR).")
        add("Near the pivot (1M high)", f"≤ {cfg['htf_max_off1m_adr']:g} ADR below", _fmt(off, 2, " ADR"),
            chk(off, lambda v: v <= cfg["htf_max_off1m_adr"]), "Too far below the flag high: the flag is too deep or still pulling back.")
        normal = chk(rwd, lambda v: cfg["htf_min_rwd"] <= v <= cfg["htf_max_rwd"])
        leader = (np.isfinite(rwd) and rwd > cfg["htf_max_rwd"] and np.isfinite(m10) and m10 <= cfg["htf_flag_max_10ma_adr"])
        if normal or leader:
            add("Distance", f"{cfg['htf_min_rwd']:g}–{cfg['htf_max_rwd']:g} ADR from 10w, or leader flag ≤ "
                f"{cfg['htf_flag_max_10ma_adr']:g} ADR above 10MA",
                f"{_fmt(rwd, 1)} ADR from 10w · {_fmt(m10, 1)} ADR from 10MA" + (" (leader flag)" if leader else ""), True, "")
        else:
            if np.isfinite(rwd) and rwd < cfg["htf_min_rwd"]:
                w = "Hasn't moved enough off the 10w line for an HTF."
            elif np.isfinite(m10):
                w = (f"Extended {m10:.1f} ADR above the 10MA. Wait for the flag to go sideways until the 10MA is within "
                     f"{cfg['htf_flag_max_10ma_adr']:g} ADR (≈ {max(m10 - cfg['htf_flag_max_10ma_adr'], 0) * adr:.1f}% to close).")
            else:
                w = "Distance data missing."
            add("Distance", f"{cfg['htf_min_rwd']:g}–{cfg['htf_max_rwd']:g} ADR from 10w, or leader flag ≤ "
                f"{cfg['htf_flag_max_10ma_adr']:g} ADR above 10MA", f"{_fmt(rwd, 1)} ADR from 10w · {_fmt(m10, 1)} ADR from 10MA",
                None if not np.isfinite(rwd) else False, w)
    return out


def last_snapshot_row(sb, mcfg, sym):
    """Most recent saved metrics for a symbol (any scanner): (date, scanner, one-row DataFrame) or None."""
    r = (sb.table("daily_snapshots").select("snap_date,scanner,metrics").eq("user_id", mcfg["user_id"])
         .eq("market", mcfg["market"]).eq("symbol", sym).order("snap_date", desc=True).limit(1).execute().data)
    if not r:
        return None
    m = dict(r[0].get("metrics") or {})
    m.pop("setups", None)
    m["Symbol"] = sym
    return str(r[0]["snap_date"])[:10], r[0].get("scanner"), pd.DataFrame([m])


def render_why_not(st, base_df, cfg, mcfg, sb=None):
    st.markdown("#### Why isn't it on the list?")
    c1, c2 = st.columns([2, 1])
    sym = c1.text_input("Symbol", key="sl_why_sym", placeholder="e.g. MOBIKWIK").strip().upper()
    setup = c2.selectbox("Setup", ["3WTC", "HTF"], format_func=lambda k: SETUPS[k], index=1, key="sl_why_setup")
    if not sym:
        return
    d = _prep(base_df)
    hit = d[d["Symbol"].astype(str).str.upper() == sym]
    asof = None
    if hit.empty:
        near = [s for s in d["Symbol"].astype(str) if sym[:4] in s.upper()][:5]
        snap = None
        if sb is not None:
            try:
                snap = last_snapshot_row(sb, mcfg, sym)
            except Exception:
                snap = None
        if snap is None:
            st.warning(f"{sym} isn't in today's scan and has never been saved, so none of the rules can be checked. "
                       "The MIO screens only return stocks that pass their own filters (RS, price, volume)."
                       + (f" Did you mean: {', '.join(near)}?" if near else ""))
            return
        asof, scanner, snap_df = snap
        hit = _prep(snap_df)
        st.warning(f"{sym} dropped out of today's scan (it no longer passes a MarketInOut screen filter, usually RS). "
                   f"Checked on its last saved data from **{asof}** ({str(scanner).title()}) — re-check the chart, "
                   "the numbers below are not today's.")
    r = hit.iloc[0]
    liq_unit = "₹ cr" if mcfg.get("market") == "NSE" else "$M"
    rows = rule_checks(r, setup, cfg, liq_unit)
    fails = [x for x in rows if x["Pass"] == "❌"]
    unknown = [x for x in rows if x["Pass"] == "—"]
    when = f" on {asof} data" if asof else ""
    passed = sum(1 for x in rows if x["Pass"] == "✅")
    if not fails and not unknown:
        st.success(f"{sym} passes every {SETUPS[setup]} rule{when}" + (" — but it's not in today's scan, so it can't be on the list."
                   if asof else " — it should be on that list tonight."))
    elif fails:
        st.error(f"{sym} meets {passed} of {len(rows)} {SETUPS[setup]} rules{when}. Fails: " + ", ".join(x["Rule"] for x in fails) + ".")
    else:
        st.info(f"{sym}: some data is missing ({', '.join(x['Rule'] for x in unknown)}).")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    if fails:
        st.markdown("**What to watch for:**\n" + "\n".join(f"- **{x['Rule']}:** {x['Watch for']}" for x in fails))
