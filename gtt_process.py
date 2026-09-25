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
import numpy as np
import pandas as pd

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


# ════════════════════════════════════════════════════════════════════════════
# Market settings — add a USA entry's scanner page later with MARKETS["USA"]
# ════════════════════════════════════════════════════════════════════════════
MARKETS = {
    "NSE": {"market": "NSE", "exchange": "NSE", "user_id": "nse_user", "tz": "Asia/Kolkata",
            "open": (9, 15), "close": (15, 30), "local_hint": "8:00 PM Sydney"},
    "USA": {"market": "USA", "exchange": "NASDAQ", "user_id": "usa_user", "tz": "America/New_York",
            "open": (9, 30), "close": (16, 0), "local_hint": "6:00 AM Sydney"},
}

SETUP_TYPES = ["EP", "TIGHT_BO", "WEMA_BO"]
SETUP_NAMES = {"EP": "Episodic pivot", "TIGHT_BO": "Tight-range breakout", "WEMA_BO": "10-week EMA breakout",
               "UNTAGGED": "Untagged (old saved list)"}
NO_TAG = "—"

# Anticipation / tomorrow's list rules (fixed for 8 weeks)
TOMORROW_DEFAULTS = {
    "min_scan_count": 2, "min_adr": 3.0, "min_liq": 10.0, "max_reltight": 0.8, "max_rwd": 1.5,
    "min_wktight": 2, "min_chg": -1.0, "max_chg": 3.0, "list_size": 10,
    "bo_min_chg": 2.0, "bo_min_vol": 1.5, "gap_max_adr": 2.0,
    "pullback_band": 2.0, "stale_days": 20,
}
# Post-breakout tagging rules
BREAKOUT_DEFAULTS = {
    "bo_min_chg": 2.0, "bo_min_vol": 1.5,   # what counts as a breakout
    "save_min_vol": 2.0,                    # pre-fill a tag only at this volume or more
    "ep_min_chg": 8.0, "ep_min_vol": 3.0,   # EP: big move on huge volume
    "tight_max_reltight": 1.0,              # TIGHT_BO: yesterday's NR4 / ADR
    "wema_max_adr": 2.0,                    # WEMA_BO: within N ADRs of the 10w EMA
}
CLEANUP_DEFAULTS = {"max_age_days": 30, "unseen_days": 10}

LIST_LABELS = ["ADDED", "1_BUY_SIGNAL", "2_SAVE", "3_WAIT", "4_RESETUP", "5_REMOVE", "CANDIDATE", "CHECK", "SCANNED"]
LABEL_NAMES = {"ADDED": "Added by you", "1_BUY_SIGNAL": "1 · Buy signal", "2_SAVE": "2 · Save (tag it)", "3_WAIT": "3 · Wait",
               "4_RESETUP": "4 · Re-setup", "5_REMOVE": "5 · Remove", "CANDIDATE": "New candidate",
               "CHECK": "Check chart"}
TOMORROW_LABELS = {"3_WAIT", "4_RESETUP"}   # always carried onto tomorrow's list

SNAPSHOT_METRICS = ["Last", "_chg_percentclose", "_vol_ratio", "Adr", "_nr4", "_nr4_previous",
                    "_rel_tightness_today", "_rel_tightness_prev", "_rel_wk_dist", "W_Dist10wMA",
                    "W_TightCloses_10w", "_10madist", "_20madist", "_avgvol_mln", "Avg_RS",
                    "RS_1M", "RS_3M", "RS_6M", "Sector", "Suggested"]


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


def tv_symbol(sym, mcfg, exchange=None):
    return f"{exchange or mcfg['exchange']}:{sym}"


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
    return d


