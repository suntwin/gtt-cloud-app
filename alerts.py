"""
Alerts tab: your active TradingView alerts, copied into Supabase by sync_tv_alerts.py (runs on your PC).

Shows every alert with the line's price now, distance from the last close, triggered or waiting, days to expiry
and which Shortlister lists the name is on. Also: Focus names with no alert yet, and alerts on names that
are on no list today (candidates to delete).
"""
import datetime as dt
import math

import numpy as np
import pandas as pd

import gtt_process as gp
import market_calendar as mcal

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

SESSION_MIN = {"NSE": 375, "USA": 390, "CRYPTO": 1440}       # minutes per session, for counting intraday bars
LIST_LABELS = {"FOCUS": "Picked", "FLAGGED": "Flagged", "ACTION": "Action", "RECENT": "BO→tight", "3WTC": "3WTC", "HTF": "HTF"}
TZ = {"NSE": "Asia/Kolkata", "USA": "America/New_York", "CRYPTO": "UTC"}
TV_LIMIT = 100                      # TradingView plan: max active alerts (all markets together)
FOCUS_TARGET = 15                   # suggested waiting alerts per market: only the best breakout candidates
SHOW = ["Priority", "Symbol", "TF", "Line", "Alert at", "Last", "To alert %", "Status", "Set d ago", "Expires in d", "Lists", "Note"]


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else np.nan
    except Exception:
        return np.nan


def _ts(x):
    """Timestamp (UTC) or None for blanks / NaN / junk."""
    if x is None or (isinstance(x, float) and not math.isfinite(x)) or (isinstance(x, str) and not x.strip()):
        return None
    try:
        t = pd.to_datetime(x, utc=True)
        return None if pd.isna(t) else t
    except Exception:
        return None


def _day(t):
    return t.strftime("%d-%b") if t is not None else "?"


# ── data ───────────────────────────────────────────────────────────────────
def load_alerts(sb):
    rows = sb.table("tv_alerts").select("*").execute().data or []
    return pd.DataFrame(rows)


def load_syncs(sb, n=5):
    return sb.table("tv_alert_sync").select("*").order("synced_at", desc=True).limit(n).execute().data or []


def is_triggered(r):
    """A one-shot alert that has fired is done. Repeating alerts keep waiting."""
    freq = r.get("frequency")
    freq = freq if isinstance(freq, str) and freq else "on_first_fire"
    return _ts(r.get("last_fired")) is not None and freq == "on_first_fire"


def waiting_tickers(sb, market):
    """Tickers in this market with an alert that hasn't fired yet (fills the Shortlister's 'Alert set')."""
    try:
        df = load_alerts(sb)
    except Exception:
        return set()
    if df.empty:
        return set()
    df = df[df["market"] == market]
    return {str(r["ticker"]).upper() for r in df.to_dict("records") if r.get("active", True) and not is_triggered(r)}


# ── where is a trendline today? ───────────────────────────────────────────
def _tf(res):
    s = str(res or "").upper()
    if s.isdigit():
        m = int(s)
        return ("m", m)
    if s in ("D", "1D"): return ("D", 1)
    if s in ("W", "1W"): return ("W", 1)
    if s in ("M", "1M"): return ("M", 1)
    return ("?", 0)


def _tf_label(res):
    k, n = _tf(res)
    if k == "m":
        return f"{n // 60}h" if n % 60 == 0 else f"{n}m"
    return {"D": "D", "W": "W", "M": "M"}.get(k, str(res or ""))


def _trading_days_between(market, d0, d1):
    """Sessions after d0 up to and including d1."""
    if d1 <= d0:
        return 0
    if market == "CRYPTO":
        return (d1 - d0).days
    n, d = 0, d0 + dt.timedelta(days=1)
    while d <= d1:
        if mcal.is_trading_day(market, d):
            n += 1
        d += dt.timedelta(days=1)
    return n


def _bars_since(base, market, res, now):
    k, n = _tf(res)
    tz = ZoneInfo(TZ.get(market, "UTC")) if ZoneInfo else None
    b = base.tz_convert(tz) if tz else base
    t = now.astimezone(tz) if tz else now
    if k == "W":
        return (t.date() - b.date()).days / 7.0
    if k == "M":
        return (t.year - b.year) * 12 + (t.month - b.month)
    days = _trading_days_between(market, b.date(), t.date())
    if k == "D":
        return days
    if k == "m" and n:
        return days * math.ceil(SESSION_MIN.get(market, 390) / n)
    return np.nan


