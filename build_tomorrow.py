"""
Build Tomorrow — turns today's scan into tomorrow's plan and saves a daily snapshot.

Goal: manage the lists so no potential trade is missed.
Every stock the scanner sees gets exactly one label (the five situations from the playbook),
and every day's labels are saved to Supabase so the watchlist history builds up.

Labels
  1_BUY_SIGNAL   on yesterday's list, broke out with volume          -> buy (already happened today)
  2_SAVE         not on the list, broke out with >= save_min_vol     -> save for later (don't chase)
  3_WAIT         on the list, broke out without volume               -> stays on tomorrow's list
  4_RESETUP      saved stock setting up again (pullback / quiet)      -> back on tomorrow's list
  5_REMOVE       gapped too far, or saved stock failed / went stale   -> skip or remove
  CANDIDATE      new coiled setup that passes the scanner rules       -> pick the best ~10 for tomorrow
  CHECK          saved/listed stock not in today's scan, or missing weekly data -> look at the chart
  SCANNED        everything else (kept in the snapshot for history only)

Tables used (see supabase_migration.sql):
  daily_snapshots   one row per (user, date, symbol)
  saved_breakouts   the existing watchlist, with extra breakout-day columns
"""
from datetime import datetime, date, timedelta
import json
import numpy as np
import pandas as pd

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    IST = None

USER_ID = "nse_user"
MARKET = "NSE"
SCANNER = "ANTICIPATION"

DEFAULTS = {
    # tomorrow's candidates (evening list)
    "min_scan_count": 2,      # in at least 2 of 1M/3M/6M
    "min_adr": 3.0,
    "min_liq": 10.0,          # _avgvol_mln (crore) — liquidity
    "max_reltight": 0.8,      # _nr4 / ADR
    "max_rwd": 1.5,           # |dist from 10w EMA| in ADRs
    "min_wktight": 2,
    "min_chg": -1.0,          # not yet broken out
    "max_chg": 3.0,
    "list_size": 10,
    # breakouts
    "bo_min_chg": 2.0,
    "bo_min_vol": 1.5,        # on-list breakout needs this volume ratio
    "save_min_vol": 2.0,      # off-list breakout needs this to be saved
    "gap_max_adr": 2.0,       # chg% > this many ADRs = too far, skip
    # saved stocks
    "pullback_band": 2.0,     # |dist to 10/20 EMA| in %
    "stale_days": 20,         # saved this long with no re-setup -> suggest remove
}

LIST_LABELS = ["1_BUY_SIGNAL", "2_SAVE", "3_WAIT", "4_RESETUP", "5_REMOVE", "CANDIDATE", "CHECK", "SCANNED"]
TOMORROW_LABELS = {"3_WAIT", "4_RESETUP"}  # always carried onto tomorrow's list

SNAPSHOT_METRICS = ["Last", "_chg_percentclose", "_vol_ratio", "Adr", "_nr4", "_rel_tightness_today",
                    "_rel_wk_dist", "W_Dist10wMA", "W_TightCloses_10w", "_10madist", "_20madist",
                    "_avgvol_mln", "Avg_RS", "RS_1M", "RS_3M", "RS_6M", "Sector"]


def today_ist():
    return datetime.now(IST).date() if IST else date.today()


def session_date(now):
    """Date of the latest NSE session: before 09:15 IST or on weekends, step back to the last weekday."""
    d = now.date()
    if now.hour * 60 + now.minute < 9 * 60 + 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ────────────────────────────────────────────────────────────────────────────
# Pure logic (no Streamlit, no DB) — easy to test
# ────────────────────────────────────────────────────────────────────────────
def add_derived(df):
    """Add Scan_Count and today's tightness/distance metrics. Never drops rows."""
    d = df.copy()
    for c in ["RS_1M", "RS_3M", "RS_6M"]:
        if c not in d.columns:
            d[c] = 0
    d["Scan_Count"] = (d[["RS_1M", "RS_3M", "RS_6M"]].fillna(0) > 0).sum(axis=1).astype(int)
    adr = d["Adr"].replace(0, np.nan) if "Adr" in d.columns else pd.Series(np.nan, index=d.index)
    d["_rel_tightness_today"] = (d["_nr4"] / adr).round(2) if "_nr4" in d.columns else np.nan
    if "W_Dist10wMA" in d.columns:
        d["_rel_wk_dist"] = (d["W_Dist10wMA"].abs() / adr).round(2)   # NaN stays NaN (not 999)
    else:
        d["_rel_wk_dist"] = np.nan
        d["W_Dist10wMA"] = np.nan
    if "W_TightCloses_10w" not in d.columns:
        d["W_TightCloses_10w"] = np.nan
    if "dvol" in d.columns and "_avgvol_mln" in d.columns:
        d["_vol_ratio"] = (d["dvol"] / d["_avgvol_mln"].replace(0, np.nan)).round(2)
    d["Missing_Weekly"] = d["W_Dist10wMA"].isna() | d["W_TightCloses_10w"].isna()
    return d