def _age_days(row, today):
    s = row.get("trigger_date") or row.get("added_date")
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
    d = add_derived(scan_df).drop_duplicates("Symbol").set_index("Symbol", drop=False)

    # one entry per symbol (a stock can sit under two tags; use the most recent trigger)
    watch = {}
    for r in sorted(watch_rows or [], key=lambda r: str(r.get("trigger_date") or r.get("added_date") or "")):
        watch[r["symbol"]] = r
    d["On_List"] = d["Symbol"].isin(yesterday_list)
    d["Saved"] = d["Symbol"].isin(watch.keys())
    d["Tag"] = d["Symbol"].map(lambda s: watch[s]["setup_type"] if s in watch else "")
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

    c1, v1 = chg.round(1).astype(str), vol.round(1).astype(str)
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
        last, prev_close, bo_close = _num(r["Last"]), _num(row.get("prev_close")), _num(row.get("trigger_close"))
        radr = _num(r["Adr"], 0)
        d10, d20 = abs(_num(r["_10madist"], 99)), abs(_num(r["_20madist"], 99))
        rt = _num(r["_rel_tightness_today"], 99)
        if np.isfinite(prev_close) and np.isfinite(last) and last < prev_close:
            d.at[sym, "Label"], d.at[sym, "Reason"] = "5_REMOVE", f"Failed: closed {last:g} below pre-breakout close {prev_close:g}"
            updates.append({"id": wid, "symbol": sym, "action": "remove", "reason": "failed"})
            continue
        near_bo = (not np.isfinite(bo_close)) or (
            np.isfinite(last) and bo_close * (1 - radr / 100) <= last <= bo_close * (1 + 2 * radr / 100))
        if min(d10, d20) <= cfg["pullback_band"]:
            which = "10" if d10 <= d20 else "20"
            d.at[sym, "Label"], d.at[sym, "Reason"] = "4_RESETUP", f"{row['setup_type']}: pulled back to {which} MA ({min(d10, d20):.1f}% away)"
        elif rt <= cfg["max_reltight"] and near_bo:
            d.at[sym, "Label"], d.at[sym, "Reason"] = "4_RESETUP", f"{row['setup_type']}: quiet near breakout level (rel tight {rt:.2f})"
        else:
            age = _age_days(row, today)
            if age is not None and age >= cfg["stale_days"]:
                d.at[sym, "Label"], d.at[sym, "Reason"] = "5_REMOVE", f"Stale: {age} days, no re-setup"
                updates.append({"id": wid, "symbol": sym, "action": "remove", "reason": "stale"})
                continue
            d.at[sym, "Reason"] = f"{row['setup_type']}: saved, not set up yet"
        updates.append({"id": wid, "symbol": sym, "action": "seen"})

    # New tight candidates
    cand = ((d["Label"] == "SCANNED") & (d["Scan_Count"] >= cfg["min_scan_count"]) & (adr >= cfg["min_adr"])
            & (d["_avgvol_mln"].fillna(0) >= cfg["min_liq"])
            & (d["_rel_tightness_today"].fillna(99) <= cfg["max_reltight"])
            & chg.between(cfg["min_chg"], cfg["max_chg"]))
    weekly_ok = (d["_rel_wk_dist"] <= cfg["max_rwd"]) & (d["W_TightCloses_10w"] >= cfg["min_wktight"])
    put(cand & weekly_ok, "CANDIDATE",
        "Scan " + d["Scan_Count"].astype(str) + "/3, rel tight " + d["_rel_tightness_today"].round(2).astype(str))
    put(cand & d["Missing_Weekly"], "CHECK", "Passes daily rules, weekly data missing — check chart")
    d.loc[d["On_List"] & (d["Label"] == "SCANNED"), "Reason"] = "Was on list, no longer qualifies"

    # Listed / saved stocks missing from today's scan — never silently disappear
    missing = []
    for sym in sorted(set(yesterday_list) | set(watch.keys())):
        if sym in d.index:
            continue
        row = watch.get(sym)
        age = _age_days(row, today) if row else None
        if row and age is not None and age >= cfg["stale_days"]:
            label, reason = "5_REMOVE", f"Not in scan, {age} days old — suggest remove"
            updates.append({"id": row.get("id"), "symbol": sym, "action": "remove", "reason": "stale"})
        else:
            label, reason = "CHECK", "Not in today's scan — check chart"
        missing.append({"Symbol": sym, "Label": label, "Reason": reason, "On_List": sym in yesterday_list,
                        "Saved": row is not None, "Tag": row["setup_type"] if row else "", "Scan_Count": 0})
    if missing:
        d = pd.concat([d, pd.DataFrame(missing).set_index("Symbol", drop=False)])

    d["Rank"] = np.nan
    cm = d["Label"] == "CANDIDATE"
    ranked = d[cm].sort_values(["Scan_Count", "_rel_tightness_today", "Avg_RS"],
                               ascending=[False, True, False], na_position="last")
    d.loc[ranked.index, "Rank"] = range(1, len(ranked) + 1)
    d["Keep"] = d["Label"].isin(TOMORROW_LABELS) | (cm & (d["Rank"] <= cfg["list_size"]))
    order = {k: i for i, k in enumerate(LIST_LABELS)}
    d["_o"] = d["Label"].map(order)
    d = d.sort_values(["_o", "Rank"], na_position="last").drop(columns="_o").reset_index(drop=True)
    return d, updates