def line_now(r, now=None):
    """Horizontal line / price alert → its price. Trendline → its value at the latest bar (≈, bar count estimated)."""
    p1, p2 = _num(r.get("price1")), _num(r.get("price2"))
    if str(r.get("line_tool") or "") != "TrendLine" or not np.isfinite(p1) or not np.isfinite(p2) or p1 == p2:
        return p2 if np.isfinite(p2) else p1
    try:
        line = next(s for s in (r.get("raw") or {}).get("condition", {}).get("series", []) if s.get("type") == "line")
        o1, o2, base = _num(line.get("offset1")), _num(line.get("offset2")), _ts(line.get("base_time"))
        if base is None or not np.isfinite(o1) or not np.isfinite(o2) or o1 == o2:
            return p2
        bars = _bars_since(base, r.get("market"), r.get("resolution"), now or dt.datetime.now(dt.timezone.utc))
        if not np.isfinite(bars):
            return p2
        return p1 + (bars - o1) * (p2 - p1) / (o2 - o1)
    except Exception:
        return p2


# ── table ─────────────────────────────────────────────────────────────────
def build_table(df, base_df, lists, market_filter=None):
    if df is None or df.empty:
        return pd.DataFrame(columns=SHOW + ["_trig", "_adr", "_fired", "_mkt"])
    now = dt.datetime.now(dt.timezone.utc)
    last = adr = {}
    if base_df is not None and not getattr(base_df, "empty", True) and "Symbol" in base_df.columns:
        b = base_df.drop_duplicates("Symbol").set_index(base_df.drop_duplicates("Symbol")["Symbol"].astype(str).str.upper())
        last = pd.to_numeric(b.get("Last"), errors="coerce").to_dict() if "Last" in b else {}
        adr = pd.to_numeric(b.get("Adr"), errors="coerce").to_dict() if "Adr" in b else {}
    on = {}
    for key, syms in (lists or {}).items():
        for s in syms:
            on.setdefault(str(s).upper(), []).append(LIST_LABELS.get(key, key))
    out = []
    for r in df.to_dict("records"):
        if isinstance(r.get("raw"), str):
            try:
                import json; r["raw"] = json.loads(r["raw"])
            except Exception:
                r["raw"] = {}
        if market_filter and r.get("market") not in market_filter:
            continue
        tk = str(r.get("ticker") or "").upper()
        lvl = line_now(r, now)
        lp = _num(last.get(tk)) if r.get("market") in ("NSE", "USA") else np.nan
        trig = is_triggered(r)
        fired, cre, exp = _ts(r.get("last_fired")), _ts(r.get("created")), _ts(r.get("expiration"))
        tool = r.get("line_tool") if isinstance(r.get("line_tool"), str) else ""
        line = {"HorzLine": "Horizontal", "TrendLine": "Trendline ≈"}.get(tool, tool or str(r.get("alert_type") or "Price").title())
        if trig:
            status = f"Triggered {_day(fired)}"
        elif fired is not None:
            status = f"Waiting (fired {_day(fired)})"
        else:
            status = "Waiting"
        out.append({
            "Symbol": tk if r.get("market") in ("NSE", "USA") else str(r.get("symbol")),
            "TF": _tf_label(r.get("resolution")), "Line": line,
            "Alert at": round(lvl, 4 if np.isfinite(lvl) and abs(lvl) < 10 else 2) if np.isfinite(lvl) else np.nan,
            "Last": lp, "To alert %": round((lvl / lp - 1) * 100, 1) if np.isfinite(lp) and lp and np.isfinite(lvl) else np.nan,
            "Status": status,
            "Set d ago": (now - cre).days if cre is not None else np.nan,
            "Expires in d": (exp - now).days if exp is not None else np.nan,
            "Lists": " · ".join(on.get(tk, [])), "Note": r.get("message") if isinstance(r.get("message"), str) else "",
            "_trig": trig, "_adr": _num(adr.get(tk)), "_fired": fired, "_mkt": r.get("market"),
            "_tv": r.get("symbol"), "_id": r.get("alert_id"),
        })
    t = pd.DataFrame(out)
    if t.empty:
        return t
    t["_d"] = pd.to_numeric(t["To alert %"], errors="coerce").abs()
    t["_dadr"] = [d / a if np.isfinite(_num(a)) and _num(a) else d / 3.0 for d, a in zip(t["_d"], t["_adr"])]
    pr, rank = [], []
    for trig, ls, da in zip(t["_trig"], t["Lists"], t["_dadr"]):
        if trig:
            p = ("Delete · triggered", 9)
        elif not lists:
            p = ("—", 5)
        elif "Picked" in ls and (not np.isfinite(da) or da <= 1):
            p = ("A · keep", 1)
        elif ls:
            p = ("B · keep", 2) if "Picked" in ls or (np.isfinite(da) and da <= 2) else ("B · far", 3)
        else:
            p = ("C · cut?", 4)
        pr.append(p[0]); rank.append(p[1])
    t["Priority"], t["_rank"] = pr, rank
    w = t[~t["_trig"]].sort_values(["_rank", "_d", "Symbol"], na_position="last")
    f = t[t["_trig"]].sort_values("_fired", ascending=False)
    return pd.concat([w, f], ignore_index=True)


