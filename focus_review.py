"""
Learning loop for Sector Focus — no new features, just evidence for tuning.

Every session Sector Focus saves what it said about each scanned stock (focus_snapshots). The review then takes every
stock in today's scan that BROKE OUT (up ≥ 0.8 ADR and ≥ 3% on ≥ 2x volume) and looks up what we said the session before:
  Held back by rules   → which rules failed (a rule that blocks good breakouts again and again is a tuning candidate)
  Shown, not flagged   → the scanner had it (score ≥ 6 or a ready setup), the choice was yours
  Flagged / Picked     → it was on your list (check the alert / entry)
  Not in the scan      → it wasn't in the scan the session before (dropped out and came back)
The rule check shows both sides over recent sessions: how often a rule blocked a later breakout, and how often stocks
that passed / failed it went on to break out within 10 sessions — so a rule is loosened only when both agree.
"""
import json

import numpy as np
import pandas as pd

import gtt_process as gp

RULE_IDS = ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"]
READY = {"1-min ORH ready", "Leader flag on 10MA", "Tight at pivot, no volume yet", "Broke out today"}


def _num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else np.nan
    except Exception:
        return np.nan


def _clean(x):
    if isinstance(x, (float, np.floating)):
        return None if not np.isfinite(x) else float(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else x


def save_snapshot(sb, mcfg, sd, live, flags):
    """Upsert today's view of every stock in the scan."""
    picked = {s for s, r in flags.items() if r.get("build_range")}
    rows = []
    for r in live.to_dict("records"):
        s = r["Symbol"]
        rows.append({"user_id": mcfg["user_id"], "market": mcfg["market"], "snap_date": str(sd), "symbol": s,
                     "sector": _clean(r.get("Sector")), "score": _clean(_num(r.get("Score"))), "setup": _clean(r.get("Setup")),
                     "ma_hold": _clean(r.get("MA hold")) or None,
                     "rules": {**{k: r.get(k) for k in RULE_IDS if k in r}, **({"HIDDEN": True} if r.get("Hidden") else {})},
                     "flagged": s in flags, "picked": s in picked, "adr": _clean(_num(r.get("ADR"))),
                     "rel_tight": _clean(_num(r.get("Rel tight"))), "last": _clean(_num(r.get("Last")))})
    for i in range(0, len(rows), 500):
        sb.table("focus_snapshots").upsert(rows[i:i + 500], on_conflict="user_id,market,snap_date,symbol").execute()
    return len(rows)


def todays_breakouts(live):
    chg, vx, adr = (pd.to_numeric(live.get(c), errors="coerce") for c in ("Chg %", "Vol x", "ADR"))
    m = (chg >= np.maximum(0.8 * adr.fillna(0), 3.0)) & (vx >= 2.0)
    return live[m.fillna(False)]


def _prev(sb, mcfg, sd, syms):
    """Latest snapshot before sd for these symbols: {symbol: row}, and the date used."""
    d = (sb.table("focus_snapshots").select("snap_date").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
         .lt("snap_date", str(sd)).order("snap_date", desc=True).limit(1).execute().data)
    if not d:
        return {}, None
    pdate = str(d[0]["snap_date"])[:10]
    rows = []
    syms = list(syms)
    for i in range(0, len(syms), 100):
        rows += (sb.table("focus_snapshots").select("*").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
                 .eq("snap_date", pdate).in_("symbol", syms[i:i + 100]).execute().data or [])
    return {r["symbol"]: r for r in rows}, pdate


def run_review(sb, mcfg, sd, live, shown_min=6, ready=READY):
    """Sort today's breakouts into buckets against the previous snapshot and store them."""
    bo = todays_breakouts(live)
    if bo.empty:
        return pd.DataFrame()
    prev, pdate = _prev(sb, mcfg, sd, bo["Symbol"])
    out = []
    for r in bo.to_dict("records"):
        s, p = r["Symbol"], prev.get(r["Symbol"])
        failed = ""
        prules = (p or {}).get("rules") or {}
        if isinstance(prules, str):
            try:
                prules = json.loads(prules)
            except Exception:
                prules = {}
        if p is None:
            bucket = "Not in the scan before" if pdate else "No earlier snapshot yet"
        elif prules.get("HIDDEN"):
            bucket = "Hidden by you"
        elif p.get("picked"):
            bucket = "Picked"
        elif p.get("flagged"):
            bucket = "Flagged"
        elif (_num(p.get("score")) >= shown_min) or (p.get("setup") in ready):
            bucket = "Shown, not flagged"
        else:
            rules = p.get("rules") or {}
            if isinstance(rules, str):
                try:
                    rules = json.loads(rules)
                except Exception:
                    rules = {}
            failed = " ".join(k for k in RULE_IDS if rules.get(k) in ("✗", "½"))
            bucket = "Held back by rules"
        out.append({"user_id": mcfg["user_id"], "market": mcfg["market"], "snap_date": str(sd), "symbol": s,
                    "sector": _clean(r.get("Sector")), "chg": _clean(_num(r.get("Chg %"))), "vol_x": _clean(_num(r.get("Vol x"))),
                    "adr": _clean(_num(r.get("ADR"))), "bucket": bucket, "prev_date": pdate if p is not None else None,
                    "prev_score": _clean(_num(p.get("score"))) if p else None, "prev_setup": p.get("setup") if p else None,
                    "prev_ma_hold": p.get("ma_hold") if p else None, "failed_rules": failed})
    sb.table("breakout_reviews").upsert(out, on_conflict="user_id,market,snap_date,symbol").execute()
    return pd.DataFrame(out)


def _recent(sb, mcfg, table, cols, days=40):
    since = (pd.Timestamp.today() - pd.Timedelta(days=days)).date().isoformat()
    rows, start = [], 0
    while True:
        chunk = (sb.table(table).select(cols).eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
                 .gte("snap_date", since).range(start, start + 999).execute().data or [])
        rows += chunk
        if len(chunk) < 1000:
            break
        start += 1000
    return pd.DataFrame(rows)


def rule_check(sb, mcfg):
    """Per rule: breakouts it held back, and the later-breakout rate when the rule passed vs failed."""
    rev = _recent(sb, mcfg, "breakout_reviews", "snap_date,symbol,bucket,failed_rules")
    snap = _recent(sb, mcfg, "focus_snapshots", "snap_date,symbol,rules")
    if rev.empty or snap.empty:
        return pd.DataFrame(), rev
    rev["snap_date"] = pd.to_datetime(rev["snap_date"])
    snap["snap_date"] = pd.to_datetime(snap["snap_date"])
    dates = sorted(snap["snap_date"].unique())
    bo = rev.groupby("symbol")["snap_date"].apply(list).to_dict()

    def broke_later(sym, d):
        nxt = [x for x in dates if x > d][:10]
        return bool(nxt) and any(x in nxt for x in bo.get(sym, []))
    snap = snap[snap["snap_date"] < (dates[-1] if dates else pd.Timestamp.max)]
    rows = []
    held = rev[rev["bucket"] == "Held back by rules"]
    for k in RULE_IDS:
        blocked = int(held["failed_rules"].fillna("").str.split().apply(lambda l: k in l).sum()) if not held.empty else 0
        res = {"✓": [], "✗": []}
        for r in snap.itertuples():
            rules = r.rules if isinstance(r.rules, dict) else {}
            m = rules.get(k)
            if m in ("✓", "✗"):
                res[m].append(broke_later(r.symbol, r.snap_date))
        p_ok = 100 * np.mean(res["✓"]) if res["✓"] else np.nan
        p_no = 100 * np.mean(res["✗"]) if res["✗"] else np.nan
        if not blocked and not res["✓"] and not res["✗"]:
            continue                                   # rule not used on this page (crypto has R1–R7)
        rows.append({"Rule": k, "Breakouts held back": blocked,
                     "Broke out within 10 sessions — when ✓ %": round(p_ok, 1) if np.isfinite(p_ok) else np.nan,
                     "— when ✗ %": round(p_no, 1) if np.isfinite(p_no) else np.nan,
                     "Stocks ✓ / ✗": f"{len(res['✓'])} / {len(res['✗'])}"})
    return pd.DataFrame(rows), rev


BUCKET_COLOURS = {"Hidden by you": "#f5c2c7", "Held back by rules": "#fff3cd", "Shown, not flagged": "#cfe2ff", "Flagged": "#d4edda", "Picked": "#28a745",
                  "Not in the scan before": "#e2e3e5", "No earlier snapshot yet": "#e2e3e5"}


def render_review(st, sb, mcfg, sd, live, flags, step="5", base=None, shown_min=6, ready=READY):
    mkt = mcfg.get("market")
    st.markdown(f"#### {step} · Review — today's breakouts vs what this page said the session before")
    key = f"fr_saved_{mkt}_{sd}"
    try:
        if not st.session_state.get(key):
            save_snapshot(sb, mcfg, sd, live, flags)
            st.session_state[key] = run_review(sb, mcfg, sd, live, shown_min, set(ready))
            st.session_state[key] = True if st.session_state[key] is None else st.session_state[key]
        rv = st.session_state.get(key)
        rv = rv if isinstance(rv, pd.DataFrame) else pd.DataFrame()
    except Exception as e:
        st.caption(f"Review unavailable — run supabase_focus_review.sql once in Supabase. ({e})"); return
    st.caption("Breakout = up ≥ 0.8 ADR and ≥ 3% on ≥ 2x volume today. Each one is compared with the snapshot from the "
               "previous session you opened this page. Tune a rule only when it keeps blocking good breakouts "
               "(about 3+ times) AND the rule check below agrees.")
    if rv.empty:
        st.info("No breakouts in today's scan (or no earlier snapshot yet — it starts filling from today).")
    else:
        view = rv[["symbol", "sector", "chg", "vol_x", "adr", "bucket", "prev_date", "prev_score", "prev_setup",
                   "prev_ma_hold", "failed_rules"]].rename(columns={
            "symbol": "Symbol", "sector": "Sector", "chg": "Chg %", "vol_x": "Vol x", "adr": "ADR", "bucket": "What happened",
            "prev_date": "We looked on", "prev_score": "Score then", "prev_setup": "Setup then", "prev_ma_hold": "MA hold then",
            "failed_rules": "Rules that held it back"})
        order = {"Hidden by you": 0, "Held back by rules": 1, "Shown, not flagged": 2, "Flagged": 3, "Picked": 4}
        view = view.assign(_o=view["What happened"].map(order).fillna(9)).sort_values(["_o", "Chg %"], ascending=[True, False]) \
                   .drop(columns="_o").reset_index(drop=True)
        counts = view["What happened"].value_counts()
        st.caption(" · ".join(f"**{k}** {v}" for k, v in counts.items()))
        sty = view.style.format({"Chg %": "{:+.1f}", "Vol x": "{:.1f}", "ADR": "{:.1f}", "Score then": "{:.1f}"}, na_rep="–") \
            .map(lambda x: f"background-color:{BUCKET_COLOURS.get(str(x), '')};color:{'white' if x == 'Picked' else 'black'}"
                 if str(x) in BUCKET_COLOURS else "", subset=["What happened"])
        try:
            extra = gp.live_extra(base if base is not None else st.session_state.get("gtt_base_df"), view["Symbol"])
        except Exception:
            extra = None
        gp.card_grid(st, view, key=f"fr_grid_{mkt}_{sd}", sty=sty, extra=extra,
                     height=min(32 * len(view) + 60, 420), widths={"Symbol": 110, "What happened": 160, "Setup then": 170,
                                                                    "Rules that held it back": 150})
    with st.expander("Rule check — last ~40 days (both sides of the evidence)"):
        try:
            rc, rev = rule_check(sb, mcfg)
        except Exception as e:
            st.caption(f"Rule check unavailable ({e})."); return
        if rc.empty:
            st.caption("Needs a few sessions of snapshots and breakouts — check back in a week or two.")
            return
        st.caption("Breakouts held back = later breakouts where this rule was ✗ or ½ the session before. "
                   "Broke out within 10 sessions = of all stocks where the rule passed (✓) or failed (✗), how many broke out "
                   "in the next 10 sessions. A rule earns its place when ✓ clearly beats ✗. If ✗ breaks out as often as ✓ "
                   "and it keeps holding back breakouts, it's a candidate to loosen.")
        st.dataframe(rc, hide_index=True, use_container_width=True)
        if not rev.empty:
            st.caption("Breakouts reviewed so far: " + " · ".join(f"{k} {v}" for k, v in rev["bucket"].value_counts().items()))