def suggest_tag(r, cfg):
    """Best-guess setup type from scan columns. Confirm on the chart."""
    chg, vol, adr = _num(r.get("_chg_percentclose"), 0), _num(r.get("_vol_ratio"), 0), _num(r.get("Adr"), 0)
    if chg >= cfg["ep_min_chg"] and vol >= cfg["ep_min_vol"]:
        return "EP", f"+{chg:.1f}% on {vol:.1f}x vol"
    rtp = _num(r.get("_rel_tightness_prev"), 99)
    d10, d20 = _num(r.get("_10madist"), -1), _num(r.get("_20madist"), -1)
    if rtp <= cfg["tight_max_reltight"] and d10 > 0 and d20 > 0:
        return "TIGHT_BO", f"tight before (rel {rtp:.2f}), above 10 & 20 MA — confirm both rising"
    rwd = _num(r.get("_rel_wk_dist"), 99)
    if rwd <= cfg["wema_max_adr"]:
        return "WEMA_BO", f"{rwd:.1f} ADR from 10w EMA"
    return NO_TAG, "no clear setup"


def find_breakouts(scan_df, yesterday_list, watch_rows, cfg=None):
    """Post Breakout: today's breakouts with a suggested tag. Pre-fills Tag only for strong volume."""
    cfg = {**BREAKOUT_DEFAULTS, **(cfg or {})}
    d = add_derived(scan_df).drop_duplicates("Symbol")
    bo = d[(d["_chg_percentclose"].fillna(0) >= cfg["bo_min_chg"]) & (d["_vol_ratio"].fillna(0) >= cfg["bo_min_vol"])].copy()
    active = {}
    for r in watch_rows or []:
        active.setdefault(r["symbol"], set()).add(r["setup_type"])
    sug = bo.apply(lambda r: suggest_tag(r, cfg), axis=1)
    bo["Suggested"] = [s[0] for s in sug]
    bo["Why"] = [s[1] for s in sug]
    bo["On_List"] = bo["Symbol"].isin(yesterday_list)
    bo["In_Watchlist"] = bo["Symbol"].map(lambda s: ", ".join(sorted(active.get(s, []))))
    strong = bo["_vol_ratio"].fillna(0) >= cfg["save_min_vol"]
    already = bo["Symbol"].isin(active.keys())   # saved already (any tag) → don't pre-fill again
    bo["Tag"] = np.where(strong & ~already & (bo["Suggested"] != NO_TAG), bo["Suggested"], NO_TAG)
    bo["Missed"] = bo["On_List"]   # on the list and broke out: tag it only if you missed the entry
    return bo.sort_values("_vol_ratio", ascending=False).reset_index(drop=True)


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
         "why": r.get("Why")}
    return {k: v for k, v in p.items() if v is not None}


