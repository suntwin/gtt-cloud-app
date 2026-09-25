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

PROCESS_VERSION = "v2026-09-25f · one watchlist entry per stock"   # shown on the page so you can tell which code is running
SETUP_TYPES = ["EP", "TIGHT_BO", "WEMA_BO", "CONTINUATION"]
SETUP_NAMES = {"EP": "Episodic pivot", "TIGHT_BO": "Tight-range breakout", "WEMA_BO": "10-week EMA breakout",
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
    "min_scan_count": 2, "min_adr": 3.0, "min_liq": 10.0, "max_reltight": 0.8, "max_rwd": 1.5,
    "min_wktight": 2, "min_chg": -1.0, "max_chg": 3.0, "list_size": 10,
    "bo_min_chg": 2.0, "bo_min_vol": 1.5, "gap_max_adr": 2.0,
    "pullback_band": 2.0, "stale_days": 20,
}
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

LIST_LABELS = ["ADDED", "1_BUY_SIGNAL", "2_SAVE", "3_WAIT", "4_RESETUP", "5_REMOVE", "CANDIDATE", "CHECK", "SCANNED"]
LABEL_NAMES = {"ADDED": "Added by you", "1_BUY_SIGNAL": "1 · Buy signal", "2_SAVE": "2 · Save (tag it)", "3_WAIT": "3 · Wait",
               "4_RESETUP": "4 · Re-setup", "5_REMOVE": "5 · Remove", "CANDIDATE": "New candidate",
               "CHECK": "Check chart"}
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
    d = add_derived(scan_df).drop_duplicates("Symbol").set_index("Symbol", drop=False)

    # one entry per symbol (a stock can sit under two tags; use the most recent trigger)
    watch = {}
    for r in sorted(watch_rows or [], key=lambda r: str(r.get("trigger_date") or r.get("added_date") or "")):
        watch[r["symbol"]] = r
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
            d.at[sym, "Label"], d.at[sym, "Reason"] = "4_RESETUP", f"{'+'.join(tags_of(row))}: pulled back to {which} MA ({min(d10, d20):.1f}% away)"
        elif rt <= cfg["max_reltight"] and near_bo:
            d.at[sym, "Label"], d.at[sym, "Reason"] = "4_RESETUP", f"Continuation setup ({'+'.join(tags_of(row))}): tight near breakout level (rel tight {rt:.2f})"
            if "CONTINUATION" not in all_tags.get(sym, []):
                d.at[sym, "CONT"] = True
        else:
            age = _age_days(row, today)
            if age is not None and age >= cfg["stale_days"]:
                d.at[sym, "Label"], d.at[sym, "Reason"] = "5_REMOVE", f"Stale: {age} days, no re-setup"
                updates.append({"id": wid, "symbol": sym, "action": "remove", "reason": "stale"})
                continue
            d.at[sym, "Reason"] = f"{'+'.join(tags_of(row))}: saved, not set up yet"
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
                        "Saved": row is not None, "Tag": " + ".join(tags_of(row)) if row else "", "Scan_Count": 0})
    if missing:
        d = pd.concat([d, pd.DataFrame(missing).set_index("Symbol", drop=False)])

    d["CONT"] = d["CONT"].fillna(False).astype(bool)
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
    bo = d[(d["_chg_percentclose"].fillna(0) >= cfg["bo_min_chg"]) & (d["_vol_ratio"].fillna(0) >= cfg["bo_min_vol"])].copy()
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
    bo["Skip"] = False
    bo["Batch"] = np.where(bo["_vol_ratio"].fillna(0) >= cfg["strong_min_vol"], "Strong", "Moderate")
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


