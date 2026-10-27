"""
Positions tab — the open trades you're managing.

Prices come from Yahoo Finance daily candles (about 15 min delayed, NSE + US), so a position keeps its warnings even
after it drops out of the scan — the weakening ones are exactly the ones that drop out. If Yahoo fails, today's scan
numbers are used where the stock is still in it, and the row says so.

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
LEVEL = {"🔴": 0, "🟠": 1, "🟡": 2, "🟢": 3}


def _num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else np.nan
    except Exception:
        return np.nan


# ── data ───────────────────────────────────────────────────────────────────
def yahoo_symbol(sym, mcfg):
    s = str(sym).upper().split(":")[-1].strip()
    if mcfg.get("market") == "NSE":
        return s.replace("&", "%26") + ".NS"
    return s.replace(".", "-")


@_st.cache_data(ttl=600, show_spinner=False)
def fetch_daily(ysym):
    """~1 year of daily candles from Yahoo: DataFrame (date index) o h l c v, or None."""
    for host in ("query1", "query2"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{ysym}?range=1y&interval=1d",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            res = (r.json().get("chart") or {}).get("result") or []
            if not res:
                continue
            j = res[0]
            q = j["indicators"]["quote"][0]
            idx = pd.to_datetime(j["timestamp"], unit="s") + pd.Timedelta(seconds=int(j["meta"].get("gmtoffset") or 0))
            df = pd.DataFrame({"o": q["open"], "h": q["high"], "l": q["low"], "c": q["close"], "v": q["volume"]},
                              index=idx.normalize()).dropna(subset=["c"])
            df = df[~df.index.duplicated(keep="last")].astype(float)
            if len(df) >= 25:
                return df
        except Exception:
            continue
    return None


def metrics(df, entry_date, avg, stop):
    """Everything the warnings need, from daily candles."""
    c, h, l, v = df["c"], df["h"], df["l"], df["v"]
    last, prev = c.iloc[-1], c.iloc[-2]
    adr = ((h / l - 1) * 100).rolling(20).mean().iloc[-1]
    ma = {n: c.rolling(n).mean().iloc[-1] for n in (10, 20, 50)}
    wk = c.resample("W-FRI").last().dropna()
    ma10w = wk.rolling(10).mean().iloc[-1] if len(wk) >= 10 else np.nan
    avgv = v.iloc[-51:-1].mean()
    up = 0
    for a, b in zip(c.iloc[::-1].values[:-1], c.iloc[::-1].values[1:]):
        if a > b:
            up += 1
        else:
            break
    held = df[df.index >= pd.Timestamp(entry_date)]
    dist = lambda x: (last / x - 1) * 100 if x and np.isfinite(x) else np.nan
    m = {"Last": last, "Chg %": (last / prev - 1) * 100, "ADR": adr,
         "10MA": ma[10], "20MA": ma[20], "50MA": ma[50],
         "10MA %": dist(ma[10]), "20MA %": dist(ma[20]), "50MA %": dist(ma[50]),
         "10w %": dist(ma10w), "Vol x": v.iloc[-1] / avgv if avgv else np.nan,
         "NR4 %": (h.iloc[-4:].max() / l.iloc[-4:].min() - 1) * 100, "Up days": up,
         "Days held": max(len(held), 0), "High since entry": held["h"].max() if len(held) else np.nan,
         "Off 52W hi %": (h.max() / last - 1) * 100, "Bar": df.index[-1].date()}
    for k in ("10MA", "20MA", "50MA", "10w"):
        m[f"{k} (ADR)"] = m[f"{k} %"] / adr if adr else np.nan
    m["Rel tight"] = m["NR4 %"] / adr if adr else np.nan
    m["Return %"] = (last / avg - 1) * 100 if avg else np.nan
    m["R"] = (last - avg) / (avg - stop) if stop and avg and avg > stop else np.nan
    return m


def from_scan(row):
    """Fallback when Yahoo has nothing: the same measures from today's scan row (no days-held / up-days)."""
    adr = _num(row.get("Adr"))
    m = {"Last": _num(row.get("Last")), "Chg %": _num(row.get("_chg_percentclose")), "ADR": adr,
         "10MA %": _num(row.get("_10madist")), "20MA %": _num(row.get("_20madist")), "10w %": _num(row.get("W_Dist10wMA")),
         "Vol x": _num(row.get("_vol_ratio")), "NR4 %": _num(row.get("_nr4")), "Off 52W hi %": _num(row.get("off_high_252"))}
    for k in ("10MA", "20MA", "10w"):
        m[f"{k} (ADR)"] = m[f"{k} %"] / adr if adr else np.nan
    m["Rel tight"] = m["NR4 %"] / adr if adr else np.nan
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
    if 3 <= d <= 5 and _num(m.get("Return %")) > 0 and n_exits == 0:
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
    fmt.update({c: "{:.1f}" for c in ("ADR", "Vol x", "Off 52W hi %")})
    sty = v.style.format({k: f for k, f in fmt.items() if k in v.columns}, na_rep="–")
    for c, f in (("Return %", _c_ret), ("Status", _c_status), ("Chg %", gp._c_chg), ("Vol x", gp._c_vol),
                 ("10MA (ADR)", ff._c_ma10), ("Rel tight", ff._hl("tight")), ("ADR", ff._hl("adr")),
                 ("10w (ADR)", ff._hl("relwk"))):
        if c in v.columns:
            sty = sty.map(f, subset=[c])
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
    st.caption("Your open trades, checked every time the page loads: prices from Yahoo Finance daily candles (≈15 min "
               "delayed), so warnings keep working after a stock drops out of the scan. 🔴 act now · 🟠 tighten / trim · "
               "🟡 heads-up · 🟢 fine.")
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

    if not open_:
        st.info("No open positions."); _render_closed(st, sb, mcfg); return

    # ── evaluate ──
    live = gp.add_derived(base_df).drop_duplicates("Symbol").set_index("Symbol") if base_df is not None and not base_df.empty else None
    view = st.session_state.get(f"sf_view_{mkt}")
    vrow = view.set_index("Symbol") if view is not None and not view.empty else None
    exits = pd.DataFrame(load_exits(sb, [p["id"] for p in open_]))
    rows, notes, yahoo_fail = [], {}, []
    with st.spinner("Checking your positions…"):
        for p in open_:
            sym = p["symbol"]
            in_scan = live is not None and sym in live.index
            df = fetch_daily(yahoo_symbol(sym, mcfg))
            if df is not None:
                m, src = metrics(df, p["entry_date"], _num(p["avg_price"]), _num(p.get("init_stop") or p.get("stop"))), f"Yahoo {df.index[-1]:%d-%b}"
            elif in_scan:
                m, src = from_scan(live.loc[sym]), "scan (Yahoo failed)"
                m["Return %"] = (m["Last"] / _num(p["avg_price"]) - 1) * 100
                m["Days held"] = int(np.busday_count(pd.Timestamp(p["entry_date"]).date(), date.today())) + 1
                yahoo_fail.append(sym)
            else:
                m, src = {}, "no data"
                yahoo_fail.append(sym)
            ex = exits[exits["position_id"] == p["id"]] if not exits.empty else pd.DataFrame()
            w, plan = assess(m, p, len(ex), in_scan) if m else ([("🟠", "No price data — check it in TradingView")], "Check the chart")
            status = w[0][0] if w else "🟢"
            notes[sym] = w
            realised = float(((pd.to_numeric(ex["price"]) - _num(p["avg_price"])) * pd.to_numeric(ex["qty"])).sum()) if len(ex) else 0.0
            vr = vrow.loc[sym] if vrow is not None and sym in vrow.index else {}
            rows.append({"id": p["id"], "Symbol": sym, "Status": status + (f" {len(w)}" if len(w) > 1 else ""), "Plan": plan,
                         "Return %": m.get("Return %"), "R": m.get("R"), "Days": m.get("Days held"),
                         "Last": m.get("Last"), "Avg": _num(p["avg_price"]), "Stop": _num(p.get("stop")),
                         "Qty open": _num(p.get("qty_open")), "Chg %": m.get("Chg %"), "Vol x": m.get("Vol x"),
                         "10MA (ADR)": m.get("10MA (ADR)"), "20MA (ADR)": m.get("20MA (ADR)"), "10w (ADR)": m.get("10w (ADR)"),
                         "Rel tight": m.get("Rel tight"), "ADR": m.get("ADR"), "Up days": m.get("Up days"),
                         "Off 52W hi %": m.get("Off 52W hi %"), "Sector": p.get("sector") or (vr.get("Sector") if len(vr) else None),
                         "Setup": p.get("setup"), "👑": vr.get("Leader") if len(vr) else "", "MA hold": vr.get("MA hold") if len(vr) else "",
                         "In scan": "yes" if in_scan else "no", "Entry": str(p["entry_date"])[:10],
                         "Warnings": " · ".join(f"{a} {b}" for a, b in w), "Data": src, "Note": p.get("note") or "",
                         "Sell qty": 0.0, "Sell price": 0.0, "Close": False, "Delete": False,
                         "_realised": realised, "_pnl": (m.get("Last", np.nan) - _num(p["avg_price"])) * _num(p.get("qty_open"))})
    v = pd.DataFrame(rows)
    v = v.assign(_o=v["Status"].str[:1].map(LEVEL)).sort_values(["_o", "Return %"], ascending=[True, False]).drop(columns="_o") \
         .reset_index(drop=True)

    # ── summary ──
    n_red = int(v["Status"].str.startswith("🔴").sum()); n_or = int(v["Status"].str.startswith("🟠").sum())
    c = st.columns(5)
    c[0].metric("Open", len(v))
    c[1].metric("🔴 Act now", n_red)
    c[2].metric("🟠 Tighten / trim", n_or)
    c[3].metric("Avg return", f"{pd.to_numeric(v['Return %'], errors='coerce').mean():+.1f}%")
    pnl = pd.to_numeric(v["_pnl"], errors="coerce").sum() + v["_realised"].sum()
    c[4].metric("P&L (open + booked)", f"{pnl:,.0f}" if v["Qty open"].fillna(0).sum() else "–",
                help="Needs quantities. Booked = partial sells recorded below.")
    if yahoo_fail:
        st.warning(f"Yahoo Finance had no data for {', '.join(yahoo_fail)} — those rows use today's scan where possible.")
    for _, r in v[v["Status"].str.startswith("🔴")].iterrows():
        st.error(f"**{r['Symbol']}** — {r['Warnings']}")

    # ── table ──
    shown = [c for c in v.columns if not c.startswith("_") and c != "id"]
    ed, saved = gp.card_grid(st, v[shown], key=f"pos_grid_{mkt}", sty=_style(v[shown]),
                             edit={"Stop": "number", "Note": "text", "Sell qty": "number", "Sell price": "number",
                                   "Close": "bool", "Delete": "bool"},
                             submit="Save positions", height=min(32 * len(v) + 90, 560),
                             widths={"Symbol": 105, "Status": 62, "Plan": 330, "Warnings": 420, "Sector": 150, "Note": 200})
    st.caption("Click a ticker for the full card · type a new **Stop** to trail it · record a partial sale with **Sell qty** + "
               "**Sell price** · **Close** sells what's left (at Sell price, or the last price if blank) · **Delete** = "
               "entered by mistake. Then **Save positions**.")
    if saved and ed is not None and "Symbol" in ed.columns:
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
                price = sp if np.isfinite(sp) and sp > 0 else _num(o["Last"])
                sb.table("position_exits").insert({"position_id": pid, "qty": q_open, "price": price,
                                                   "note": "closed"}).execute()
                upd.update(status="closed", closed_at=date.today().isoformat(), qty_open=0)
                msgs.append(f"closed {s} @ {price:g}")
            elif sq > 0 and np.isfinite(sp) and sp > 0:
                sb.table("position_exits").insert({"position_id": pid, "qty": sq, "price": sp, "note": "partial"}).execute()
                upd["qty_open"] = max(q_open - sq, 0)
                if q_open and q_open - sq <= 0:
                    upd.update(status="closed", closed_at=date.today().isoformat())
                msgs.append(f"sold {sq:g} {s} @ {sp:g}")
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
    _render_closed(st, sb, mcfg)


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