# ════════════════════════════════════════════════════════════════════════════
# Database
# ════════════════════════════════════════════════════════════════════════════
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
    rows = (sb.table("daily_snapshots").select("symbol").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
            .eq("scanner", scanner).eq("snap_date", last).eq("selected", True).execute().data)
    return last, sorted({x["symbol"] for x in rows})


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
    already = bo.apply(lambda r: r["Tag"] in str(r.get("In_Watchlist", "")).split(", "), axis=1)
    tagged = bo[bo["Tag"].isin(SETUP_TYPES) & ~already]
    for _, r in tagged.iterrows():
        tags = ["missed"] if bool(r.get("On_List")) else ["off-list"]
        sb.rpc("add_to_watchlist", {"p_user": mcfg["user_id"], "p_market": mcfg["market"], "p_symbol": r["Symbol"],
                                    "p_setup_type": r["Tag"], "p_source": "BREAKOUT",
                                    "p_trigger_date": snap_date.isoformat(), "p_data": breakout_payload(r, mcfg),
                                    "p_tags": tags}).execute()
    snap = bo.copy()
    prior = snap["In_Watchlist"].fillna("").str.split(", ").str[0] if "In_Watchlist" in snap else ""
    snap["_list"] = np.where(snap["Tag"].isin(SETUP_TYPES), snap["Tag"], np.where(prior != "", prior, "NOT_SAVED"))
    snap["_sel"] = snap["_list"] != "NOT_SAVED"
    upsert_snapshots(sb, snapshot_rows(snap, snap_date, mcfg, "BREAKOUT", "_list", "_sel"))
    return len(tagged), len(bo)


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
    active = {(r["symbol"], r["setup_type"]) for r in load_active_watchlist(sb, mcfg)}
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
        sd = session_date(market_now(mcfg), mcfg)
        order = (["TOMORROW"] + SETUP_TYPES) if scan_mode == "Anticipation" else (SETUP_TYPES + ["TOMORROW"])
        labels = {"TOMORROW": "Add to tomorrow's list", "EP": "Save as EP", "TIGHT_BO": "Save as TIGHT_BO",
                  "WEMA_BO": "Save as WEMA_BO"}
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
def _header_time(st, mcfg):
    now = market_now(mcfg)
    sd = session_date(now, mcfg)
    if market_is_open(now, mcfg):
        st.warning(f"{mcfg['market']} is open ({now:%H:%M}). Numbers are intraday — the routine runs after the close "
                   f"({mcfg['close'][0]}:{mcfg['close'][1]:02d} local, {mcfg['local_hint']}).")
    return sd


def _rules_editor(st, title, defaults, saved, key):
    cfg = {**defaults, **(saved or {})}
    with st.container(border=True):
        show = st.toggle(title, value=False, key=f"{key}_show")
        cols = st.columns(3) if show else []
        for i, (k, v) in enumerate(defaults.items() if show else []):
            with cols[i % 3]:
                if isinstance(v, int) and not isinstance(v, bool):
                    cfg[k] = int(st.number_input(k, value=int(cfg[k]), step=1, key=f"{key}_{k}"))
                else:
                    cfg[k] = float(st.number_input(k, value=float(cfg[k]), step=0.5, key=f"{key}_{k}"))
        if show:
            st.caption("Saved with 'Save filter settings'. Don't change these during the 8 weeks.")
    st.session_state[key] = cfg
    return cfg