def render_tomorrow_panel(st, sb, base_df, saved_prefs, mcfg, shared=None):
    """Anticipation mode: evening, once."""
    st.markdown("---")
    st.subheader("Tomorrow's list  ·  evening, once")
    if sb is None:
        st.error("Database not connected."); return
    sd = _header_time(st, mcfg)
    shared = shared or {}
    own = {k: v for k, v in TOMORROW_DEFAULTS.items() if k not in shared}   # the rest come from the sidebar
    cfg = {**_rules_editor(st, "More list rules (scan count, liquidity, list size, breakouts, re-setups)", own,
                           saved_prefs.get("build_tomorrow"), "bt_cfg"), **shared}
    if shared:
        st.caption("From the sidebar's Coiled Setup Filters: " + ", ".join(f"{k} {v:g}" for k, v in shared.items()))
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

    show = ["Keep", "CONT", "Label", "Symbol", "Reason", "Tag", "Rank", "Scan_Count", "_chg_percentclose", "_vol_ratio",
            "Adr", "_rel_tightness_today", "_rel_wk_dist", "_10madist", "_20madist", "_avgvol_mln", "Avg_RS", "Sector"]
    view = lab[lab["Label"] != "SCANNED"][[c for c in show if c in lab.columns]]
    edited = st.data_editor(
        view, hide_index=True, height=480, key="bt_editor",
        disabled=[c for c in view.columns if c not in ("Keep", "CONT")],
        column_config={
            "Keep": st.column_config.CheckboxColumn("Tomorrow", help="On tomorrow's list"),
            "CONT": st.column_config.CheckboxColumn("Save CONT", help="Also save to the watchlist as CONTINUATION "
                                                    "(tight around the breakout candle)"),
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
    lab.loc[edited["Symbol"], "CONT"] = edited["CONT"].fillna(False).astype(bool).values
    lab = lab.reset_index(drop=True)

    removes = [u for u in updates if u["action"] == "remove"]
    if removes:
        untagged = {r["symbol"] for r in watch if "UNTAGGED" in tags_of(r)}
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
            cont = lab[lab["CONT"] == True]["Symbol"].tolist()  # noqa: E712
            c_added = save_symbols_to_watchlist(sb, cont, "CONTINUATION", base_df, sd, mcfg, "ANTICIPATION")[0] if cont else 0
            st.success(f"Saved {n} names for tomorrow and a snapshot of {len(lab)} stocks. "
                       f"Saved {c_added} as CONTINUATION. Removed {r} saved breakouts.")
        except Exception as ex:
            st.error(f"Save failed: {ex}")


def render_breakout_panel(st, sb, base_df, saved_prefs, mcfg, shared=None):
    """Post Breakout mode: once, late in the session or after the close."""
    st.markdown("---")
    st.subheader("Tag breakouts  ·  once a day")
    st.caption(f"gtt_process {PROCESS_VERSION}")
    if sb is None:
        st.error("Database not connected."); return
    sd = _header_time(st, mcfg)
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

    show = ["Skip"] + SETUP_TYPES + ["Rating", "Symbol", "Suggested", "Why", "In_Watchlist", "On_List", "_chg_percentclose", "_vol_ratio",
                          "Adr", "_rel_tightness_prev", "_rel_wk_dist", "_10madist", "_20madist", "Scan_Count", "Avg_RS", "Sector"]
    view = part[[c for c in show if c in part.columns]].reset_index(drop=True)
    ekey = f"bo_editor_{pick}"
    edited = st.data_editor(
        view, hide_index=True, height=440, key=ekey,
        disabled=[c for c in view.columns if c not in ["Skip"] + SETUP_TYPES + ["Rating"]],
        column_config={
            "Skip": st.column_config.CheckboxColumn("Skip", help="Don't save this stock, whatever is ticked"),
            "CONTINUATION": st.column_config.CheckboxColumn("CONT", help="Continuation — second leg after an earlier breakout"),
            "Rating": st.column_config.SelectboxColumn("Rating", options=RATINGS, required=True,
                                                       help="Your grade after the chart check: 3★, 4★, 5★"),
            "EP": st.column_config.CheckboxColumn("EP", help="Episodic pivot"),
            "TIGHT_BO": st.column_config.CheckboxColumn("TIGHT_BO", help="Breakout from a tight range, 10 & 20 rising"),
            "WEMA_BO": st.column_config.CheckboxColumn("WEMA_BO", help="Breakout from the 10-week EMA"),
            "Suggested": st.column_config.TextColumn("My suggestion"),
            "Why": st.column_config.TextColumn("Why", width="large"),
            "In_Watchlist": st.column_config.TextColumn("Already saved as"),
            "On_List": st.column_config.CheckboxColumn("Was on list"),
            "_chg_percentclose": st.column_config.NumberColumn("Chg %", format="%.1f"),
            "_vol_ratio": st.column_config.NumberColumn("Vol x", format="%.1f"),
            "_rel_tightness_prev": st.column_config.NumberColumn("Rel tight (prev)", format="%.2f"),
            "_rel_wk_dist": st.column_config.NumberColumn("ADRs from 10w", format="%.1f"),
            "_10madist": st.column_config.NumberColumn("10MA %", format="%.1f"),
            "_20madist": st.column_config.NumberColumn("20MA %", format="%.1f"),
        })
    part = part.reset_index(drop=True)
    for t in SETUP_TYPES:
        part[t] = edited[t].fillna(False).astype(bool).values
    part["Rating"] = edited["Rating"].fillna("—").values
    part["Skip"] = edited["Skip"].fillna(False).astype(bool).values
    live = part[~part["Skip"]]
    counts = {t: int(live[t].sum()) for t in SETUP_TYPES}
    n = sum(counts.values())
    st.caption("To save: " + ", ".join(f"{t} {c}" for t, c in counts.items()) +
               f" · {int(live[SETUP_TYPES].any(axis=1).sum())} stocks · {int(part['Skip'].sum())} skipped")
    if st.button(f"Save {pick.lower()} batch to watchlist ({n} tags)  ·  {sd}", type="primary", key=f"bo_save_{pick}"):
        try:
            a, t = save_breakouts(sb, part, sd, mcfg)
            st.session_state["bo_msg"] = f"{pick} batch: saved {a} tags to the watchlist; {t} breakouts recorded."
            st.session_state.pop(ekey, None)
            st.rerun()
        except Exception as ex:
            st.error(f"Save failed: {ex}")

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
            st.caption(f"Tomorrow's list saved on {tdate or '—'} — tight names plus re-setups.")
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
        v = wdf[["id", "symbol", "Rating", "trigger_date", "last_trigger_date", "trigger_close", "prev_close", "vol_ratio",
                 "last_seen", "last_status", "tags"]].copy()
        for t in SETUP_TYPES:
            v.insert(2 + SETUP_TYPES.index(t), t, wdf["_tags"].map(lambda ts, t=t: t in ts).values)
        v.insert(2, "UNTAGGED", wdf["_tags"].map(lambda ts: "UNTAGGED" in ts).values)
        v["age"] = v["last_trigger_date"].fillna(v["trigger_date"]).fillna(wdf["added_date"]).map(
            lambda s: (date.today() - pd.to_datetime(s).date()).days if s else None)
        if live is not None:
            for c, src in [("Last", "Last"), ("Chg %", "_chg_percentclose"), ("Vol x", "_vol_ratio"),
                           ("10MA %", "_10madist"), ("20MA %", "_20madist")]:
                v[c] = v["symbol"].map(live[src]) if src in live.columns else np.nan
            v["In scan"] = v["symbol"].isin(live.index)
        v["tags"] = v["tags"].map(lambda t: ", ".join(t) if isinstance(t, list) else "")
        v.insert(0, "Action", "keep")
        v = v.assign(_r=wdf["rating"].fillna(0).values).sort_values(["_r", "symbol"], ascending=[False, True]).drop(columns="_r")
        if not v["UNTAGGED"].any():
            v = v.drop(columns="UNTAGGED")
        editable = ["Action", "Rating"] + SETUP_TYPES
        ed = st.data_editor(
            v, hide_index=True, height=420, key="wl_editor",
            disabled=[c for c in v.columns if c not in editable],
            column_config={
                "id": None,
                "Action": st.column_config.SelectboxColumn("Action", options=["keep", "traded", "remove"], required=True),
                "UNTAGGED": st.column_config.CheckboxColumn("Untagged", help="Old saved stock — tick its real tag(s)"),
                "CONTINUATION": st.column_config.CheckboxColumn("CONT"),
                "last_trigger_date": "Latest BO",
                "Rating": st.column_config.SelectboxColumn("Rating", options=RATINGS, required=True),
                "trigger_date": "Breakout day", "trigger_close": "BO close", "prev_close": "Fail level",
                "vol_ratio": st.column_config.NumberColumn("BO vol x", format="%.1f"),
            })
        changes = []
        orig = v.set_index("id")
        tagset = lambda r: [t for t in SETUP_TYPES if bool(r[t])]
        for _, r in ed.iterrows():
            o = orig.loc[r["id"]]
            if r["Action"] != "keep" or tagset(r) != tagset(o) or r["Rating"] != o["Rating"]:
                changes.append(r)
        st.caption("One row per stock. Tick/untick its tags, set the rating, or mark it traded / remove, then Apply.")
        if st.button(f"Apply changes ({len(changes)})", key="wl_apply", disabled=not changes):
            errs = []
            for r in changes:
                try:
                    if r["Action"] in ("traded", "remove"):
                        sb.rpc("remove_from_watchlist", {"p_id": int(r["id"]), "p_reason": "manual" if r["Action"] == "remove" else "traded",
                                                         "p_status": "traded" if r["Action"] == "traded" else "removed"}).execute()
                    if r["Action"] == "keep" and r["Rating"] != orig.loc[r["id"], "Rating"]:
                        sb.table("watchlist").update({"rating": rating_int(r["Rating"])}).eq("id", int(r["id"])).execute()
                    new_t, old_t = tagset(r), tagset(orig.loc[r["id"]])
                    if r["Action"] == "keep" and new_t != old_t:
                        if not new_t:
                            raise ValueError("no tag ticked — tick at least one, or set Action to remove")
                        sb.table("watchlist").update({"setup_types": new_t}).eq("id", int(r["id"])).execute()
                        sb.table("watchlist_events").insert({"watchlist_id": int(r["id"]), "event": "retagged",
                                                             "detail": f"{'+'.join(old_t) or 'UNTAGGED'} → {'+'.join(new_t)}"}).execute()
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
            if only != "All" and only not in tags_of(r):
                continue
            a = _age_days(r, date.today())
            ls = r.get("last_seen")
            un = (date.today() - pd.to_datetime(ls).date()).days if ls else None
            if r.get("expires_on") and pd.to_datetime(r["expires_on"]).date() < date.today():
                prev.append((r["symbol"], " + ".join(tags_of(r)), "expired"))
            elif a is not None and a > age:
                prev.append((r["symbol"], " + ".join(tags_of(r)), f"stale ({a} days)"))
            elif un is not None and un > unseen:
                prev.append((r["symbol"], " + ".join(tags_of(r)), f"unseen ({un} days)"))
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