def _num(x, default=np.nan):
    try:
        v = float(x)
        return v if np.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def classify(scan_df, yesterday_list, saved_rows, cfg=None, today=None):
    """
    scan_df        today's merged scan (gtt_base_df)
    yesterday_list set of symbols on the last saved 'tomorrow' list
    saved_rows     list of dicts from saved_breakouts (status != removed)
    returns (labelled_df, saved_updates)
       labelled_df   one row per symbol, with columns Label, Reason, On_List, Saved, Keep
       saved_updates list of dicts: {symbol, action: 'remove'|'seen', reason}
    """
    cfg = {**DEFAULTS, **(cfg or {})}
    today = today or today_ist()
    d = add_derived(scan_df)
    d = d.drop_duplicates("Symbol").set_index("Symbol", drop=False)

    saved = {r["symbol"]: r for r in (saved_rows or [])}
    d["On_List"] = d["Symbol"].isin(yesterday_list)
    d["Saved"] = d["Symbol"].isin(saved.keys())
    d["Label"] = "SCANNED"
    d["Reason"] = ""

    chg = d["_chg_percentclose"].fillna(0)
    vol = d["_vol_ratio"].fillna(0)
    adr = d["Adr"].fillna(0)
    broke_out = chg >= cfg["bo_min_chg"]
    too_far = (adr > 0) & (chg > cfg["gap_max_adr"] * adr)

    def put(mask, label, reason):
        m = mask & (d["Label"] == "SCANNED")
        d.loc[m, "Label"] = label
        d.loc[m, "Reason"] = reason if isinstance(reason, str) else reason[m]

    # 5 — on the list but ran too far today
    put(d["On_List"] & too_far, "5_REMOVE",
        "Gapped/ran " + chg.round(1).astype(str) + "% (> " + str(cfg["gap_max_adr"]) + " ADR). Skip.")
    # 1 — on the list, broke out with volume
    put(d["On_List"] & broke_out & (vol >= cfg["bo_min_vol"]), "1_BUY_SIGNAL",
        "On list, +" + chg.round(1).astype(str) + "% on " + vol.round(1).astype(str) + "x vol")
    # 3 — on the list, broke out without volume
    put(d["On_List"] & broke_out & (vol < cfg["bo_min_vol"]), "3_WAIT",
        "Broke out on only " + vol.round(1).astype(str) + "x vol. Keep waiting.")
    # 2 — not on list, strong volume breakout
    put(~d["On_List"] & ~d["Saved"] & broke_out & (vol >= cfg["save_min_vol"]), "2_SAVE",
        "Off-list breakout +" + chg.round(1).astype(str) + "% on " + vol.round(1).astype(str) + "x vol. Save, don't chase.")

    # Saved stocks (situations 4 and 5)
    saved_updates = []
    for sym, row in saved.items():
        if sym not in d.index:
            continue
        r = d.loc[sym]
        if d.at[sym, "Label"] != "SCANNED":
            saved_updates.append({"symbol": sym, "action": "seen"})
            continue
        last = _num(r.get("Last"))
        prev_close = _num(row.get("bo_prev_close"))
        bo_close = _num(row.get("bo_close"))
        radr = _num(r.get("Adr"), 0)
        d10 = abs(_num(r.get("_10madist"), 99))
        d20 = abs(_num(r.get("_20madist"), 99))
        rt = _num(r.get("_rel_tightness_today"), 99)
        if np.isfinite(prev_close) and np.isfinite(last) and last < prev_close:
            d.at[sym, "Label"] = "5_REMOVE"
            d.at[sym, "Reason"] = f"Failed: closed {last:g} below pre-breakout close {prev_close:g}"
            saved_updates.append({"symbol": sym, "action": "remove", "reason": "failed"})
            continue
        near_bo = (not np.isfinite(bo_close)) or (
            np.isfinite(last) and bo_close * (1 - radr / 100) <= last <= bo_close * (1 + 2 * radr / 100))
        if min(d10, d20) <= cfg["pullback_band"]:
            which = "10" if d10 <= d20 else "20"
            d.at[sym, "Label"] = "4_RESETUP"
            d.at[sym, "Reason"] = f"Pulled back to {which} EMA ({min(d10, d20):.1f}% away)"
        elif rt <= cfg["max_reltight"] and near_bo:
            d.at[sym, "Label"] = "4_RESETUP"
            d.at[sym, "Reason"] = f"Quiet near breakout level (rel tight {rt:.2f})"
        else:
            age = _age_days(row, today)
            if age is not None and age >= cfg["stale_days"]:
                d.at[sym, "Label"] = "5_REMOVE"
                d.at[sym, "Reason"] = f"Stale: saved {age} days, no re-setup"
                saved_updates.append({"symbol": sym, "action": "remove", "reason": "stale"})
                continue
            d.at[sym, "Reason"] = "Saved — not set up yet"
        saved_updates.append({"symbol": sym, "action": "seen"})

    # New candidates for tomorrow
    cand = (
        (d["Label"] == "SCANNED")
        & (d["Scan_Count"] >= cfg["min_scan_count"])
        & (adr >= cfg["min_adr"])
        & (d["_avgvol_mln"].fillna(0) >= cfg["min_liq"])
        & (d["_rel_tightness_today"].fillna(99) <= cfg["max_reltight"])
        & chg.between(cfg["min_chg"], cfg["max_chg"])
    )
    weekly_ok = (d["_rel_wk_dist"] <= cfg["max_rwd"]) & (d["W_TightCloses_10w"] >= cfg["min_wktight"])
    put(cand & weekly_ok, "CANDIDATE",
        "Scan " + d["Scan_Count"].astype(str) + "/3, rel tight " + d["_rel_tightness_today"].round(2).astype(str))
    # passes everything daily, but weekly data missing -> don't silently drop it
    put(cand & d["Missing_Weekly"], "CHECK", pd.Series("Passes daily rules, weekly data missing — check chart", index=d.index))
    # on list, still in scan, nothing happened: if it still qualifies it's a CANDIDATE above; else note it
    d.loc[d["On_List"] & (d["Label"] == "SCANNED"), "Reason"] = "Was on list, no longer qualifies"

    # Rows for listed / saved stocks that are NOT in today's scan at all
    missing = []
    for sym in sorted(set(yesterday_list) | set(saved.keys())):
        if sym in d.index:
            continue
        row = saved.get(sym, {})
        age = _age_days(row, today) if row else None
        if row and age is not None and age >= cfg["stale_days"]:
            label, reason = "5_REMOVE", f"Not in scan, saved {age} days — suggest remove"
            saved_updates.append({"symbol": sym, "action": "remove", "reason": "stale"})
        else:
            label, reason = "CHECK", "Not in today's scan — check chart"
        missing.append({"Symbol": sym, "Label": label, "Reason": reason,
                        "On_List": sym in yesterday_list, "Saved": sym in saved, "Scan_Count": 0})
    if missing:
        d = pd.concat([d, pd.DataFrame(missing).set_index("Symbol", drop=False)])

    # Rank and pre-tick tomorrow's list
    d["Rank"] = np.nan
    cm = d["Label"] == "CANDIDATE"
    ranked = d[cm].sort_values(["Scan_Count", "_rel_tightness_today", "Avg_RS"],
                               ascending=[False, True, False], na_position="last")
    d.loc[ranked.index, "Rank"] = range(1, len(ranked) + 1)
    d["Keep"] = d["Label"].isin(TOMORROW_LABELS) | (cm & (d["Rank"] <= cfg["list_size"]))
    d["Save_It"] = d["Label"] == "2_SAVE"

    order = {k: i for i, k in enumerate(LIST_LABELS)}
    d["_o"] = d["Label"].map(order)
    d = d.sort_values(["_o", "Rank"], na_position="last").drop(columns="_o").reset_index(drop=True)
    return d, saved_updates


