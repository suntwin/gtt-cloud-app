"""
Positions tab — the open trades you're managing.

Prices come from MarketInOut — your positions screen (a MIO screen over your positions watchlist, same GTT formula),
then today's scan. Yahoo Finance was dropped (its numbers didn't match).

Warnings, most urgent first:
  🔴 at/below the stop · back below the breakout's fail level · big-volume down day · closed below the 20MA
  🟠 below the 10MA · stretched (> 4 ADR above the 10MA, or > 3 ADR above the 10-week MA)
  🟡 day 3–5 and up → sell ⅓ into strength, stop to breakeven · dropped out of the scan
"""
import math
from datetime import date

import numpy as np
import pandas as pd
import requests
import streamlit as _st

import gtt_process as gp
import focus_flow as ff

STRETCH_10MA, STRETCH_10W, VOL_DUMP = 4.0, 3.0, 2.0
REMAINDER = "↳ rest after partial sale"
LEVEL = {"🔴": 0, "🟠": 1, "🟡": 2, "🟢": 3}


def _num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else np.nan
    except Exception:
        return np.nan


# ── data ───────────────────────────────────────────────────────────────────
# Prices come from MarketInOut — the same numbers as the scanner. Your positions screen (a MIO screen over your
# positions watchlist, same GTT formula) is read first; anything not in it falls back to today's scan.
POS_SCREEN = {"NSE": "https://api.marketinout.com/run/screen?key=dbf1d7c7f45c4fac", "USA": None}
GTT_COLS = ['Symbol', 'Last', 'Timestamp', '_chg_percentclose', 'dvol', '_avgvol_mln', '_dayhigh',
            '_circuit', '_days_since_bo', '_bo_dollar_vol_mln', '_daylow', 'Adr', 'Ti65',
            '_avg_vol_float_ratio', '_20madist', '_10wmadist', '_10madist', '_nr4', '_nr4_previous', '_rs', '_inside']