def render_tomorrow_panel(st, sb, base_df, saved_prefs, mcfg):
    """Anticipation mode: evening, once."""
    st.markdown("---")
    st.subheader("Tomorrow's list  ·  evening, once")
    if sb is None:
        st.error("Database not connected."); return
    sd = _header_time(st, mcfg)
    cfg = _rules_editor(st, "Tomorrow's list rules", TOMORROW_DEFAULTS, saved_prefs.get("build_tomorrow"), "bt_cfg")
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
    counts = lab["Label"].value_counts()
    for c, (k, n) in zip(st.columns(len(LABEL_NAMES)), LABEL_NAMES.items()):
        c.metric(n, int(counts.get(k, 0)))

    show = ["Keep", "Label", "Symbol", "Reason", "Tag", "Rank", "Scan_Count", "_chg_percentclose", "_vol_ratio",
            "Adr", "_rel_tightness_today", "_rel_wk_dist", "_10madist", "_20madist", "_avgvol_mln", "Avg_RS", "Sector"]
    view = lab[lab["Label"] != "SCANNED"][[c for c in show if c in lab.columns]]
    edited = st.data_editor(
        view, hide_index=True, height=480, key="bt_editor",
        disabled=[c for c in view.columns if c != "Keep"],
        column_config={
            "Keep": st.column_config.CheckboxColumn("Tomorrow", help="On tomorrow's list"),
            "Label": st.column_config.TextColumn("Situation"),
            "Tag": st.column_config.TextColumn("Saved as"),
            "_chg_percentclose": st.column_config.NumberColumn("Chg %", format="%.1f"),
            "_vol_ratio": st.column_config.NumberColumn("Vol x", format="%.1f"),
            "_rel_tightness_today": st.column_config.NumberColumn("Rel tight", format="%.2f"),
            "_rel_wk_dist": st.column_config.NumberColumn("ADRs from 10w", format="%.1f"),
            "_avgvol_mln": st.column_config.NumberColumn("Avg value", format="%.0f"),
        })
    lab = lab.set_index("Symbol", drop=False)
    lab.loc[edited["Symbol"], "Keep"] = edited["Keep"].astype(bool).values
    lab = lab.reset_index(drop=True)

    removes = [u for u in updates if u["action"] == "remove"]
    if removes:
        untagged = {r["symbol"] for r in watch if r.get("setup_type") == "UNTAGGED"}
        ok = st.multiselect("Remove these saved breakouts when you save:", [u["symbol"] for u in removes],
                            default=[u["symbol"] for u in removes if u["symbol"] not in untagged], key="bt_rm",
                            help="Old UNTAGGED stocks aren't pre-selected — retag or remove them in the Watchlist tab.")
        for u in removes:
            u["confirmed"] = u["symbol"] in ok

    keep = lab[lab["Keep"] == True]["Symbol"].tolist()  # noqa: E712
    st.markdown(f"**Tomorrow's list — {len(keep)} names**")
    if keep:
        st.code(",".join(tv_symbol(s, mcfg) for s in keep), language=None)
    if len(keep) > 15:
        st.warning("More than 15 names. The plan is the best 10 or so.")
    if st.button(f"Save tomorrow's list  ({sd})", type="primary", key="bt_save"):
        try:
            n, r = save_tomorrow(sb, lab, updates, sd, mcfg)
            st.success(f"Saved {n} names for tomorrow and a snapshot of {len(lab)} stocks. Removed {r} saved breakouts.")
        except Exception as ex:
            st.error(f"Save failed: {ex}")


def render_breakout_panel(st, sb, base_df, saved_prefs, mcfg):
    """Post Breakout mode: once, late in the session or after the close."""
    st.markdown("---")
    st.subheader("Tag breakouts  ·  once a day")
    if sb is None:
        st.error("Database not connected."); return
    sd = _header_time(st, mcfg)
    cfg = _rules_editor(st, "Breakout tagging rules", BREAKOUT_DEFAULTS, saved_prefs.get("breakout_tags"), "bo_cfg")
    try:
        _, ylist = load_last_list(sb, mcfg, before_date=sd)   # the list you traded this session
        watch = load_active_watchlist(sb, mcfg)
    except Exception as e:
        st.error(f"Could not read the database. Did you run supabase_migration.sql? ({e})"); return
    on_list = set(ylist)
    bo = find_breakouts(base_df, on_list, watch, cfg)
    st.caption(f"Session **{sd}** · {len(bo)} breakouts (≥{cfg['bo_min_chg']}% on ≥{cfg['bo_min_vol']}x vol). "
               f"Tags are pre-filled at ≥{cfg['save_min_vol']}x volume — change or clear them after checking the chart.")
    if bo.empty:
        st.info("No breakouts today."); return
    show = ["Tag", "Symbol", "Suggested", "Why", "On_List", "In_Watchlist", "_chg_percentclose", "_vol_ratio", "Adr",
            "_rel_tightness_prev", "_rel_wk_dist", "_10madist", "_20madist", "Scan_Count", "Avg_RS", "Sector"]
    view = bo[[c for c in show if c in bo.columns]]
    edited = st.data_editor(
        view, hide_index=True, height=440, key="bo_editor",
        disabled=[c for c in view.columns if c != "Tag"],
        column_config={
            "Tag": st.column_config.SelectboxColumn("Tag", options=[NO_TAG] + SETUP_TYPES, required=True,
                                                    help="Blank = don't save"),
            "On_List": st.column_config.CheckboxColumn("Was on list"),
            "In_Watchlist": st.column_config.TextColumn("Already saved as"),
            "_chg_percentclose": st.column_config.NumberColumn("Chg %", format="%.1f"),
            "_vol_ratio": st.column_config.NumberColumn("Vol x", format="%.1f"),
            "_rel_tightness_prev": st.column_config.NumberColumn("Rel tight (prev)", format="%.2f"),
            "_rel_wk_dist": st.column_config.NumberColumn("ADRs from 10w", format="%.1f"),
        })
    bo["Tag"] = edited["Tag"].fillna(NO_TAG).values
    n = int(bo["Tag"].isin(SETUP_TYPES).sum())
    by = bo[bo["Tag"].isin(SETUP_TYPES)].groupby("Tag")["Symbol"].count().to_dict()
    st.caption("To save: " + (", ".join(f"{k} {v}" for k, v in by.items()) if by else "nothing tagged"))
    if st.button(f"Save to watchlist ({n})  ·  {sd}", type="primary", key="bo_save"):
        try:
            a, t = save_breakouts(sb, bo, sd, mcfg)
            st.success(f"Saved {a} to the watchlist. Snapshot of {t} breakouts stored.")
        except Exception as ex:
            st.error(f"Save failed: {ex}")