def _age_days(row, today):
    s = row.get("bo_date") or row.get("saved_date")
    if not s:
        return None
    try:
        return (today - pd.to_datetime(str(s)[:10]).date()).days
    except Exception:
        return None


def snapshot_rows(labelled, snap_date, user_id=USER_ID):
    rows = []
    for _, r in labelled.iterrows():
        m = {}
        for c in SNAPSHOT_METRICS:
            if c in labelled.columns:
                v = r.get(c)
                if isinstance(v, (np.generic,)):
                    v = v.item()
                if isinstance(v, float) and not np.isfinite(v):
                    v = None
                if v is not None and not (isinstance(v, float) and np.isnan(v)):
                    m[c] = v
        m["reason"] = r.get("Reason") or ""
        m["on_list"] = bool(r.get("On_List"))
        m["saved"] = bool(r.get("Saved"))
        rows.append({
            "user_id": user_id,
            "market": MARKET,
            "scanner": SCANNER,
            "snap_date": snap_date.isoformat(),
            "symbol": r["Symbol"],
            "list": r["Label"],
            "selected": bool(r.get("Keep")),
            "scan_count": int(_num(r.get("Scan_Count"), 0)),
            "metrics": m,
        })
    return rows


def breakout_record(r, snap_date):
    last = _num(r.get("Last"))
    chg = _num(r.get("_chg_percentclose"), 0)
    prev = round(last / (1 + chg / 100), 2) if np.isfinite(last) else None
    return {
        "bo_date": snap_date.isoformat(),
        "bo_close": None if not np.isfinite(last) else round(last, 2),
        "bo_prev_close": prev,
        "bo_vol_ratio": None if not np.isfinite(_num(r.get("_vol_ratio"))) else round(_num(r.get("_vol_ratio")), 2),
        "bo_adr": None if not np.isfinite(_num(r.get("Adr"))) else round(_num(r.get("Adr")), 2),
    }