def _css(bg, fg="black", bold=False):
    return f"background-color:{bg};color:{fg}" + (";font-weight:bold" if bold else "")


def style_alerts(v, adr):
    sty = v.style.format({"Alert at": "{:g}", "Last": "{:g}", "To alert %": "{:+.1f}"}, na_rep="")

    def dist(col):
        res = []
        for x, a in zip(col, adr):
            x, a = _num(x), _num(a)
            if not np.isfinite(x):
                res.append(""); continue
            d = abs(x) / a if np.isfinite(a) and a else abs(x) / 3.0
            res.append(_css("#28a745", "white", True) if d <= 0.5 else _css("#8ee68e") if d <= 1 else
                       _css("#d4edda") if d <= 2 else "")
        return res

    def status(x):
        s = str(x)
        return _css("#e2e3e5", "#41464b") if s.startswith("Triggered") else _css("#cfe2ff", "#084298")

    def expiry(x):
        x = _num(x)
        return _css("#f8d7da", "#721c24", True) if np.isfinite(x) and x <= 2 else \
            _css("#fff3cd", "#664d03") if np.isfinite(x) and x <= 5 else ""

    def lists(x):
        s = str(x)
        return _css("#28a745", "white", True) if "Picked" in s else (_css("#d4edda") if s else "")

    if "To alert %" in v.columns:
        sty = sty.apply(dist, subset=["To alert %"])
    def prio(x):
        s = str(x)
        return (_css("#28a745", "white", True) if s.startswith("A") else _css("#d4edda") if s == "B · keep" else
                _css("#fff3cd", "#664d03") if s in ("B · far", "C · cut?") else
                _css("#f8d7da", "#721c24") if s.startswith("Delete") else "")

    for c, f in [("Priority", prio), ("Status", status), ("Expires in d", expiry), ("Lists", lists)]:
        if c in v.columns:
            sty = sty.map(f, subset=[c])
    return sty