def render_watchlist_tab(st, sb, base_df, mcfg):
    """Working watchlist: generate lists, update entries, clean up, history."""
    st.subheader(f"Watchlist · {mcfg['market']}")
    if sb is None:
        st.error("Database not connected."); return
    try:
        watch = load_active_watchlist(sb, mcfg)
        tdate, tlist = load_last_list(sb, mcfg)
    except Exception as e:
        st.error(f"Could not read the database. Did you run supabase_migration.sql? ({e})"); return
    wdf = pd.DataFrame(watch)

    # ── 1. Generate lists ──
    st.markdown("#### Generate a list")
    counts = wdf["setup_type"].value_counts().to_dict() if not wdf.empty else {}
    btns = [("TOMORROW", f"Tomorrow's list ({len(tlist)})")] + [(t, f"{t} ({counts.get(t, 0)})") for t in SETUP_TYPES]
    if counts.get("UNTAGGED"):
        btns.append(("UNTAGGED", f"UNTAGGED ({counts['UNTAGGED']})"))
    for c, (k, label) in zip(st.columns(len(btns)), btns):
        if c.button(label, key=f"wl_btn_{k}", use_container_width=True):
            st.session_state["wl_show"] = k
    show = st.session_state.get("wl_show")
    if show:
        if show == "TOMORROW":
            syms, ex = tlist, {}
            st.caption(f"Tomorrow's list saved on {tdate or '—'} — tight names plus re-setups.")
        else:
            part = wdf[wdf["setup_type"] == show] if not wdf.empty else wdf
            syms = sorted(part["symbol"].tolist()) if not part.empty else []
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
        v = wdf[["id", "symbol", "setup_type", "trigger_date", "trigger_close", "prev_close", "vol_ratio", "last_seen",
                 "last_status", "tags"]].copy()
        v["age"] = v["trigger_date"].fillna(wdf["added_date"]).map(
            lambda s: (date.today() - pd.to_datetime(s).date()).days if s else None)
        if live is not None:
            for c, src in [("Last", "Last"), ("Chg %", "_chg_percentclose"), ("Vol x", "_vol_ratio"),
                           ("10MA %", "_10madist"), ("20MA %", "_20madist")]:
                v[c] = v["symbol"].map(live[src]) if src in live.columns else np.nan
            v["In scan"] = v["symbol"].isin(live.index)
        v["tags"] = v["tags"].map(lambda t: ", ".join(t) if isinstance(t, list) else "")
        v.insert(0, "Action", "keep")
        v = v.sort_values(["setup_type", "trigger_date"], ascending=[True, False])
        ed = st.data_editor(
            v, hide_index=True, height=420, key="wl_editor",
            disabled=[c for c in v.columns if c not in ("Action", "setup_type")],
            column_config={
                "id": None,
                "Action": st.column_config.SelectboxColumn("Action", options=["keep", "traded", "remove"], required=True),
                "setup_type": st.column_config.SelectboxColumn("Tag", options=SETUP_TYPES + ["UNTAGGED"], required=True),
                "trigger_date": "Breakout day", "trigger_close": "BO close", "prev_close": "Fail level",
                "vol_ratio": st.column_config.NumberColumn("BO vol x", format="%.1f"),
            })
        changes = []
        orig = v.set_index("id")
        for _, r in ed.iterrows():
            o = orig.loc[r["id"]]
            if r["Action"] != "keep" or r["setup_type"] != o["setup_type"]:
                changes.append(r)
        if st.button(f"Apply changes ({len(changes)})", key="wl_apply", disabled=not changes):
            errs = []
            for r in changes:
                try:
                    if r["Action"] in ("traded", "remove"):
                        sb.rpc("remove_from_watchlist", {"p_id": int(r["id"]), "p_reason": "manual" if r["Action"] == "remove" else "traded",
                                                         "p_status": "traded" if r["Action"] == "traded" else "removed"}).execute()
                    elif r["setup_type"] != orig.loc[r["id"], "setup_type"]:
                        sb.table("watchlist").update({"setup_type": r["setup_type"]}).eq("id", int(r["id"])).execute()
                        sb.table("watchlist_events").insert({"watchlist_id": int(r["id"]), "event": "retagged",
                                                             "detail": f"{orig.loc[r['id'], 'setup_type']} → {r['setup_type']}"}).execute()
                except Exception as ex:
                    errs.append(f"{r['symbol']}: {ex}")
            if errs:
                st.error("; ".join(errs))
            else:
                st.success("Updated."); st.rerun()

    # ── 3. Add manually ──
    with st.container(border=True):
        st.markdown("**Add a stock manually**")
        c1, c2, c3 = st.columns([2, 2, 1])
        sym = c1.text_input("Symbols (comma separated)", key="wl_add_sym")
        tag = c2.selectbox("Save to", ["Tomorrow's list"] + SETUP_TYPES, key="wl_add_tag")
        c3.write(""); c3.write("")
        syms = _parse_symbols(sym)
        if c3.button("Add", key="wl_add_btn", disabled=not syms):
            sd = session_date(market_now(mcfg), mcfg)
            try:
                if tag == "Tomorrow's list":
                    add_to_tomorrow(sb, syms, base_df, sd, mcfg)
                else:
                    save_symbols_to_watchlist(sb, syms, tag, base_df, sd, mcfg, "MANUAL")
                st.success(f"Added {', '.join(syms)} to {tag}."); st.rerun()
            except Exception as ex:
                st.error(f"Save failed: {ex}")

    # ── 4. Clean-up (weekend) ──
    with st.container(border=True):
        st.markdown("**Clean-up** · weekend")
        c1, c2, c3 = st.columns(3)
        age = c1.number_input("Expire if older than (days)", 1, 365, CLEANUP_DEFAULTS["max_age_days"], key="wl_c_age")
        unseen = c2.number_input("…or not in a scan for (days)", 1, 365, CLEANUP_DEFAULTS["unseen_days"], key="wl_c_unseen")
        only = c3.selectbox("Only", ["All"] + SETUP_TYPES + ["UNTAGGED"], key="wl_c_only")
        prev = []
        for r in watch:
            if only != "All" and r["setup_type"] != only:
                continue
            a = _age_days(r, date.today())
            ls = r.get("last_seen")
            un = (date.today() - pd.to_datetime(ls).date()).days if ls else None
            if r.get("expires_on") and pd.to_datetime(r["expires_on"]).date() < date.today():
                prev.append((r["symbol"], r["setup_type"], "expired"))
            elif a is not None and a > age:
                prev.append((r["symbol"], r["setup_type"], f"stale ({a} days)"))
            elif un is not None and un > unseen:
                prev.append((r["symbol"], r["setup_type"], f"unseen ({un} days)"))
        if prev:
            st.dataframe(pd.DataFrame(prev, columns=["Symbol", "Tag", "Reason"]), hide_index=True)
        else:
            st.caption("Nothing to clean up with these settings.")
        if st.button(f"Run clean-up ({len(prev)})", key="wl_c_run", disabled=not prev):
            res = sb.rpc("cleanup_watchlist", {"p_user": mcfg["user_id"], "p_market": mcfg["market"],
                                               "p_max_age_days": int(age), "p_unseen_days": int(unseen),
                                               "p_setup_type": None if only == "All" else only}).execute()
            st.success(f"Expired {len(res.data or [])} entries."); st.rerun()

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