# ────────────────────────────────────────────────────────────────────────────
# Database helpers
# ────────────────────────────────────────────────────────────────────────────
def load_yesterday_list(sb, before_date, user_id=USER_ID):
    """Symbols ticked (selected) on the most recent snapshot before `before_date`."""
    r = (sb.table("daily_snapshots").select("snap_date").eq("user_id", user_id).eq("market", MARKET)
         .eq("scanner", SCANNER).lt("snap_date", before_date.isoformat()).order("snap_date", desc=True).limit(1).execute())
    if not r.data:
        return None, set()
    last = r.data[0]["snap_date"]
    r2 = (sb.table("daily_snapshots").select("symbol").eq("user_id", user_id).eq("market", MARKET)
          .eq("scanner", SCANNER).eq("snap_date", last).eq("selected", True).execute())
    return last, {x["symbol"] for x in r2.data}


def load_saved(sb, user_id=USER_ID):
    r = sb.table("saved_breakouts").select("*").eq("user_id", user_id).execute()
    return [x for x in r.data if (x.get("status") or "saved") != "removed"]


def save_plan(sb, labelled, saved_updates, snap_date, user_id=USER_ID):
    rows = snapshot_rows(labelled, snap_date, user_id)
    for i in range(0, len(rows), 500):
        sb.table("daily_snapshots").upsert(rows[i:i + 500], on_conflict="user_id,market,scanner,snap_date,symbol").execute()

    all_saved = sb.table("saved_breakouts").select("id,symbol,status").eq("user_id", user_id).execute().data
    by_sym = {x["symbol"]: x for x in all_saved}
    added = 0
    for _, r in labelled[labelled["Save_It"] == True].iterrows():  # noqa: E712
        rec = {**breakout_record(r, snap_date), "status": "saved", "last_seen": snap_date.isoformat(),
               "removed_date": None, "removed_reason": None}
        if r["Symbol"] in by_sym:
            sb.table("saved_breakouts").update(rec).eq("user_id", user_id).eq("symbol", r["Symbol"]).execute()
        else:
            sb.table("saved_breakouts").insert({"symbol": r["Symbol"], "user_id": user_id,
                                                "saved_date": datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
                                                **rec}).execute()
        added += 1
    removed = 0
    for u in saved_updates:
        if u["action"] == "remove" and u.get("confirmed"):
            sb.table("saved_breakouts").update({"status": "removed", "removed_date": snap_date.isoformat(),
                                                "removed_reason": u.get("reason")}) \
                .eq("user_id", user_id).eq("symbol", u["symbol"]).execute()
            removed += 1
        elif u["action"] == "seen":
            sb.table("saved_breakouts").update({"last_seen": snap_date.isoformat()}) \
                .eq("user_id", user_id).eq("symbol", u["symbol"]).execute()
    return len(rows), added, removed


