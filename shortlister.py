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

SETUPS = {"3WTC": "3-Week Tight Closing", "HTF": "High Tight Flag"}

SHORTLIST_DEFAULTS = {
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
MARKET_OVERRIDES = {"NSE": {"min_liq": 50.0, "htf_min_adr": 5.0},
                    "USA": {"min_liq": 30.0, "htf_min_adr": 6.0}}
RULE_LABELS = {
    "min_liq": "Min avg traded value (₹ cr / $M)", "ma_min_adr": "10/20MA floor (ADRs)", "late_rwd": "Flag 'check steps' above (ADRs from 10w)",
    "wtc_min_tight": "3WTC: min weekly tight closes", "wtc_max_wkchg_adr": "3WTC: max week's close change (ADRs)",
    "wtc_max_off1m_adr": "3WTC: max below 1M high (ADRs)", "wtc_min_rwd": "3WTC: min ADRs from 10w",
    "wtc_max_rwd": "3WTC: max ADRs from 10w", "wtc_min_adr": "3WTC: min ADR",
    "htf_min_ti65": "HTF: min Ti65", "htf_min_adr": "HTF: min ADR", "htf_max_reltight": "HTF: max rel tightness",
    "htf_min_rwd": "HTF: min ADRs from 10w", "htf_max_rwd": "HTF: max ADRs from 10w",
    "htf_flag_max_10ma_adr": "HTF leader flag: max ADRs above 10MA", "htf_max_off1m_adr": "HTF: max below 1M high (ADRs)",
}
gp.RULE_LABELS.update(RULE_LABELS)

EDIT_COLS = ["Keep", "Rank", "Comment", "Weekly comment"]


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
        out[key] = v.reset_index(drop=True)
    return out


def _num(x):
    try:
        x = float(x)
        return x if np.isfinite(x) else np.nan
    except (TypeError, ValueError):
        return np.nan


SHOW = ["Keep", "Rank", "Symbol", "Sector", "Grp hold %", "Saved", "_rel_wk_dist", "_ma10_adr", "Pullback",
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
    st.subheader("Shortlister  ·  two setups, ranked by you")
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
    lists = build_shortlists(base_df, cfg, _holding_map(sb, mcfg, base_df) if sb else {},
                             _tags_map(sb, mcfg) if sb else {}, saved)
    if "W_TightCloses_10w" not in base_df.columns or base_df["W_TightCloses_10w"].isna().all():
        st.warning("Weekly scan data is missing, so the 3-Week Tight Closing list can't be built today.")
    st.caption(f"Session **{sd}** · {len(lists['3WTC'])} on 3-Week Tight Closing · {len(lists['HTF'])} on High Tight Flag"
               + (" · ranks saved earlier tonight are loaded" if saved else ""))
    cc = _column_config(st, mcfg)
    edited = {}
    tabs = st.tabs([f"{SETUPS['3WTC']} ({len(lists['3WTC'])})", f"{SETUPS['HTF']} ({len(lists['HTF'])})"])
    helptext = {
        "3WTC": "Weekly closes bunched together (MIO weekly tight closes + this week's close change in ADRs), within "
                f"{cfg['wtc_max_off1m_adr']:g} ADR of the 1-month high, {cfg['wtc_min_rwd']:g}–{cfg['wtc_max_rwd']:g} ADR "
                "from the 10w line. Check the weekly chart: the last 3 closes should sit in a tight band.",
        "HTF": f"Ti65 ≥ {cfg['htf_min_ti65']:g}, ADR ≥ {cfg['htf_min_adr']:g}%, rel tight ≤ {cfg['htf_max_reltight']:g}, "
               f"{cfg['htf_min_rwd']:g}–{cfg['htf_max_rwd']:g} ADR from 10w — or a leader flag within "
               f"{cfg['htf_flag_max_10ma_adr']:g} ADR of the 10MA. Far from the 10w line: count the steps — "
               "3rd+ step = pivot must clear any climax high, half size.",
    }
    for tab, key in zip(tabs, ("3WTC", "HTF")):
        with tab:
            st.caption(helptext[key])
            v = lists[key]
            if v.empty:
                st.info("Nothing matches tonight."); edited[key] = v; continue
            cols = [c for c in SHOW if c in v.columns]
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