# ── tab ───────────────────────────────────────────────────────────────────
def render_alerts_tab(st, sb, base_df, mcfg):
    mkt = mcfg.get("market")
    st.subheader("Alert management")
    if sb is None:
        st.info("Supabase isn't connected."); return
    c1, c2 = st.columns([4, 1])
    with c2:
        if st.button("Refresh", key=f"al_refresh_{mkt}"):
            gp.clear_db_cache()
    try:
        df, syncs = load_alerts(sb), load_syncs(sb)
    except Exception as e:
        st.error(f"Run supabase_tv_alerts.sql once in Supabase, then run sync_tv_alerts.bat on your PC. ({e})")
        return
    with c1:
        if not syncs:
            st.warning("No sync yet. On your PC: open TradingView in debug mode, then run sync_tv_alerts.bat.")
        else:
            s0 = syncs[0]
            t0 = _ts(s0.get("synced_at"))
            age_h = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds() / 3600 if t0 is not None else np.nan
            tz = ZoneInfo(mcfg.get("tz", "UTC")) if ZoneInfo else None
            when = t0.tz_convert(tz).strftime("%a %d-%b %H:%M") if t0 is not None and tz else str(s0.get("synced_at"))
            ok_last = next((s for s in syncs if s.get("ok")), None)
            if not s0.get("ok"):
                st.warning(f"Last sync failed {when}: {s0.get('error')}"
                           + (f" — showing the sync from {_day(_ts(ok_last['synced_at']))}" if ok_last else ""))
            elif np.isfinite(age_h) and age_h > 24:
                st.warning(f"Last synced {when} ({age_h / 24:.0f} days ago). Run sync_tv_alerts.bat after setting alerts.")
            else:
                st.caption(f"Last synced **{when}** · {s0.get('n_alerts')} alerts in TradingView")

    total = len(df)
    by_m = df["market"].value_counts().to_dict() if total and "market" in df.columns else {}
    n_trig_all = sum(1 for r in df.to_dict("records") if is_triggered(r)) if total else 0
    st.progress(min(total / TV_LIMIT, 1.0),
                text=f"{total} / {TV_LIMIT} alerts used (TradingView limit, all markets) · "
                     + " · ".join(f"{k} {v}" for k, v in sorted(by_m.items()))
                     + (f" · {n_trig_all} already triggered — delete them" if n_trig_all else ""))
    if total >= 0.8 * TV_LIMIT:
        st.warning(f"{total} of {TV_LIMIT} alerts used. Delete triggered and C-priority alerts before adding new ones.")
    st.info(f"**Fewer alerts, better focus.** The limit is {TV_LIMIT}, but aim for about {FOCUS_TARGET} waiting per market: "
            "only names with the highest odds of breaking out — your Picks in Sector Focus, within 1 ADR of the pivot, volume "
            "already came in. Priority **A** = Picked and within 1 ADR (keep) · **B** = Flagged (keep if close) · "
            "**C** = on no list today (cut unless you hold it) · **Delete** = already triggered.")
    lists = st.session_state.get(f"sl_lists_{mkt}", {})
    opts = [mkt] + [m for m in ("NSE", "USA") if m != mkt] + ["Crypto", "All markets"]   # this page's market first
    scope = st.radio("Show", opts, horizontal=True, key=f"al_scope_{mkt}")
    mf = {"NSE": ["NSE"], "USA": ["USA"], "Crypto": ["CRYPTO"], "All markets": None}[scope]
    if scope != mkt:
        base_df, lists = None, {}          # Last / lists come from this page's scan only
    t = build_table(df, base_df, lists, mf)
    if t.empty:
        st.info("No alerts here."); return
    n_w, n_t = int((~t["_trig"]).sum()), int(t["_trig"].sum())
    near = int((pd.to_numeric(t["To alert %"], errors="coerce").abs() <= 2).sum())
    exp5 = int((pd.to_numeric(t["Expires in d"], errors="coerce") <= 5).sum())
    if scope in ("NSE", "USA") and n_w > FOCUS_TARGET:
        st.warning(f"{n_w} waiting {scope} alerts — about {n_w - FOCUS_TARGET} more than the ~{FOCUS_TARGET} that keep you focused. "
                   "Start cutting from the bottom of the table (C, then B · far).")
    st.caption(f"**{n_w} waiting** · {n_t} triggered (delete them in TradingView) · {near} within 2% of the close"
               + (f" · ⚠️ {exp5} expire within 5 days" if exp5 else ""))
    view = st.radio("Status", ["Waiting", "Triggered", "All"], horizontal=True, key=f"al_view_{mkt}")
    v = t[~t["_trig"]] if view == "Waiting" else t[t["_trig"]] if view == "Triggered" else t
    cols = [c for c in SHOW if c in v.columns]
    v = v.copy()
    for c in ("Alert at", "Last", "To alert %", "Set d ago", "Expires in d"):
        if c in v.columns:
            v[c] = pd.to_numeric(v[c], errors="coerce")
    vv = v[cols].reset_index(drop=True)
    extra = gp.live_extra(st.session_state.get("gtt_base_df"), v["Symbol"]) if scope == mkt else None
    if extra is not None:
        extra = extra.loc[:, [c for c in extra.columns if extra[c].notna().any()]]
    gp.card_grid(st, vv, key=f"al_grid_{mkt}_{scope}_{view}", sty=style_alerts(vv, v["_adr"].tolist()), extra=extra,
                 height=min(32 * len(vv) + 60, 520), widths={"Note": 220, "Status": 130, "Lists": 110})
    st.caption("Click a ticker for all its data. Trendline prices are estimated for today's bar. "
               "Last / To alert % come from today's scan (generate data first).")

    if scope == mkt:
        waiting = set(t.loc[~t["_trig"], "Symbol"].astype(str).str.upper())
        focus = [str(s).upper() for s in lists.get("FOCUS", [])]
        missing = [s for s in focus if s not in waiting]
        listed = {str(s).upper() for syms in lists.values() for s in syms}
        stale = sorted(set(t.loc[(~t["_trig"]) & (t["Lists"] == ""), "Symbol"].astype(str)))
        a, b = st.columns(2)
        with a:
            st.markdown("**Picked in Sector Focus, no alert yet**")
            if not lists:
                st.caption("Open Sector Focus first (it loads your flags and picks).")
            elif missing:
                gp.copy_area(st, "Set these in TradingView", ",".join(gp.tv_symbol(s, mcfg) for s in missing), height=68,
                             key=f"al_missing_{mkt}")
            else:
                st.caption("Every pick has an alert. ✓")
        with b:
            st.markdown("**Waiting alerts on names you haven't flagged or picked**")
            if lists and stale:
                st.caption("Still a valid plan (holding, your own idea)? Keep it. Otherwise delete it in TradingView.")
                gp.copy_area(st, "Review / delete", ",".join(gp.tv_symbol(s, mcfg) for s in stale), height=68,
                             key=f"al_stale_{mkt}")
            elif lists:
                st.caption("None — every waiting alert is on tonight's lists. ✓")
    cut = t[t["Priority"].isin(["Delete · triggered", "C · cut?"])]
    if not cut.empty:
        gp.copy_area(st, f"Suggested deletes in TradingView ({len(cut)}: triggered + on no list)",
                     ",".join(dict.fromkeys(str(tv).split(":", 1)[-1] if m == "USA" else str(tv)
                                            for tv, m in zip(cut["_tv"], cut["_mkt"]))),
                     height=68, key=f"al_cut_{mkt}_{scope}")
    st.markdown("**Grab the alerts in this view** (the ranges you've marked to focus on)")
    syms = ",".join(dict.fromkeys(str(tv).split(":", 1)[-1] if m == "USA" else str(tv)
                                  for tv, m in zip(v["_tv"], v["_mkt"])))
    gp.copy_area(st, f"{len(v)} alerts · {scope} · {view} — symbols for TradingView",
                 syms, height=68, key=f"al_copy_{mkt}_{scope}_{view}")
    lines = [f"{r['Symbol']} | {r['TF']} | {r['Line']} | alert {r['Alert at']:g} | last "
             + (f"{r['Last']:g}" if np.isfinite(_num(r['Last'])) else "-")
             + (f" | {r['To alert %']:+.1f}%" if np.isfinite(_num(r['To alert %'])) else "")
             + f" | {r['Status']}" + (f" | {r['Lists']}" if r['Lists'] else "")
             for r in v.to_dict("records") if np.isfinite(_num(r["Alert at"]))]
    gp.copy_area(st, "Same alerts with their levels — paste to Claude or your notes", "\n".join(lines),
                 height=150, key=f"al_lines_{mkt}_{scope}_{view}")
    out = v[[c for c in SHOW if c in v.columns]].copy()
    out.insert(1, "TradingView", v["_tv"].values)
    out["Alert ID"] = v["_id"].values
    st.download_button(f"Download {len(v)} alerts (CSV)", out.to_csv(index=False).encode(),
                       file_name=f"tv_alerts_{scope.replace(' ', '_').lower()}_{view.lower()}_{dt.date.today()}.csv",
                       mime="text/csv", key=f"al_dl_{mkt}_{scope}_{view}")