# ────────────────────────────────────────────────────────────────────────────
# Streamlit UI
# ────────────────────────────────────────────────────────────────────────────
def render_build_tomorrow(st, supabase, base_df, saved_prefs):
    st.subheader("Build Tomorrow")
    st.caption("Every stock gets one label. Tick what goes on tomorrow's list, then save — "
               "the whole day is stored as a snapshot so nothing is lost.")
    if supabase is None:
        st.error("Database not connected."); return
    if base_df is None or base_df.empty:
        st.info("Click 'Generate GTT Trading Plan' first."); return

    now = datetime.now(IST) if IST else datetime.now()
    snap_date = session_date(now)
    mins = now.hour * 60 + now.minute
    if now.weekday() < 5 and 9 * 60 + 15 <= mins < 15 * 60 + 30:
        st.warning(f"It's {now:%H:%M} IST — NSE is still open, so today's numbers are intraday. "
                   "Best run after 15:30 IST (8:00 PM Sydney).")
    st.caption(f"Snapshot date: **{snap_date}** (last NSE session as of {now:%a %H:%M} IST).")

    # Settings (seeded from saved prefs so the 8-week rules stay fixed)
    bt = saved_prefs.get("build_tomorrow", {})
    cfg = {**DEFAULTS, **bt}
    with st.expander("List rules (saved with 'Save filter settings' — don't change for 8 weeks)"):
        c1, c2, c3 = st.columns(3)
        with c1:
            cfg["min_scan_count"] = st.number_input("Min scan count (of 3)", 1, 3, int(cfg["min_scan_count"]), key="bt_sc")
            cfg["min_adr"] = st.number_input("Min ADR %", value=float(cfg["min_adr"]), step=0.5, key="bt_adr")
            cfg["min_liq"] = st.number_input("Min avg value (cr)", value=float(cfg["min_liq"]), step=1.0, key="bt_liq")
            cfg["list_size"] = st.number_input("Tomorrow list size", 1, 50, int(cfg["list_size"]), key="bt_ls")
        with c2:
            cfg["max_reltight"] = st.number_input("Max rel tightness (NR4/ADR)", value=float(cfg["max_reltight"]), step=0.1, key="bt_rt")
            cfg["max_rwd"] = st.number_input("Max ADRs from 10w EMA", value=float(cfg["max_rwd"]), step=0.5, key="bt_rwd")
            cfg["min_wktight"] = st.number_input("Min weekly tight closes", 0, 5, int(cfg["min_wktight"]), key="bt_wt")
            cfg["pullback_band"] = st.number_input("Pullback band to 10/20 EMA (%)", value=float(cfg["pullback_band"]), step=0.5, key="bt_pb")
        with c3:
            cfg["bo_min_chg"] = st.number_input("Breakout min chg %", value=float(cfg["bo_min_chg"]), step=0.5, key="bt_bc")
            cfg["bo_min_vol"] = st.number_input("On-list breakout min vol (x)", value=float(cfg["bo_min_vol"]), step=0.1, key="bt_bv")
            cfg["save_min_vol"] = st.number_input("Off-list save min vol (x)", value=float(cfg["save_min_vol"]), step=0.1, key="bt_sv")
            cfg["gap_max_adr"] = st.number_input("Too far = chg > N × ADR", value=float(cfg["gap_max_adr"]), step=0.5, key="bt_gap")
            cfg["stale_days"] = st.number_input("Stale after (days)", 1, 120, int(cfg["stale_days"]), key="bt_st")
        st.session_state["bt_cfg"] = {k: cfg[k] for k in DEFAULTS}

    try:
        prev_date, ylist = load_yesterday_list(supabase, snap_date)
        saved_rows = load_saved(supabase)
    except Exception as e:
        st.error(f"Could not read snapshots/watchlist. Did you run supabase_migration.sql? ({e})"); return

    if prev_date:
        st.caption(f"Comparing with the list saved on **{prev_date}** ({len(ylist)} symbols) and "
                   f"{len(saved_rows)} saved stocks.")
    else:
        st.info("No previous list yet — situations 1 and 3 start working after your first save.")

    lab, saved_updates = classify(base_df, ylist, saved_rows, cfg, snap_date)

    # Summary tiles
    counts = lab["Label"].value_counts()
    names = {"1_BUY_SIGNAL": "1 · Bought/buy", "2_SAVE": "2 · Save", "3_WAIT": "3 · Wait",
             "4_RESETUP": "4 · Re-setup", "5_REMOVE": "5 · Remove", "CANDIDATE": "New candidates",
             "CHECK": "Check chart"}
    cols = st.columns(len(names))
    for c, (k, n) in zip(cols, names.items()):
        c.metric(n, int(counts.get(k, 0)))

    show = ["Keep", "Save_It", "Label", "Symbol", "Reason", "Rank", "Scan_Count", "_chg_percentclose",
            "_vol_ratio", "Adr", "_rel_tightness_today", "_rel_wk_dist", "_10madist", "_20madist",
            "_avgvol_mln", "Avg_RS", "Sector", "On_List", "Saved"]
    view = lab[lab["Label"] != "SCANNED"][[c for c in show if c in lab.columns]].copy()
    edited = st.data_editor(
        view, hide_index=True, use_container_width=True, height=520, key="bt_editor",
        disabled=[c for c in view.columns if c not in ("Keep", "Save_It")],
        column_config={
            "Keep": st.column_config.CheckboxColumn("Tomorrow", help="On tomorrow's list"),
            "Save_It": st.column_config.CheckboxColumn("Save", help="Add to saved watchlist"),
            "_chg_percentclose": st.column_config.NumberColumn("Chg %", format="%.1f"),
            "_vol_ratio": st.column_config.NumberColumn("Vol x", format="%.1f"),
            "_rel_tightness_today": st.column_config.NumberColumn("Rel tight", format="%.2f"),
            "_rel_wk_dist": st.column_config.NumberColumn("ADRs from 10w", format="%.1f"),
            "_avgvol_mln": st.column_config.NumberColumn("Avg val cr", format="%.0f"),
        })
    # write edits back
    lab = lab.set_index("Symbol", drop=False)
    e = edited.set_index("Symbol")
    lab.loc[e.index, "Keep"] = e["Keep"].astype(bool)
    lab.loc[e.index, "Save_It"] = e["Save_It"].astype(bool)
    lab = lab.reset_index(drop=True)

    removes = [u for u in saved_updates if u["action"] == "remove"]
    if removes:
        ok = st.multiselect("Remove these from saved watchlist on save:",
                            [u["symbol"] for u in removes], default=[u["symbol"] for u in removes], key="bt_rm")
        for u in removes:
            u["confirmed"] = u["symbol"] in ok

    tomorrow = lab[lab["Keep"] == True]["Symbol"].tolist()  # noqa: E712
    st.markdown(f"**Tomorrow's list — {len(tomorrow)} symbols**")
    if tomorrow:
        st.code(",".join(f"nse:{s}" for s in tomorrow), language=None)
    if len(tomorrow) > 15:
        st.warning("More than 15 names — the plan is the best 10 or so.")

    if st.button(f"Save tomorrow's plan ({snap_date})", type="primary", key="bt_save"):
        try:
            n, a, r = save_plan(supabase, lab, saved_updates, snap_date)
            st.success(f"Saved snapshot of {n} stocks. Added {a} to watchlist, removed {r}.")
        except Exception as ex:
            st.error(f"Save failed: {ex}")

    # History
    st.markdown("---"); st.markdown("#### Watchlist history")
    try:
        dates = sorted({x["snap_date"] for x in supabase.table("daily_snapshots").select("snap_date")
                        .eq("user_id", USER_ID).order("snap_date", desc=True).limit(5000).execute().data}, reverse=True)
    except Exception:
        dates = []
    if dates:
        pick = st.selectbox("Snapshot date", dates, key="bt_hist")
        h = supabase.table("daily_snapshots").select("symbol,list,selected,scan_count,metrics") \
            .eq("user_id", USER_ID).eq("snap_date", pick).neq("list", "SCANNED").execute().data
        if h:
            hd = pd.DataFrame(h)
            if "metrics" in hd.columns:
                hd["reason"] = hd["metrics"].apply(lambda m: (m or {}).get("reason", "") if isinstance(m, dict) else "")
                hd = hd.drop(columns="metrics")
            st.dataframe(hd.sort_values("list"), hide_index=True, use_container_width=True)
    else:
        st.caption("No snapshots yet.")