@_st.cache_data(ttl=300, show_spinner=False)
def fetch_mio(url):
    """A MarketInOut GTT-layout screen → DataFrame keyed by Symbol (.NS stripped), or None."""
    from io import StringIO
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        if r.status_code != 200 or not r.text.strip():
            return None
        df = pd.read_csv(StringIO(r.text), sep="|", header=None)
        df = gp.assign_scan_columns(df, GTT_COLS)
        df["Symbol"] = df["Symbol"].astype(str).str.upper().str.replace(".NS", "", regex=False).str.strip()
        for c in df.columns:
            if c not in ("Symbol", "Timestamp"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.drop_duplicates("Symbol").set_index("Symbol")
    except Exception:
        return None


def metrics(row, entry_date, avg, stop):
    """Everything the warnings need, from one MarketInOut row (positions screen or today's scan)."""
    g = lambda k: _num(row.get(k))
    last, adr = g("Last"), g("Adr")
    m = {"Last": last, "Chg %": g("_chg_percentclose"), "ADR": adr, "Ti65": g("Ti65"),
         "10MA %": g("_10madist"), "20MA %": g("_20madist"),
         "10w %": g("_10wmadist") if np.isfinite(g("_10wmadist")) else g("W_Dist10wMA"),
         "Vol x": g("dvol") / g("_avgvol_mln") if g("_avgvol_mln") else np.nan, "NR4 %": g("_nr4"),
         "Off 1M hi %": g("off_high_21"), "Off 52W hi %": g("off_high_252"), "Timestamp": row.get("Timestamp")}
    for k in ("10MA", "20MA", "10w"):
        m[f"{k} (ADR)"] = m[f"{k} %"] / adr if adr else np.nan
    m["10MA"] = last / (1 + m["10MA %"] / 100) if np.isfinite(m["10MA %"]) else np.nan
    m["20MA"] = last / (1 + m["20MA %"] / 100) if np.isfinite(m["20MA %"]) else np.nan
    m["Rel tight"] = m["NR4 %"] / adr if adr else np.nan
    m["MA hold"] = gp.ma_hold_label(row.get("_hl_pct"), row.get("_low_10ma"), row.get("_low_20ma"), row.get("_hold_ma"),
                                    adr, m["20MA %"], m["10MA %"], m["Rel tight"])
    m["Days held"] = int(np.busday_count(pd.Timestamp(entry_date).date(), date.today())) + 1
    m["Return %"] = (last / avg - 1) * 100 if avg and np.isfinite(last) else np.nan
    m["R"] = (last - avg) / (avg - stop) if stop and avg and avg > stop and np.isfinite(last) else np.nan
    return m


def assess(m, p, n_exits, in_scan):
    """[(level, text)] warnings + a one-line plan."""
    w = []
    last, stop, fail = m.get("Last"), _num(p.get("stop")), _num(p.get("fail_level"))
    if np.isfinite(stop) and np.isfinite(last) and last <= stop:
        w.append(("🔴", f"At/below your stop {stop:g} — exit"))
    if np.isfinite(fail) and np.isfinite(last) and last < fail:
        w.append(("🔴", f"Back below the breakout's fail level {fail:g} — the breakout failed"))
    if _num(m.get("Chg %")) < 0 and _num(m.get("Vol x")) >= VOL_DUMP:
        w.append(("🔴", f"Down {abs(m['Chg %']):.1f}% on {m['Vol x']:.1f}x volume — sellers stepping in"))
    if _num(m.get("20MA %")) < 0:
        w.append(("🔴", "Below the 20MA — exit signal for most swings"))
    elif _num(m.get("10MA %")) < 0:
        w.append(("🟠", "Below the 10MA — first warning; tighten the stop"))
    if _num(m.get("10MA (ADR)")) > STRETCH_10MA:
        w.append(("🟠", f"Stretched: {m['10MA (ADR)']:.1f} ADR above the 10MA — consider selling a part into strength"))
    if _num(m.get("10w (ADR)")) > STRETCH_10W:
        w.append(("🟠", f"Stretched weekly: {m['10w (ADR)']:.1f} ADR above the 10-week MA — late, trim"))
    d = _num(m.get("Days held"))
    if 3 <= d <= 5 and _num(m.get("Return %")) > 0 and n_exits == 0 and REMAINDER not in str(p.get("note") or ""):
        w.append(("🟡", f"Day {int(d)} and up {m['Return %']:.1f}% — sell ⅓ into strength, move the stop to breakeven"))
    if not in_scan:
        w.append(("🟡", "Dropped out of the scan — no longer passes the screens"))
    w.sort(key=lambda x: LEVEL[x[0]])
    if w:
        plan = w[0][1]
    elif np.isfinite(_num(m.get("10MA"))) and _num(m.get("Return %")) > 0:
        plan = f"Hold — trail the stop on the 10MA ({m['10MA']:.2f}, {m['10MA (ADR)']:.1f} ADR below)"
    else:
        plan = "Hold — keep the stop" + (f" at {stop:g}" if np.isfinite(stop) else "; set one")
    return w, plan


# ── database ───────────────────────────────────────────────────────────────
def load_positions(sb, mcfg, status="open"):
    return (sb.table("positions").select("*").eq("user_id", mcfg["user_id"]).eq("market", mcfg["market"])
            .eq("status", status).order("entry_date").execute().data or [])


def load_exits(sb, ids):
    ids = [int(i) for i in ids]
    return (sb.table("position_exits").select("*").in_("position_id", ids).execute().data or []) if ids else []


def _equity(st, sb, mcfg):
    """Account size for the % figures — saved in scoring_prefs (scanner_type POSITIONS_<market>)."""
    mkt, k = mcfg["market"], f"pos_equity_{mcfg['market']}"
    if f"{k}_loaded" not in st.session_state:
        st.session_state[f"{k}_loaded"] = True
        try:
            r = sb.table("scoring_prefs").select("config").eq("user_id", mcfg["user_id"]).eq("scanner_type", f"POSITIONS_{mkt}").execute()
            st.session_state[k] = float((r.data[0]["config"] or {}).get("equity", 0)) if r.data else 0.0
        except Exception:
            st.session_state.setdefault(k, 0.0)
    c1, c2 = st.columns([1, 3])
    eq = c1.number_input("Account size", min_value=0.0, step=10000.0, format="%.0f", key=k,
                         help="Used for exposure / risk as % of the account. Saved for next time.")
    if f"{k}_saved" in st.session_state and eq != st.session_state[f"{k}_saved"]:      # changed → save it
        try:
            cfg = {"equity": eq}
            ex = sb.table("scoring_prefs").select("id").eq("user_id", mcfg["user_id"]).eq("scanner_type", f"POSITIONS_{mkt}").execute()
            if ex.data:
                sb.table("scoring_prefs").update({"config": cfg}).eq("user_id", mcfg["user_id"]).eq("scanner_type", f"POSITIONS_{mkt}").execute()
            else:
                sb.table("scoring_prefs").insert({"user_id": mcfg["user_id"], "scanner_type": f"POSITIONS_{mkt}", "config": cfg}).execute()
        except Exception:
            pass
    st.session_state[f"{k}_saved"] = eq
    if not eq:
        c2.caption("Set your account size to see exposure and risk as % of the account.")
    return eq


def _booked_closed(sb, mcfg):
    """Realised P&L of all closed trades (needs quantities)."""
    try:
        closed = load_positions(sb, mcfg, "closed")
        ex = pd.DataFrame(load_exits(sb, [p["id"] for p in closed]))
        if ex.empty:
            return 0.0
        avg = {p["id"]: _num(p["avg_price"]) for p in closed}
        return float(((pd.to_numeric(ex["price"]) - ex["position_id"].map(avg)) * pd.to_numeric(ex["qty"])).sum())
    except Exception:
        return 0.0


# ── UI ─────────────────────────────────────────────────────────────────────
def _c_ret(x):
    x = _num(x)
    if not np.isfinite(x):
        return ""
    return ff._css("#28a745", "white", True) if x >= 10 else ff._css("#8ee68e") if x > 0 else \
        ff._css("#f8d7da", "#721c24") if x > -5 else ff._css("#dc3545", "white", True)


def _c_status(x):
    return {"🔴": ff._css("#dc3545", "white", True), "🟠": ff._css("#fd7e14", "white", True),
            "🟡": ff._css("#fff3cd", "#664d03", True), "🟢": ff._css("#d4edda", "#155724", True)}.get(str(x)[:1], "")


def _style(v):
    fmt = {c: "{:.2f}" for c in ("Last", "Avg", "Stop", "R", "Rel tight", "10MA (ADR)", "20MA (ADR)", "10w (ADR)")}
    fmt.update({c: "{:+.1f}" for c in ("Return %", "Chg %")})
    fmt.update({c: "{:.1f}" for c in ("ADR", "Vol x", "Off 52W hi %", "Off 1M hi %")})
    fmt["Ti65"] = "{:.2f}"
    fmt.update({c: "{:,.0f}" for c in ("Cost", "Value", "Risk to stop")})
    fmt.update({"P&L": "{:+,.0f}", "Weight %": "{:.1f}"})
    sty = v.style.format({k: f for k, f in fmt.items() if k in v.columns}, na_rep="–")
    for c, f in (("Return %", _c_ret), ("Status", _c_status), ("Chg %", gp._c_chg), ("Vol x", gp._c_vol),
                 ("10MA (ADR)", ff._c_ma10), ("Rel tight", ff._hl("tight")), ("ADR", ff._hl("adr")),
                 ("10w (ADR)", ff._hl("relwk"))):
        if c in v.columns:
            sty = sty.map(f, subset=[c])
    if "P&L" in v.columns:
        sty = sty.map(lambda x: ff._css("#d4edda", "#155724") if _num(x) > 0 else ff._css("#f8d7da", "#721c24")
                      if _num(x) < 0 else "", subset=["P&L"])
    if "R" in v.columns:
        sty = sty.map(lambda x: ff._css("#28a745", "white", True) if _num(x) >= 2 else ff._css("#8ee68e") if _num(x) > 0
                      else ff._css("#f8d7da", "#721c24") if np.isfinite(_num(x)) else "", subset=["R"])
    return sty


def _lookup(st, base_df, mcfg, sym):
    """Sector / industry / setup / fail level for a new position, from the scan, Sector Focus and the watchlist."""
    out = {}
    view = st.session_state.get(f"sf_view_{mcfg['market']}")
    if view is not None and not view.empty and sym in set(view["Symbol"]):
        r = view[view["Symbol"] == sym].iloc[0]
        out.update(sector=r.get("Sector"), industry=r.get("Industry"))
    if base_df is not None and not base_df.empty and "Symbol" in base_df.columns:
        b = base_df[base_df["Symbol"].astype(str).str.upper() == sym]
        if not b.empty:
            out.setdefault("sector", b.iloc[0].get("Sector"))
            out.setdefault("industry", b.iloc[0].get("Industry"))
    return {k: (None if (isinstance(v, float) and not np.isfinite(v)) or v in ("Unknown", "") else v) for k, v in out.items()}


def render_positions_tab(st, sb, base_df, mcfg):
    mkt = mcfg["market"]
    st.subheader(f"Positions · {mkt}")
    st.caption("Your open trades, checked every time the page loads with MarketInOut's numbers (your positions screen first, "
               "then today's scan) — so warnings keep working after a stock drops out of the scan, as long as it's in your "
               "MIO positions watchlist. 🔴 act now · 🟠 tighten / trim · 🟡 heads-up · 🟢 fine.")
    if sb is None:
        st.error("Database not connected."); return
    try:
        open_ = load_positions(sb, mcfg)
    except Exception as e:
        st.error(f"Run supabase_positions.sql once in Supabase (creates the positions tables). ({e})"); return

    # ── add ──
    with st.expander("➕ Add a position", expanded=not open_):
        with st.form(f"pos_add_{mkt}", clear_on_submit=True):
            c = st.columns([2, 2, 2, 2, 2])
            sym = c[0].text_input("Symbol", placeholder="e.g. SUNTV" if mkt == "NSE" else "e.g. GRAL").strip().upper().split(":")[-1]
            ent = c[1].date_input("Entry date", value=date.today())
            avg = c[2].number_input("Average price", min_value=0.0, value=0.0, step=0.05, format="%.2f")
            qty = c[3].number_input("Quantity (0 = don't track size)", min_value=0.0, value=0.0, step=1.0)
            stop = c[4].number_input("Initial stop (0 = none)", min_value=0.0, value=0.0, step=0.05, format="%.2f")
            c2 = st.columns([2, 6])
            setup = c2[0].selectbox("Setup", ["—"] + list(gp.SETUP_TYPES) + ["1-min ORH", "Pullback", "Other"])
            note = c2[1].text_input("Note", placeholder="why you took it, plan …")
            add = st.form_submit_button("Add position", type="primary")
        if add:
            if not sym or avg <= 0:
                st.warning("Symbol and average price are needed.")
            else:
                info = _lookup(st, base_df, mcfg, sym)
                fail = None
                try:
                    w = [r for r in gp.load_active_watchlist(sb, mcfg) if str(r.get("symbol")).upper() == sym]
                    fail = _num(w[0].get("prev_close")) if w else None
                    fail = fail if fail and np.isfinite(fail) else None
                except Exception:
                    pass
                sb.table("positions").insert({
                    "user_id": mcfg["user_id"], "market": mkt, "symbol": sym, "sector": info.get("sector"),
                    "industry": info.get("industry"), "setup": None if setup == "—" else setup, "entry_date": ent.isoformat(),
                    "avg_price": float(avg), "qty": float(qty), "qty_open": float(qty), "stop": float(stop) or None,
                    "init_stop": float(stop) or None, "fail_level": fail, "note": note}).execute()
                st.success(f"Added {sym}."); st.rerun()

    _edit_box(st, sb, mcfg, open_)
    if not open_ and st.session_state.get(f"pos_msg_{mkt}"):
        st.success(st.session_state.pop(f"pos_msg_{mkt}"))

    if not open_:
        st.info("No open positions.")
        _render_stats(st, sb, mcfg, None, _equity(st, sb, mcfg))
        _render_closed(st, sb, mcfg); return

    # ── evaluate ──
    live = gp.add_derived(base_df).drop_duplicates("Symbol").set_index("Symbol") if base_df is not None and not base_df.empty else None
    view = st.session_state.get(f"sf_view_{mkt}")
    vrow = view.set_index("Symbol") if view is not None and not view.empty else None
    exits = pd.DataFrame(load_exits(sb, [p["id"] for p in open_]))
    url = POS_SCREEN.get(mkt)
    pos_df = fetch_mio(url) if url else None
    rows, missing = [], []
    with st.spinner("Checking your positions…"):
        for p in open_:
            sym = p["symbol"]
            in_scan = live is not None and sym in live.index
            if pos_df is not None and sym in pos_df.index:
                row, src = pos_df.loc[sym], "positions screen"
            elif in_scan:
                row, src = live.loc[sym], "scan"
            else:
                row, src = None, "no data"
                missing.append(sym)
            m = metrics(row, p["entry_date"], _num(p["avg_price"]), _num(p.get("init_stop") or p.get("stop"))) if row is not None else {}
            if m and m.get("Timestamp"):
                src += f" {str(m['Timestamp'])[:16]}"
            ex = exits[exits["position_id"] == p["id"]] if not exits.empty else pd.DataFrame()
            w, plan = assess(m, p, len(ex), in_scan) if m else \
                ([("🟠", "No data — add it to your MarketInOut positions watchlist")], "Check the chart")
            status = w[0][0] if w else "🟢"
            realised = float(((pd.to_numeric(ex["price"]) - _num(p["avg_price"])) * pd.to_numeric(ex["qty"])).sum()) if len(ex) else 0.0
            vr = vrow.loc[sym] if vrow is not None and sym in vrow.index else {}
            if not p.get("sector"):                       # learn the sector once from the scan / Sector Focus and keep it
                sec_ = (vr.get("Sector") if len(vr) else None) or (live.loc[sym].get("Sector") if in_scan else None)
                if isinstance(sec_, str) and sec_ and sec_ != "Unknown":
                    p["sector"] = sec_
                    try:
                        sb.table("positions").update({"sector": sec_}).eq("id", int(p["id"])).execute()
                    except Exception:
                        pass
            rows.append({"id": p["id"], "Symbol": sym, "Status": status + (f" {len(w)}" if len(w) > 1 else ""), "Plan": plan,
                         "Return %": m.get("Return %"), "R": m.get("R"), "Days": m.get("Days held"),
                         "Last": m.get("Last"), "Chg %": m.get("Chg %"), "Avg": _num(p["avg_price"]), "Stop": _num(p.get("stop")),
                         "Qty open": _num(p.get("qty_open")), "Vol x": m.get("Vol x"),
                         "10MA (ADR)": m.get("10MA (ADR)"), "20MA (ADR)": m.get("20MA (ADR)"), "10w (ADR)": m.get("10w (ADR)"),
                         "Rel tight": m.get("Rel tight"), "ADR": m.get("ADR"), "Ti65": m.get("Ti65"),
                         "Off 1M hi %": m.get("Off 1M hi %"), "Off 52W hi %": m.get("Off 52W hi %"),
                         "MA hold": m.get("MA hold") or (vr.get("MA hold") if len(vr) else ""),
                         "Sector": p.get("sector") or (vr.get("Sector") if len(vr) else None),
                         "Setup": p.get("setup"), "👑": vr.get("Leader") if len(vr) else "",
                         "In scan": "yes" if in_scan else "no", "Entry": str(p["entry_date"])[:10],
                         "Warnings": " · ".join(f"{a} {b}" for a, b in w), "Data": src, "Note": p.get("note") or "",
                         "Sell qty": 0.0, "Sell price": 0.0, "Close": False, "Delete": False,
                         "_realised": realised, "_pnl": (m.get("Last", np.nan) - _num(p["avg_price"])) * _num(p.get("qty_open")),
                         "_cost": _num(p["avg_price"]) * _num(p.get("qty_open")),
                         "_value": _num(m.get("Last")) * _num(p.get("qty_open")),
                         "_risk": max(_num(m.get("Last")) - _num(p.get("stop")), 0) * _num(p.get("qty_open"))
                         if np.isfinite(_num(p.get("stop"))) else np.nan})
    v = pd.DataFrame(rows)
    v = v.assign(_o=v["Status"].str[:1].map(LEVEL)).sort_values(["_o", "Return %"], ascending=[True, False]).drop(columns="_o") \
         .reset_index(drop=True)

    _close_form(st, sb, mcfg)
    if st.session_state.get(f"pos_msg_{mkt}"):                 # shown here so it isn't hidden in a collapsed section
        st.success(st.session_state.pop(f"pos_msg_{mkt}"))
    _red = v.loc[v["Status"].str.startswith("🔴"), "Symbol"].tolist()
    st.markdown(f"**{len(v)} open** · 🔴 {len(_red)} · 🟠 {int(v['Status'].str.startswith('🟠').sum())}"
                + (f" — **act now: {', '.join(_red)}**" if _red else ""))
    with st.expander("📋 Open positions — summary, warnings, table"):
        # ── summary ──
        eq = _equity(st, sb, mcfg)
        n_red = int(v["Status"].str.startswith("🔴").sum()); n_or = int(v["Status"].str.startswith("🟠").sum())
        has_qty = bool(pd.to_numeric(v["Qty open"], errors="coerce").fillna(0).sum())
        cost, opnl = float(np.nansum(v["_cost"])), float(np.nansum(v["_pnl"]))
        value, risk = cost + opnl, float(np.nansum(v["_risk"]))          # a row without a price counts at cost
        booked = float(v["_realised"].sum()) + _booked_closed(sb, mcfg)
        pct = lambda x: f"{100 * x / eq:.1f}% of account" if eq else None
        c = st.columns(5)
        c[0].metric("Open", len(v), help="Open positions")
        c[1].metric("🔴 Act now", n_red)
        c[2].metric("🟠 Tighten / trim", n_or)
        c[3].metric("Avg return", f"{pd.to_numeric(v['Return %'], errors='coerce').mean():+.1f}%")
        c[4].metric("Locked in", f"{int((pd.to_numeric(v['Stop'], errors='coerce') >= v['Avg']).sum())} of {len(v)}",
                    help="Positions whose stop is at or above your average price — can't turn into a loss")
        if has_qty:
            c = st.columns(5)
            c[0].metric("Exposure (cost)", f"{cost:,.0f}", pct(cost), delta_color="off",
                        help="Total cost basis of open positions = average price × shares still held")
            c[1].metric("Market value", f"{value:,.0f}", pct(value), delta_color="off")
            c[2].metric("Open P&L", f"{opnl:+,.0f}", f"{100 * opnl / cost:+.1f}%" if cost else None)
            c[3].metric("Open risk to stops", f"{risk:,.0f}", pct(risk), delta_color="off",
                        help="What you'd give back if every position hit its current stop (from the last price). "
                             "Positions without a stop aren't counted.")
            c[4].metric("Booked P&L", f"{booked:+,.0f}", help="Partial sells on open positions + all closed trades")
            if eq:
                if value > eq:
                    st.warning(f"Invested {100 * value / eq:.0f}% of the account — above 100% (margin).")
                if risk / eq > 0.06:
                    st.warning(f"Open risk is {100 * risk / eq:.1f}% of the account — above 6%. Tighten stops or trim before adding.")
            sec = v.assign(_s=v["Sector"].fillna("Unknown")).groupby("_s")["_cost"].sum().sort_values(ascending=False)
            sec = sec[sec > 0]
            known = sec.drop("Unknown", errors="ignore")
            if len(sec) and cost:
                st.caption("Exposure by sector: " + " · ".join(f"**{k}** {100 * x / cost:.0f}%" for k, x in sec.items()))
                if len(known) and known.iloc[0] / cost > 0.4 and len(v) > 2:
                    st.warning(f"{100 * known.iloc[0] / cost:.0f}% of your exposure is in {known.index[0]} — one bad day for the "
                               "sector hits most of the book.")
            v["Cost"], v["Value"], v["P&L"] = v["_cost"], v["_value"], v["_pnl"]
            v["Weight %"] = 100 * v["_value"] / eq if eq else np.nan
            v["Risk to stop"] = v["_risk"]
        else:
            st.caption("Add quantities to your positions to see exposure, market value, P&L, open risk and sector weights.")
        if not url:
            st.info(f"No MarketInOut positions screen set for {mkt} yet — positions use today's scan only (those that dropped "
                    "out show 'No data'). Send the screen's API link to add it.")
        elif pos_df is None:
            st.warning("Couldn't read your MarketInOut positions screen — using today's scan where possible.")
        if missing:
            st.warning(f"No data for {', '.join(missing)} — not in your MarketInOut positions screen or today's scan. "
                       "Add them to the positions watchlist in MarketInOut.")
        for _, r in v[v["Status"].str.startswith("🔴")].iterrows():
            st.error(f"**{r['Symbol']}** — {r['Warnings']}")

        # ── table ──
        shown = [c for c in v.columns if not c.startswith("_") and c != "id"]
        money = [c for c in ("Cost", "Value", "P&L", "Weight %", "Risk to stop") if c in shown]
        shown = [c for c in shown if c not in money]
        shown[shown.index("Qty open") + 1:shown.index("Qty open") + 1] = money
        ed, saved = gp.card_grid(st, v[shown], key=f"pos_grid_{mkt}", sty=_style(v[shown]),
                                 edit={"Stop": "number", "Note": "text", "Sell qty": "number", "Sell price": "number",
                                       "Close": "bool", "Delete": "bool"},
                                 submit="Save positions", height=min(32 * len(v) + 90, 560),
                                 widths={"Symbol": 105, "Status": 62, "Plan": 330, "Warnings": 420, "Sector": 150, "Note": 200})
        st.caption("Click a ticker for the full card · type a new **Stop** to trail it · record a partial sale with **Sell qty** + "
                   "**Sell price** · **Close** sells what's left (at Sell price, or the last price if blank) · **Delete** = "
                   "entered by mistake. Then **Save positions**.")
        if saved and ed is not None and "Symbol" in ed.columns:
            by_id = {p["id"]: p for p in open_}
            orig = v.set_index("Symbol")
            msgs = []
            for r in ed.to_dict("records"):
                s = r["Symbol"]
                if s not in orig.index:
                    continue
                o, pid = orig.loc[s], int(orig.loc[s, "id"])
                if r.get("Delete"):
                    sb.table("positions").delete().eq("id", pid).execute(); msgs.append(f"deleted {s}"); continue
                upd = {}
                ns = _num(r.get("Stop"))
                if np.isfinite(ns) and ns != _num(o["Stop"]):
                    upd["stop"] = ns
                if str(r.get("Note") or "") != str(o["Note"] or ""):
                    upd["note"] = str(r.get("Note") or "")
                q_open = _num(o["Qty open"]) if np.isfinite(_num(o["Qty open"])) else 0.0
                sq, sp = _num(r.get("Sell qty")) or 0.0, _num(r.get("Sell price"))
                sq = sq if np.isfinite(sq) else 0.0
                if r.get("Close"):
                    if np.isfinite(sp) and sp > 0:
                        sb.table("position_exits").insert({"position_id": pid, "qty": q_open, "price": sp,
                                                           "note": "closed"}).execute()
                        upd.update(status="closed", closed_at=date.today().isoformat(), qty_open=0)
                        msgs.append(f"closed {s} @ {sp:g}")
                    else:                                   # no price typed → ask for it (below the status line)
                        pend = st.session_state.setdefault(f"pos_pend_{mkt}", {})
                        pend[pid] = {"Symbol": s, "qty": q_open, "last": _num(o["Last"])}
                        msgs.append(f"{s}: enter the closing price above")
                elif sq > 0 and np.isfinite(sp) and sp > 0:
                    # Partial sale = split, like the trades sheet: this row closes with the shares sold, and a new open row
                    # carries the rest (same entry date, price and stop).
                    sq = min(sq, q_open) if q_open else sq
                    sb.table("position_exits").insert({"position_id": pid, "qty": sq, "price": sp, "note": "partial"}).execute()
                    upd.update(qty=sq, qty_open=0, status="closed", closed_at=date.today().isoformat())
                    rest = q_open - sq
                    if rest > 0:
                        src_ = by_id.get(pid, {})
                        sb.table("positions").insert({k: src_.get(k) for k in ("user_id", "market", "symbol", "sector", "industry",
                                                      "setup", "entry_date", "avg_price", "stop", "init_stop", "fail_level")}
                                                     | {"qty": rest, "qty_open": rest,
                                                        "note": ((src_.get("note") or "").replace(REMAINDER, "").strip() + " " + REMAINDER).strip()}).execute()
                    msgs.append(f"sold {sq:g} {s} @ {sp:g}" + (f" · {rest:g} still open" if rest > 0 else ""))
                if upd:
                    upd["updated_at"] = pd.Timestamp.utcnow().isoformat()
                    sb.table("positions").update(upd).eq("id", pid).execute()
                    if "stop" in upd and not any(s in m_ for m_ in msgs):
                        msgs.append(f"{s} stop → {upd['stop']:g}")
            if msgs:
                st.session_state[f"pos_msg_{mkt}"] = "Saved: " + " · ".join(msgs)
                st.rerun()
        if st.session_state.get(f"pos_msg_{mkt}"):
            st.success(st.session_state.pop(f"pos_msg_{mkt}"))
        gp.copy_area(st, "Open positions — for TradingView", ",".join(gp.tv_symbol(x, mcfg) for x in v["Symbol"]),
                     key=f"pos_copy_{mkt}")
    _render_stats(st, sb, mcfg, v, eq)
    _render_closed(st, sb, mcfg)


def _close_form(st, sb, mcfg):
    """Positions ticked Close without a price: ask for the closing price and date before closing them."""
    mkt = mcfg["market"]
    pend = st.session_state.get(f"pos_pend_{mkt}") or {}
    if not pend:
        return
    with st.container(border=True):
        st.markdown("**Close position" + ("s" if len(pend) > 1 else "") + " — enter the price you sold at**")
        with st.form(f"pos_close_form_{mkt}"):
            vals = {}
            for pid, r in pend.items():
                c1, c2, c3 = st.columns([2, 2, 2])
                c1.markdown(f"**{r['Symbol']}** · {r['qty']:g} shares")
                last = r["last"] if np.isfinite(_num(r["last"])) else 0.0
                vals[pid] = (c2.number_input(f"Closing price · {r['Symbol']}", min_value=0.0, value=float(last), step=0.05,
                                             format="%.2f", key=f"pos_cp_{mkt}_{pid}"),
                             c3.date_input(f"Closed on · {r['Symbol']}", value=date.today(), key=f"pos_cd_{mkt}_{pid}"))
            b1, b2 = st.columns([1, 5])
            ok = b1.form_submit_button("Confirm close", type="primary")
            cancel = b2.form_submit_button("Cancel")
        if cancel:
            st.session_state.pop(f"pos_pend_{mkt}", None); st.rerun()
        if ok:
            done = []
            for pid, (price, when) in vals.items():
                if not price or price <= 0:
                    st.warning(f"{pend[pid]['Symbol']}: enter a closing price."); return
            for pid, (price, when) in vals.items():
                r = pend[pid]
                sb.table("position_exits").insert({"position_id": int(pid), "qty": r["qty"], "price": float(price),
                                                   "exit_date": when.isoformat(), "note": "closed"}).execute()
                sb.table("positions").update({"status": "closed", "closed_at": when.isoformat(), "qty_open": 0,
                                              "updated_at": pd.Timestamp.utcnow().isoformat()}).eq("id", int(pid)).execute()
                done.append(f"{r['Symbol']} @ {price:g}")
            st.session_state.pop(f"pos_pend_{mkt}", None)
            st.session_state[f"pos_msg_{mkt}"] = "Closed: " + " · ".join(done)
            st.rerun()


def _edit_box(st, sb, mcfg, open_):
    """✏️ Fix any position after entering it — open ones and closed trades (exit price / date too)."""
    mkt = mcfg["market"]
    with st.expander("✏️ Edit a position or a closed trade"):
        try:
            closed = load_positions(sb, mcfg, "closed")
        except Exception:
            closed = []
        allp = [(p, "open") for p in open_] + [(p, "closed") for p in sorted(closed, key=lambda x: str(x.get("closed_at") or ""), reverse=True)]
        if not allp:
            st.caption("Nothing to edit yet."); return
        lab = {p["id"]: f"{p['symbol']} · {'open' if k == 'open' else 'closed ' + str(p.get('closed_at') or '')[:10]} · "
                        f"entry {str(p['entry_date'])[:10]} @ {_num(p['avg_price']):g} · "
                        f"{_num(p.get('qty_open') if k == 'open' else p.get('qty')):g} sh" for p, k in allp}
        pid = st.selectbox("Position", list(lab), format_func=lambda i: lab[i], key=f"pos_edit_sel_{mkt}")
        p, kind = next((p, k) for p, k in allp if p["id"] == pid)
        ex = pd.DataFrame(load_exits(sb, [pid])) if kind == "closed" else pd.DataFrame()
        with st.form(f"pos_edit_form_{mkt}_{pid}"):
            c = st.columns(4)
            sym = c[0].text_input("Symbol", value=p["symbol"]).strip().upper().split(":")[-1]
            ent = c[1].date_input("Entry date", value=pd.Timestamp(p["entry_date"]).date())
            avg = c[2].number_input("Average price", min_value=0.0, value=float(_num(p["avg_price"]) or 0), step=0.05, format="%.2f")
            qn = _num(p.get("qty_open") if kind == "open" else p.get("qty"))
            qty = c[3].number_input("Shares" + (" held" if kind == "open" else " sold"), min_value=0.0,
                                    value=float(qn if np.isfinite(qn) else 0), step=1.0)
            c = st.columns(4)
            stp = c[0].number_input("Current stop (0 = none)", min_value=0.0, value=float(_num(p.get("stop")) if np.isfinite(_num(p.get("stop"))) else 0),
                                    step=0.05, format="%.2f")
            ist = c[1].number_input("Initial stop (for R)", min_value=0.0,
                                    value=float(_num(p.get("init_stop")) if np.isfinite(_num(p.get("init_stop"))) else 0), step=0.05, format="%.2f")
            opts = ["—"] + list(gp.SETUP_TYPES) + ["1-min ORH", "Pullback", "Other"]
            setup = c[2].selectbox("Setup", opts, index=opts.index(p.get("setup")) if p.get("setup") in opts else 0)
            sector = c[3].text_input("Sector", value=p.get("sector") or "")
            xp = xd = None
            if kind == "closed" and len(ex):
                q, pr = pd.to_numeric(ex["qty"], errors="coerce").fillna(0), pd.to_numeric(ex["price"], errors="coerce")
                px0 = float((pr * q).sum() / q.sum()) if q.sum() > 0 else float(pr.mean())
                c = st.columns(4)
                xp = c[0].number_input("Exit price", min_value=0.0, value=px0, step=0.05, format="%.2f")
                xd = c[1].date_input("Exit date", value=pd.Timestamp(p.get("closed_at") or date.today()).date())
                if len(ex) > 1:
                    c[2].caption(f"{len(ex)} sales recorded — saving sets them all to this price and date.")
            note = st.text_input("Note", value=(p.get("note") or ""))
            reopen = st.checkbox("Re-open this trade (closed by mistake)") if kind == "closed" else False
            go_ = st.form_submit_button("Save changes", type="primary")
        if go_:
            if not sym or avg <= 0:
                st.warning("Symbol and average price are needed."); return
            upd = {"symbol": sym, "entry_date": ent.isoformat(), "avg_price": float(avg), "stop": float(stp) or None,
                   "init_stop": float(ist) or None, "setup": None if setup == "—" else setup, "sector": sector or None,
                   "note": note, "updated_at": pd.Timestamp.utcnow().isoformat()}
            if kind == "open":
                upd.update(qty_open=float(qty))
                if not _num(p.get("qty")) or _num(p.get("qty")) == _num(p.get("qty_open")):
                    upd["qty"] = float(qty)
            else:
                upd["qty"] = float(qty)
                if xd is not None:
                    upd["closed_at"] = xd.isoformat()
                if reopen:
                    upd.update(status="open", closed_at=None, qty_open=float(qty))
            sb.table("positions").update(upd).eq("id", int(pid)).execute()
            if kind == "closed" and len(ex):
                if reopen:
                    sb.table("position_exits").delete().eq("position_id", int(pid)).execute()
                else:
                    for i in ex["id"]:
                        f = {"price": float(xp), "exit_date": xd.isoformat()}
                        if len(ex) == 1:
                            f["qty"] = float(qty)
                        sb.table("position_exits").update(f).eq("id", int(i)).execute()
            st.session_state[f"pos_msg_{mkt}"] = f"Updated {sym}." + (" Re-opened." if reopen else "")
            st.rerun()


def _lots(sb, mcfg, open_v):
    """One row per trade lot, like the trades sheet: every closed row (shares sold, avg exit) and every open row
    (shares held, at the last price)."""
    rows = []
    try:
        closed = load_positions(sb, mcfg, "closed")
    except Exception:
        closed = []
    allp = closed + (load_positions(sb, mcfg) if open_v is not None else [])
    ex = pd.DataFrame(load_exits(sb, [p["id"] for p in allp]))
    last = dict(zip(open_v["id"], open_v["Last"])) if open_v is not None and len(open_v) else {}
    for p in allp:
        avg, st0 = _num(p["avg_price"]), _num(p.get("init_stop"))
        e = ex[ex["position_id"] == p["id"]] if not ex.empty else pd.DataFrame()
        if len(e):
            q, pr = pd.to_numeric(e["qty"], errors="coerce").fillna(0), pd.to_numeric(e["price"], errors="coerce")
            qs = float(q.sum())
            px = float((pr * q).sum() / qs) if qs > 0 else float(pr.mean())
            d_end = pd.to_datetime(e["exit_date"]).max() if "exit_date" in e else pd.Timestamp(p.get("closed_at") or date.today())
            rows.append({"Symbol": p["symbol"], "Status": "Closed", "Entry": pd.Timestamp(p["entry_date"]), "Exit": d_end,
                         "Qty": qs, "Avg": avg, "Price": px, "Stop0": st0})
        if p.get("status") == "open" and _num(p.get("qty_open")) > 0:
            rows.append({"Symbol": p["symbol"], "Status": "Open", "Entry": pd.Timestamp(p["entry_date"]), "Exit": pd.NaT,
                         "Qty": _num(p["qty_open"]), "Avg": avg, "Price": _num(last.get(p["id"])), "Stop0": st0})
    d = pd.DataFrame(rows)
    if d.empty:
        return d
    d["Cost"] = d["Avg"] * d["Qty"]
    d["P&L"] = (d["Price"] - d["Avg"]) * d["Qty"]
    d["Return %"] = (d["Price"] / d["Avg"] - 1) * 100
    d["R"] = np.where(d["Stop0"] < d["Avg"], (d["Price"] - d["Avg"]) / (d["Avg"] - d["Stop0"]), np.nan)
    end = d["Exit"].fillna(pd.Timestamp(date.today()))
    d["Days"] = [int(np.busday_count(a.date(), b.date())) + 1 for a, b in zip(d["Entry"], end)]
    return d


TONE = {"good": ("rgba(40,167,69,.16)", "#28a745"), "warn": ("rgba(255,193,7,.16)", "#ffc107"),
        "bad": ("rgba(220,53,69,.16)", "#dc3545"), "info": ("rgba(13,110,253,.12)", "#3d8bfd"),
        "muted": ("rgba(128,128,128,.10)", "#8a8f98"), "na": ("rgba(128,128,128,.06)", "#5c6370")}


def _tiles_html(groups):
    """Material-style tiles: coloured left edge + tint by tone, grouped under small headings."""
    import html as _h
    css = ("<style>.pt-g{font-size:12px;letter-spacing:.06em;text-transform:uppercase;opacity:.65;margin:14px 0 6px}"
           ".pt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}"
           ".pt{border-radius:10px;padding:10px 12px;box-shadow:0 1px 3px rgba(0,0,0,.25)}"
           ".pt-l{font-size:12px;opacity:.75}.pt-v{font-size:22px;font-weight:700;line-height:1.25;margin-top:2px}"
           ".pt-s{font-size:11px;opacity:.65;margin-top:2px}</style>")
    out = [css]
    for title, tiles in groups:
        out.append(f"<div class='pt-g'>{_h.escape(title)}</div><div class='pt-grid'>")
        for label, value, sub, tone_, tip in tiles:
            bg, edge = TONE.get(tone_, TONE["na"])
            col = edge if tone_ in ("good", "bad", "warn") else "inherit"
            out.append(f"<div class='pt' title='{_h.escape(tip or '')}' style='background:{bg};border-left:4px solid {edge}'>"
                       f"<div class='pt-l'>{_h.escape(label)}</div><div class='pt-v' style='color:{col}'>{_h.escape(str(value))}</div>"
                       + (f"<div class='pt-s'>{_h.escape(sub)}</div>" if sub else "") + "</div>")
        out.append("</div>")
    return "".join(out)


def _render_stats(st, sb, mcfg, open_v, eq):
    """📊 Performance — the trades-sheet dashboard, from your positions (every lot: closed sales and open holdings)."""
    mkt = mcfg["market"]
    d = _lots(sb, mcfg, open_v)
    with st.expander("📊 Performance — win rate, profit factor, R, ROI"):
        if d.empty or not d["Qty"].fillna(0).sum():
            st.caption("Needs positions with quantities. Every sale (partial or full) and every open holding counts as a trade, "
                       "like your trades sheet.")
            return
        c1, c2, c3 = st.columns([1, 1, 2])
        per = c1.selectbox("Period", ["All", "This year", "Last 90 days", "Last 30 days"], key=f"pos_per_{mkt}")
        cost_pct = c2.number_input("Costs per side %", 0.0, 2.0, 0.17 if mkt == "NSE" else 0.02, 0.01, key=f"pos_cost_{mkt}",
                                   help="Brokerage + charges per buy or sell, as % of the trade value")
        tax_pct = c3.number_input("Tax on realised profit %", 0.0, 50.0, 15.0 if mkt == "NSE" else 0.0, 1.0, key=f"pos_tax_{mkt}",
                                  help="E.g. 15% short-term capital gains (STT row in your sheet). 0 to ignore.")
        start = {"This year": pd.Timestamp(date.today().year, 1, 1), "Last 90 days": pd.Timestamp(date.today()) - pd.Timedelta(days=90),
                 "Last 30 days": pd.Timestamp(date.today()) - pd.Timedelta(days=30)}.get(per)
        if start is not None:
            d = d[(d["Exit"].fillna(pd.Timestamp(date.today())) >= start)]
        d = d[d["Price"].notna()]
        if d.empty:
            st.caption("No trades in that period."); return
        win, loss = d[d["P&L"] >= 0], d[d["P&L"] < 0]
        rel, unr = d[d["Status"] == "Closed"], d[d["Status"] == "Open"]
        n, nw, nl = len(d), len(win), len(loss)
        gp_, gl = win["P&L"].sum(), loss["P&L"].sum()
        avg_w, avg_l = (gp_ / nw if nw else 0.0), (gl / nl if nl else 0.0)
        payoff = avg_w / abs(avg_l) if avg_l else np.nan
        wr = nw / n
        net = gp_ + gl
        net_rel = rel["P&L"].sum()
        costs = n * d["Cost"].mean() * cost_pct / 100 * 2
        tax = max(net_rel, 0) * tax_pct / 100
        net_after = net - costs - tax
        deployed = unr["Cost"].sum()
        kelly = 100 * (wr - (1 - wr) / payoff) if payoff and np.isfinite(payoff) else np.nan
        rr = (win["P&L"] / abs(avg_l)).mean() if nl and nw else np.nan
        fm = lambda x: f"{x:,.0f}" if np.isfinite(_num(x)) else "–"
        sgn = lambda x: f"{x:+,.0f}" if np.isfinite(_num(x)) else "–"
        pf = abs(gp_ / gl) if gl else np.nan
        avg_r = d["R"].mean() if d["R"].notna().any() else np.nan
        stop_out = 100 * abs(avg_l) / d["Cost"].mean() if nl and d["Cost"].mean() else np.nan
        dw, dl = (win["Days"].mean() if nw else np.nan), (loss["Days"].mean() if nl else np.nan)
        exp_ = net / n

        def tone(x, good, ok, higher=True):
            x = _num(x)
            if not np.isfinite(x):
                return "na"
            if not higher:
                x, good, ok = -x, -good, -ok
            return "good" if x >= good else "warn" if x >= ok else "bad"
        pos = lambda x: "na" if not np.isfinite(_num(x)) else ("good" if x > 0 else "bad" if x < 0 else "na")

        groups = [
            ("Edge", [
                ("Win %", f"{100 * wr:.0f}%", f"{nw} won · {nl} lost of {n}", tone(100 * wr, 50, 35), "Winners ÷ all trades"),
                ("Profit factor", f"{pf:.2f}" if np.isfinite(pf) else "–", "gross profit ÷ gross loss", tone(pf, 2, 1.2),
                 "Gross profit ÷ |gross loss| (realised + unrealised). ≥ 2 strong · < 1.2 weak"),
                ("Payoff", f"{payoff:.2f}" if np.isfinite(payoff) else "–", "avg win ÷ avg loss", tone(payoff, 2, 1),
                 "How much a typical win is worth vs a typical loss. ≥ 2 lets you win under 40% of trades"),
                ("Expectancy / trade", sgn(exp_), "net ÷ trades", pos(exp_), "What each trade earns on average"),
                ("Avg R", f"{avg_r:+.2f}" if np.isfinite(avg_r) else "–", "vs initial stop", tone(avg_r, 0.5, 0),
                 "Average result in multiples of the initial risk. Needs initial stops"),
                ("Kelly size", f"{kelly:.1f}%" if np.isfinite(kelly) else "–", "keep well below · ≤ 25%",
                 "info", "Win% − (1 − Win%) ÷ payoff"),
            ]),
            ("Wins & losses", [
                ("Avg win", sgn(avg_w), None, "good" if nw else "na", None),
                ("Avg loss", sgn(avg_l) if nl else "–", None, "bad" if nl else "na", None),
                ("Avg RR (winners)", f"{rr:.2f}" if np.isfinite(rr) else "–", "winner ÷ avg loss", tone(rr, 2, 1),
                 "Each winner ÷ the average loss, averaged — as in your sheet"),
                ("Largest win", sgn(d["P&L"].max()) if nw else "–", None, "good" if nw else "na", None),
                ("Largest loss", sgn(d["P&L"].min()) if nl else "–", None, "bad" if nl else "na", None),
                ("Win / loss ratio", f"{nw / nl:.2f}" if nl else "–", "winners ÷ losers (count)", tone(nw / nl if nl else np.nan, 1, 0.6), None),
            ]),
            ("Profit", [
                ("Net after costs + tax", sgn(net_after), f"net {sgn(net)} − costs {fm(costs)} − tax {fm(tax)}", pos(net_after), None),
                ("Net realised", sgn(net_rel), "closed lots only", pos(net_rel), None),
                ("Gross profit", fm(gp_), f"realised {fm(rel[rel['P&L'] >= 0]['P&L'].sum())} · open {fm(unr[unr['P&L'] >= 0]['P&L'].sum())}",
                 "good" if gp_ > 0 else "na", None),
                ("Gross loss", fm(gl), f"realised {fm(rel[rel['P&L'] < 0]['P&L'].sum())} · open {fm(unr[unr['P&L'] < 0]['P&L'].sum())}",
                 "bad" if gl < 0 else "na", None),
                ("Costs (est.)", fm(costs), f"{cost_pct:g}% × 2 sides", "muted", "Trades × avg trade size × cost % × 2"),
                ("Tax (est.)", fm(tax), f"{tax_pct:g}% of realised profit", "muted", None),
            ]),
            ("Capital & habits", [
                ("Capital deployed", fm(deployed), f"{100 * deployed / eq:.0f}% of account" if eq else "cost of open holdings", "info", None),
                ("Avg trade size", fm(d["Cost"].mean()), f"{100 * d['Cost'].mean() / eq:.1f}% of account" if eq else None, "info", None),
                ("Avg days held", f"{d['Days'].mean():.1f}", f"winners {dw:.1f} · losers {dl:.1f}" if nw and nl else None,
                 ("good" if dw > dl else "warn") if nw and nl else "na", "Winners should be held longer than losers"),
                ("Avg stop-out", f"{stop_out:.2f}%" if np.isfinite(stop_out) else "–", "avg loss ÷ avg trade size",
                 tone(stop_out, 5, 8, higher=False), "≤ 5% tight · > 8% loose"),
            ] + ([
                ("ROI on account", f"{100 * net_after / eq:+.2f}%", "net after costs ÷ account", pos(net_after), None),
                ("Projected account", fm(eq + net_after), f"account {fm(eq)}", "info", None),
                ("Projected cash", fm(eq + net_after - deployed), "projected − deployed", "info", None),
                ("Cumulative ROI", f"{100 * net_after / (eq + net_after):+.2f}%", "on projected account", pos(net_after), "As in your sheet"),
            ] if eq else [("ROI on account", "–", "set Account size above", "na", None)])),
        ]
        st.markdown(_tiles_html(groups), unsafe_allow_html=True)
        st.caption("Every lot counts as a trade, like your trades sheet: a partial sale closes those shares as their own trade, "
                   "the rest stays open. Open holdings count at today's price (unrealised).")


def _render_closed(st, sb, mcfg):
    try:
        closed = load_positions(sb, mcfg, "closed")
    except Exception:
        return
    if not closed:
        return
    ex = pd.DataFrame(load_exits(sb, [p["id"] for p in closed]))
    rows = []
    for p in closed:
        e = ex[ex["position_id"] == p["id"]] if not ex.empty else pd.DataFrame()
        q = pd.to_numeric(e.get("qty"), errors="coerce").fillna(0) if len(e) else pd.Series(dtype=float)
        pr = pd.to_numeric(e.get("price"), errors="coerce") if len(e) else pd.Series(dtype=float)
        avg_exit = float((pr * q).sum() / q.sum()) if len(e) and q.sum() > 0 else (float(pr.mean()) if len(e) else np.nan)
        avg, st0 = _num(p["avg_price"]), _num(p.get("init_stop"))
        ret = (avg_exit / avg - 1) * 100 if avg and np.isfinite(avg_exit) else np.nan
        r_ = (avg_exit - avg) / (avg - st0) if np.isfinite(st0) and avg > st0 and np.isfinite(avg_exit) else np.nan
        days = int(np.busday_count(pd.Timestamp(p["entry_date"]).date(), pd.Timestamp(p.get("closed_at") or date.today()).date())) + 1
        rows.append({"Symbol": p["symbol"], "Entry": str(p["entry_date"])[:10], "Closed": str(p.get("closed_at") or "")[:10],
                     "Days": days, "Avg": avg, "Avg exit": avg_exit, "Return %": ret, "R": r_, "Setup": p.get("setup"),
                     "Sector": p.get("sector"), "Note": p.get("note") or ""})
    d = pd.DataFrame(rows).sort_values("Closed", ascending=False)
    ret = pd.to_numeric(d["Return %"], errors="coerce")
    win = ret[ret > 0]; loss = ret[ret <= 0]
    with st.expander(f"📒 Closed trades ({len(d)}) · win rate {100 * len(win) / max(len(ret.dropna()), 1):.0f}% · "
                     f"avg win {win.mean() if len(win) else 0:+.1f}% · avg loss {loss.mean() if len(loss) else 0:+.1f}%"):
        st.dataframe(_style(d), hide_index=True, use_container_width=True)
