"""Zerodha (Kite Connect) → Positions.

Flow
  1. Connect (once a day — Kite tokens die at ~6 AM IST): /scanner/positions → Connect Zerodha → Kite login →
     kite_callback swaps the request_token for an access token (stored encrypted in GTT Broker Settings).
  2. Sync (every 15 min in market hours, once after the close, on opening the Positions tab, or Sync now):
     • GET /trades (today's fills) → Supabase broker_fills (one row per exchange trade id, never twice)
     • new fills, grouped per order, are applied to positions / position_exits:
         BUY  → adopt your hand-entered position (same symbol, entry ±3 days, not yet linked) · else average into the
                open position · else open a new one (sector from the scan, stop from the GTT)
         SELL → FIFO over the open lots, with the same partial-sale split as the Positions table
     • GET /gtt/triggers → an active SELL GTT's trigger becomes the stop (only when the GTT changes, so a stop you
       type in the app stays until you move the GTT)
     • GET /portfolio/holdings → share counts compared with the app
  3. Days you didn't connect: Console → Reports → Tradebook → CSV → Import tradebook (same matcher, same de-dup).
"""
import csv
from collections import Counter
import hashlib
import io
import secrets
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import frappe
import numpy as np
import requests

from gtt_scanner.api import core
from gtt_scanner.api.core import clean, mcfg_for, sb

import positions as pos

IST = ZoneInfo("Asia/Kolkata")
DT = "GTT Broker Settings"
API = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login"
BROKER = "zerodha"
MKT = "NSE"
NEAR_DAYS = 3                       # a hand-entered position within ±3 days of the fill is "the same trade"
SERIES = {"EQ", "BE", "BZ", "SM", "ST", "SZ", "IT", "IV", "GB", "GS", "RR", "BL", "IL", "E1", "X1", "P1", "T"}
_http = requests                    # swapped for a fake in tests
_num = pos._num


# ── settings store (Single DocType; wrapped so tests can swap it) ─────────────
def _get(field):
    return frappe.db.get_single_value(DT, field)


def _set(**kw):
    for k, v in kw.items():
        frappe.db.set_single_value(DT, k, v)
    frappe.db.commit()


def _pw(field):
    from frappe.utils.password import get_decrypted_password
    try:
        return get_decrypted_password(DT, DT, field, raise_exception=False)
    except Exception:
        return None


def _set_pw(field, value):
    from frappe.utils.password import remove_encrypted_password, set_encrypted_password
    if value:
        set_encrypted_password(DT, DT, value, field)
        frappe.db.set_single_value(DT, field, "*" * 8)
    else:
        remove_encrypted_password(DT, DT, field)
        frappe.db.set_single_value(DT, field, "")
    frappe.db.commit()


# ── time ───────────────────────────────────────────────────────────────────
def now_ist():
    return datetime.now(IST).replace(tzinfo=None, microsecond=0)


def _session_start(now=None):
    now = now or now_ist()
    s = now.replace(hour=6, minute=0, second=0)
    return s if now >= s else s - timedelta(days=1)


def _dt(x):
    if not x:
        return None
    try:
        return datetime.fromisoformat(str(x).replace("T", " ")[:19])
    except ValueError:
        return None


def _d(x):
    return _dt(x).date() if _dt(x) else date.fromisoformat(str(x)[:10])


# ── Kite HTTP ──────────────────────────────────────────────────────────────
class KiteError(Exception):
    pass


class TokenError(KiteError):
    pass


def _creds():
    return (_get("kite_api_key") or "").strip(), _pw("kite_api_secret")


def _token():
    """Today's access token, or None (not connected / expired at 6 AM IST)."""
    if not _get("kite_access_token"):
        return None
    t = _dt(_get("kite_token_date"))
    if not t or t < _session_start():
        return None
    return _pw("kite_access_token")


def _kite(path, key, tok, method="GET", data=None):
    h = {"X-Kite-Version": "3"}
    if tok:
        h["Authorization"] = f"token {key}:{tok}"
    fn = _http.post if method == "POST" else _http.delete if method == "DELETE" else _http.get
    r = fn(API + path, headers=h, data=data, timeout=20)
    try:
        j = r.json()
    except Exception:
        raise KiteError(f"Kite {path}: HTTP {r.status_code}")
    if r.status_code == 403 or j.get("error_type") == "TokenException":
        raise TokenError(j.get("message") or "Zerodha session expired — connect again")
    if j.get("status") != "success":
        raise KiteError(f"Kite {path}: {j.get('message') or r.status_code}")
    return j.get("data")


# ── options ────────────────────────────────────────────────────────────────
def _opts():
    sp = lambda f, dflt: {x.strip().upper() for x in str(_get(f) or dflt).split(",") if x.strip()}
    sf = _get("kite_sync_from")
    return {"products": sp("kite_products", "CNC"), "exchanges": sp("kite_exchanges", "NSE,BSE"),
            "gtt": bool(int(_get("kite_gtt_stops") if _get("kite_gtt_stops") is not None else 1)),
            "sync_from": _d(sf) if sf else None, "ignore": sp("kite_ignore", "")}


def _sym(ts):
    s = str(ts or "").strip().upper()
    if "-" in s and s.rsplit("-", 1)[1] in SERIES:
        s = s.rsplit("-", 1)[0]
    return s


# ── fills ──────────────────────────────────────────────────────────────────
def _fill_from_kite(t, uid):
    return {"user_id": uid, "market": MKT, "broker": BROKER, "trade_id": str(t["trade_id"]), "order_id": str(t.get("order_id") or ""),
            "exchange": str(t.get("exchange") or "").upper(), "symbol": _sym(t.get("tradingsymbol")),
            "side": str(t.get("transaction_type") or "").upper(), "qty": float(t.get("quantity") or t.get("filled") or 0),
            "price": float(t.get("average_price") or t.get("price") or 0), "product": str(t.get("product") or "").upper(),
            "filled_at": str(t.get("fill_timestamp") or t.get("exchange_timestamp") or t.get("order_timestamp") or now_ist())[:19],
            "source": "api", "status": "new", "raw": clean(t)}


def _fill_from_tradebook(r, uid):
    g = lambda *ks: next((r[k] for k in ks if r.get(k) not in (None, "")), "")
    when = g("order_execution_time", "trade_date")
    return {"user_id": uid, "market": MKT, "broker": BROKER, "trade_id": str(g("trade_id")), "order_id": str(g("order_id")),
            "exchange": str(g("exchange")).upper(), "symbol": _sym(g("symbol", "tradingsymbol")),
            "side": str(g("trade_type", "transaction_type")).upper(), "qty": float(g("quantity") or 0),
            "price": float(g("price") or 0), "product": "CNC",
            "filled_at": str(when).replace("T", " ")[:19], "source": "tradebook", "status": "new", "raw": clean(dict(r))}


def _fp(f):
    return (f["symbol"], f["side"], round(float(f["qty"]), 4), round(float(f["price"]), 2), str(f["filled_at"])[:16])


def _store_fills(c, fills):
    """Insert fills not seen before: same exchange trade id, or (from the other source) same symbol/side/qty/price/minute.
    Returns how many were new."""
    fills = [f for f in fills if f["trade_id"] and f["qty"] > 0 and f["side"] in ("BUY", "SELL")]
    if not fills:
        return 0
    lo, hi = min(str(f["filled_at"]) for f in fills)[:10], max(str(f["filled_at"]) for f in fills)[:10] + " 23:59:59"
    have = fetch_all(lambda: c.table("broker_fills").select("trade_id,symbol,side,qty,price,filled_at,source").eq("broker", BROKER)
                     .gte("filled_at", lo).lt("filled_at", hi).order("id"))
    seen = {str(r["trade_id"]) for r in have}
    other = {}
    for r in have:
        other.setdefault(r.get("source"), Counter())[_fp(r)] += 1
    new, mine = [], set()
    for f in fills:
        if f["trade_id"] in seen or f["trade_id"] in mine:
            continue
        cross = [cnt for src, cnt in other.items() if src != f["source"] and cnt[_fp(f)] > 0]
        if cross:                                             # already there from the API / the tradebook
            cross[0][_fp(f)] -= 1
            continue
        mine.add(f["trade_id"]); new.append(f)
    for ch in _chunks(new, 300):
        c.table("broker_fills").insert(ch).execute()
    return len(new)


def _orders(fills):
    """Partial fills of one order → one order (VWAP), oldest first."""
    g = {}
    for f in fills:
        k = f.get("order_id") or f["trade_id"]
        o = g.setdefault(k, {**f, "qty": 0.0, "_val": 0.0, "_ids": []})
        o["qty"] += float(f["qty"]); o["_val"] += float(f["qty"]) * float(f["price"]); o["_ids"].append(f["id"])
        o["filled_at"] = min(str(o["filled_at"]), str(f["filled_at"]))
    for o in g.values():
        o["price"] = round(o["_val"] / o["qty"], 4) if o["qty"] else 0
    return sorted(g.values(), key=lambda o: (str(o["filled_at"]), min(int(i) for i in o["_ids"])))


# ── matcher (in memory: load the book once, apply every order, write the changes in a few batched calls) ──
def fetch_all(qf, page=1000):
    """PostgREST returns at most 1000 rows per call — page through everything."""
    out, i = [], 0
    while True:
        d = qf().range(i, i + page - 1).execute().data or []
        out += d
        if len(d) < page:
            return out
        i += page


def _chunks(xs, n=300):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _near(x, d):
    try:
        return abs((_d(x) - d).days) <= NEAR_DAYS
    except Exception:
        return False


def _q(x):
    v = _num(x)
    return v if np.isfinite(v) else 0.0


def _now():
    return datetime.utcnow().isoformat()


class Book:
    """Every position + exit for the user/market in memory; changes are written by flush()."""

    def __init__(self, c, m):
        self.c, self.m = c, m
        rows = fetch_all(lambda: c.table("positions").select("*").eq("user_id", m["user_id"]).eq("market", MKT).order("id"))
        self.rows = {r["id"]: r for r in rows}
        self.orig = {r["id"]: dict(r) for r in rows}
        self.exits = {}
        ids = list(self.rows)
        for ch in _chunks(ids):
            for e in fetch_all(lambda: c.table("position_exits").select("*").in_("position_id", ch).order("id")):
                self.exits.setdefault(e["position_id"], []).append(e)
        self.dirty, self.deleted, self.new_rows, self.new_exits, self._tmp = set(), set(), [], [], -1
        self.info = {}

    def of(self, sym, status, upto=None):
        out = [r for r in self.rows.values() if r["symbol"] == sym and r.get("status") == status]
        if upto is not None:
            out = [r for r in out if _d(r["entry_date"]) <= upto]
        return sorted(out, key=lambda r: (str(r["entry_date"]), r["id"] if r["id"] > 0 else 1e12 - r["id"]))

    def update(self, r, **kw):
        if all(_same(r.get(k)) == _same(v) for k, v in kw.items()):
            return
        r.update(kw, updated_at=_now())
        if r["id"] > 0:
            self.dirty.add(r["id"])

    def own(self, x):
        """A position you entered by hand is never rewritten by a replay: the import works on a copy it owns, and your
        row is parked (status 'imported', hidden) so the next replay can start again from exactly what you typed."""
        if x.get("origin") == "broker":
            return x
        y = self.insert({k: v for k, v in x.items() if k not in ("id", "created_at", "updated_at")} | {"origin": "broker"})
        self.update(x, status="imported")
        return y

    def insert(self, row):
        row = {"status": "open", "note": "", **row, "id": self._tmp}
        self._tmp -= 1
        self.rows[row["id"]] = row
        self.new_rows.append(row)
        return row

    def exit(self, pid, qty, price, d, note="Zerodha"):
        e = {"position_id": pid, "qty": qty, "price": price, "exit_date": d.isoformat(), "note": note}
        self.exits.setdefault(pid, []).append(e)
        self.new_exits.append(e)

    def drop(self, r):
        self.rows.pop(r["id"], None)
        self.exits.pop(r["id"], None)
        if r["id"] > 0:
            self.deleted.add(r["id"]); self.dirty.discard(r["id"])

    def sector(self, sym):
        if sym not in self.info:
            try:
                from gtt_scanner.api.positions import symbol_info
                self.info[sym] = symbol_info(MKT, sym, self.c, self.m)
            except Exception:
                self.info[sym] = ({}, None)
        return self.info[sym]

    def flush(self, dropped=()):
        """Write everything; returns {temp id: real id}. A rebuilt row identical to one it replaces keeps the old row
        (and id), so a replay that changes nothing writes almost nothing."""
        c = self.c
        idmap = {}
        news = [r for r in self.new_rows if r["id"] in self.rows]
        if dropped:
            pool = {}
            for r, ex in dropped:
                pool.setdefault(_sig(r, ex), []).append(r)
            keep_news = []
            for r in news:
                hit = pool.get(_sig(r, self.exits.get(r["id"], [])))
                if hit:
                    old = hit.pop(0)
                    idmap[r["id"]] = old["id"]
                    self.deleted.discard(old["id"])
                    if any(_same(r.get(f)) != _same(old.get(f)) for f in _EDITABLE):
                        self.rows[old["id"]] = {**old, **{f: r.get(f) for f in _EDITABLE}, "updated_at": _now()}
                        self.dirty.add(old["id"])
                    self.exits.pop(r["id"], None)
                else:
                    keep_news.append(r)
            news = keep_news
        for ch in _chunks(sorted(self.deleted)):
            c.table("positions").delete().in_("id", ch).execute()
        for ch in _chunks(news, 200):
            res = c.table("positions").insert([{k: v for k, v in r.items() if k != "id"} for r in ch]).execute().data or []
            for r, got in zip(ch, res):
                idmap[r["id"]] = got["id"]
        def diff(r):
            o = self.orig.get(r["id"], {})
            return any(_same(r.get(k)) != _same(o.get(k)) for k in set(r) | set(o) if k != "updated_at")
        for ch in _chunks([self.rows[i] for i in sorted(self.dirty) if i in self.rows and diff(self.rows[i])], 200):
            c.table("positions").upsert(ch, on_conflict="id").execute()
        new_ids = {r["id"] for r in news}
        ex = [{**e, "position_id": idmap.get(e["position_id"], e["position_id"])} for e in self.new_exits
              if e["position_id"] in self.rows and (e["position_id"] > 0 or e["position_id"] in new_ids)]
        for ch in _chunks(ex, 300):
            c.table("position_exits").insert(ch).execute()
        return idmap


_EDITABLE = ("setup", "note", "stop", "init_stop", "gtt_stop", "stop_source", "sector", "industry", "fail_level", "broker", "origin")


def _same(v):
    return round(float(v), 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else (v or None)


def _sig(r, exits):
    return (r["symbol"], str(r["entry_date"])[:10], _same(r.get("avg_price")), _same(r.get("qty")), _same(r.get("qty_open")),
            r.get("status"), str(r.get("closed_at") or "")[:10],
            tuple(sorted((_same(e["qty"]), _same(e["price"]), str(e["exit_date"])[:10]) for e in exits)))


def _vwap(os_):
    q = sum(o["qty"] for o in os_)
    return q, (sum(o["qty"] * o["price"] for o in os_) / q if q else 0.0)


def _buy(bk, sym, q, p, d, stops):
    """A delivery buy of q @ p on day d."""
    gs = stops.get(sym)
    opens = bk.of(sym, "open", d)
    cand = [x for x in bk.of(sym, "open") if x.get("origin") != "broker" and not x.get("broker")
            and (_near(x["entry_date"], d) or _q(x.get("qty_open")) <= 0)]
    if cand:                                                   # the position you typed in yourself
        x = bk.own(cand[0])
        # from now on the import owns it (entry date = the fill's); your setup / note / stops are carried over on a replay
        kw = {"broker": BROKER, "origin": "broker", "entry_date": d.isoformat(), "avg_price": p, "qty": q, "qty_open": q}
        if gs and not _q(x.get("stop")):
            kw.update(stop=gs, gtt_stop=gs, stop_source="gtt", init_stop=x.get("init_stop") or gs)
        bk.update(x, **kw)
        return "adopted", x["id"], f"{sym}: matched your entry ({q:g} @ {p:.2f})"
    if opens:                                                  # adding to an open position
        x = bk.own(opens[-1])
        qo = _q(x.get("qty_open"))
        nq = qo + q
        avg = (_q(x["avg_price"]) * qo + p * q) / nq
        bk.update(x, broker=BROKER, qty=_q(x.get("qty")) + q, qty_open=nq, avg_price=round(avg, 4))
        return "added", x["id"], f"{sym}: added {q:g} @ {p:.2f} → {nq:g} @ {avg:.2f}"
    done = [x for x in bk.of(sym, "closed") if not x.get("broker") and _near(x["entry_date"], d)]
    if done:                                                   # back-fill of a trade you already logged and closed
        bk.update(done[0], broker=BROKER)
        return "linked", done[0]["id"], f"{sym}: already logged (closed)"
    info, fail = bk.sector(sym)
    r = bk.insert({
        "user_id": bk.m["user_id"], "market": MKT, "symbol": sym, "sector": info.get("sector"), "industry": info.get("industry"),
        "setup": None, "entry_date": d.isoformat(), "avg_price": round(p, 4), "qty": q,
        "qty_open": q, "stop": gs, "init_stop": gs, "gtt_stop": gs, "stop_source": "gtt" if gs else None, "fail_level": fail,
        "note": AUTO_NOTE, "broker": BROKER, "origin": "broker"})
    return "opened", r["id"], f"{sym}: new position {q:g} @ {p:.2f}"


def _sell(bk, sym, q, p, d):
    """A delivery sell of q @ p on day d: FIFO over lots bought before d, with the Positions-table partial split."""
    opens = [x for x in bk.of(sym, "open") if _d(x["entry_date"]) < d] or bk.of(sym, "open", d)
    rem, first, msg = q, None, []
    for x in opens:
        if rem <= 1e-9:
            break
        x = bk.own(x)
        qo = _q(x.get("qty_open"))
        held = qo if qo > 0 else rem                           # size not tracked → this sale closes it
        take = min(held, rem)
        bk.exit(x["id"], take, round(p, 4), d)
        kw = {"broker": BROKER, "status": "closed", "closed_at": d.isoformat(), "qty_open": 0}
        if take < held - 1e-9:                                 # partial: this lot keeps what was sold, the rest moves on
            kw["qty"] = take
            keep = ("user_id", "market", "symbol", "sector", "industry", "setup", "entry_date", "avg_price", "stop",
                    "init_stop", "fail_level", "stop_source", "gtt_stop")
            bk.insert({k: x.get(k) for k in keep if k in x} | {
                "broker": BROKER, "origin": "broker", "qty": held - take, "qty_open": held - take,
                "note": ((x.get("note") or "").replace(pos.REMAINDER, "").strip() + " " + pos.REMAINDER).strip()})
            msg.append(f"{sym}: sold {take:g} @ {p:.2f} · {held - take:g} still open")
        else:
            if qo <= 0:
                kw["qty"] = take
            a = _q(x["avg_price"])
            msg.append(f"{sym}: closed {take:g} @ {p:.2f}" + (f" ({(p / a - 1) * 100:+.1f}%)" if a else ""))
        bk.update(x, **kw)
        first = first or x["id"]
        rem -= take
    if rem <= 1e-9:
        return "applied", first, " · ".join(msg)
    if not opens:
        done = [x for x in bk.of(sym, "closed") if x.get("closed_at") and _near(x["closed_at"], d) and not x.get("origin")]
        if done:
            bk.update(done[0], broker=BROKER)
            return "linked", done[0]["id"], f"{sym}: sale already logged"
    msg.append(f"{sym}: sold {rem:g} @ {p:.2f} with nothing open — shares bought before the imported history?")
    return ("partial" if first else "unmatched"), first, " · ".join(msg)


def _intraday(bk, sym, q, pb, ps, d, sold_first):
    """The part of a day's buys and sells that cancels out = an intraday trade (Zerodha nets it the same way)."""
    r = bk.insert({"user_id": bk.m["user_id"], "market": MKT, "symbol": sym, "entry_date": d.isoformat(), "avg_price": round(pb, 4),
                   "qty": q, "qty_open": 0, "status": "closed", "closed_at": d.isoformat(), "setup": "Intraday",
                   "sector": bk.sector(sym)[0].get("sector"), "industry": bk.sector(sym)[0].get("industry"),
                   "note": "Intraday" + (" short (sold first)" if sold_first else ""), "broker": BROKER, "origin": "broker"})
    bk.exit(r["id"], q, round(ps, 4), d)
    return r["id"], f"{sym}: intraday {q:g} · bought {pb:.2f} sold {ps:.2f} ({(ps - pb) * q:+,.0f})"


AUTO_NOTE = "Imported from Zerodha"
_KEEP = ("setup", "init_stop", "sector", "industry", "fail_level")


def _remember(saved, r):
    """What you added to an imported trade (setup, note, stops …), keyed by symbol + entry date, to survive a replay."""
    a = saved.setdefault((r["symbol"], str(r["entry_date"])[:10], r.get("setup") == "Intraday"), {})
    for f in _KEEP:
        if r.get(f) not in (None, "") and f not in a and not (f == "setup" and r[f] == "Intraday"):
            a[f] = r[f]
    base = (r.get("note") or "").replace(pos.REMAINDER, "").strip()
    if base and base != AUTO_NOTE and not base.startswith("Intraday") and "note" not in a:
        a["note"] = base
    if r.get("status") == "open" and r.get("stop") not in (None, ""):
        a.update(stop=r["stop"], stop_source=r.get("stop_source"), gtt_stop=r.get("gtt_stop"))


def _restore(bk, saved):
    for r in bk.rows.values():
        a = saved.get((r["symbol"], str(r["entry_date"])[:10], r.get("setup") == "Intraday")) if r.get("origin") == "broker" else None
        if not a:
            continue
        kw = {f: a[f] for f in _KEEP if f in a}
        if "note" in a:
            kw["note"] = (a["note"] + (" " + pos.REMAINDER if pos.REMAINDER in (r.get("note") or "") else "")).strip()
        if r.get("status") == "open" and "stop" in a:
            kw.update(stop=a["stop"], stop_source=a.get("stop_source"), gtt_stop=a.get("gtt_stop"))
        r.update(kw)


def process(c, m, opts, stops, rebuild=True):
    """Replay every stored fill (except dismissed) in date order, rebuilding what the import owns; a replay that changes
    nothing writes almost nothing. Per stock per day, buys and sells that cancel out are an intraday trade (at the day's
    average buy / sell, as Zerodha nets them); what's left is a delivery buy (→ position) or sell (→ FIFO over older lots).
    That handles 'bought 100, sold 40 the same day, carried 60' and a converted MIS→CNC alike. Returns messages for
    today's fills."""
    fills = [f for f in fetch_all(lambda: c.table("broker_fills").select("*").eq("user_id", m["user_id"]).eq("market", MKT)
                                  .eq("broker", BROKER).order("id")) if f.get("status") != "dismissed"]
    bk = Book(c, m)
    saved, dropped = {}, []
    for r in list(bk.rows.values()):
        if r.get("status") == "imported":                     # your own entry, parked by an earlier replay
            r["status"] = "open"
            bk.dirty.add(r["id"])                              # written back only if this replay no longer parks it
        if r.get("origin") == "broker":
            _remember(saved, r)
            dropped.append((dict(r), list(bk.exits.get(r["id"], []))))
            bk.drop(r)
        elif r.get("broker"):
            bk.update(r, broker=None)
    days = {}
    for o in _orders(fills):
        days.setdefault((_d(o["filled_at"]), o["symbol"]), []).append(o)
    today = now_ist().date()
    res, out, counts = {}, [], {}
    for (d, sym) in sorted(days):
        os_ = days[(d, sym)]
        ok_ = [o for o in os_ if o["exchange"] in opts["exchanges"] and not (opts["sync_from"] and d < opts["sync_from"])]
        for o in os_:
            if o not in ok_:
                for i in o["_ids"]:
                    res[i] = ("skipped", None, f"{sym}: {o['exchange']} / before {opts['sync_from']} — not imported")
        if not ok_:
            continue
        try:
            B, pb = _vwap([o for o in ok_ if o["side"] == "BUY"])
            S, ps = _vwap([o for o in ok_ if o["side"] == "SELL"])
            Q = min(B, S)
            notes, pid, st_ = [], None, None
            if Q > 1e-9:
                if "MIS" in opts["products"]:
                    sold_first = ok_[0]["side"] == "SELL" and not bk.of(sym, "open", d - timedelta(days=1))
                    pid, msg = _intraday(bk, sym, Q, pb, ps, d, sold_first)
                    st_ = "intraday"
                else:
                    pid, msg, st_ = None, f"{sym}: intraday {Q:g} not imported (intraday is off)", "skipped"
                notes.append(msg)
            if B - Q > 1e-9:
                st_, pid2, msg = _buy(bk, sym, B - Q, pb, d, stops)
                pid = pid2; notes.append(msg)
            elif S - Q > 1e-9:
                st_, pid2, msg = _sell(bk, sym, S - Q, ps, d)
                pid = pid2 or pid; notes.append(msg)
        except Exception as e:  # keep going; the fills are marked 'error' and shown
            st_, pid, notes = "error", None, [f"{sym}: {e}"]
        for o in ok_:
            for i in o["_ids"]:
                res[i] = (st_, pid, " · ".join(notes))
        counts[st_] = counts.get(st_, 0) + 1
        if d == today and st_ != "skipped":
            out.append(" · ".join(notes))
    _restore(bk, saved)
    idmap = bk.flush(dropped)
    rows = {f["id"]: f for f in fills}
    upd = []
    for i, (st_, pid, msg) in res.items():
        f = rows[i]
        new = {"status": st_, "position_id": idmap.get(pid, pid), "note": msg[:300]}
        if any(_same(f.get(k)) != _same(v) for k, v in new.items()):
            upd.append({**f, **new, "processed_at": _now()})
    for ch in _chunks(upd, 300):
        c.table("broker_fills").upsert(ch, on_conflict="id").execute()
    if rebuild:
        out.append("replayed " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    return out


def apply_stops(c, m, stops):
    """New / moved GTT → the position's stop. A GTT that hasn't moved leaves a stop typed in the app alone."""
    msgs = []
    for p in pos.load_positions(c, m):
        s = stops.get(p["symbol"])
        if not s or "gtt_stop" not in p or _num(p.get("gtt_stop")) == s:
            continue
        upd = {"gtt_stop": s, "stop": s, "stop_source": "gtt", "updated_at": _now()}
        if not _q(p.get("init_stop")):
            upd["init_stop"] = s
        c.table("positions").update(upd).eq("id", int(p["id"])).execute()
        if _num(p.get("stop")) != s:
            msgs.append(f"{p['symbol']} stop → {s:g} (GTT)")
    return msgs


# ── GTT stops ──────────────────────────────────────────────────────────────
def gtt_stops(gtts, exchanges):
    """{symbol: stop} from active SELL GTTs. OCO → the lower leg; single → only if below the price (else it's a target)."""
    out = {}
    for g in gtts or []:
        cond, ords = g.get("condition") or {}, g.get("orders") or []
        if g.get("status") != "active" or not ords or str(ords[0].get("transaction_type")).upper() != "SELL":
            continue
        if str(cond.get("exchange") or "").upper() not in exchanges:
            continue
        tv = [float(x) for x in cond.get("trigger_values") or [] if x is not None]
        if not tv:
            continue
        lp = float(cond.get("last_price") or 0)
        if g.get("type") == "two-leg" or len(tv) > 1:
            s = min(tv)
        else:
            s = tv[0]
            if lp and s > lp:
                continue
        sym = _sym(cond.get("tradingsymbol"))
        out[sym] = max(out.get(sym, 0), s)
    return out


# ── checks shown on the Positions tab ──────────────────────────────────────
def checks(c, m, opts, stops, holdings=None, day=None, traded_today=()):
    open_ = pos.load_positions(c, m)
    out = {"needs_details": [], "no_gtt": [], "stop_differs": [], "qty_mismatch": [], "not_in_app": []}
    app_qty = {}
    for p in open_:
        sym = p["symbol"]
        if p.get("broker") and (not p.get("setup") or not _q(p.get("stop"))):
            out["needs_details"].append({"id": p["id"], "symbol": sym, "missing": ", ".join(
                x for x, ok in (("setup", p.get("setup")), ("stop", _q(p.get("stop")))) if not ok)})
        if opts["gtt"] and _q(p.get("qty_open")) > 0 and sym not in stops:
            out["no_gtt"].append(sym)
        g = _num(p.get("gtt_stop"))
        if np.isfinite(g) and sym in stops and abs(_q(p.get("stop")) - stops[sym]) > 1e-6:
            out["stop_differs"].append({"symbol": sym, "app": _q(p.get("stop")), "gtt": stops[sym]})
        if _q(p.get("qty_open")) > 0:
            app_qty[sym] = app_qty.get(sym, 0) + _q(p.get("qty_open"))
    if holdings is not None:
        held = {}
        for h in holdings:                                   # a holding is listed once, whichever exchange it was bought on
            s = _sym(h.get("tradingsymbol"))
            held[s] = held.get(s, 0) + float(h.get("quantity") or 0) + float(h.get("t1_quantity") or 0)
        busy = set(traded_today) | {_sym(x.get("tradingsymbol")) for x in (day or []) if float(x.get("quantity") or 0)
                                    or float(x.get("buy_quantity") or 0) or float(x.get("sell_quantity") or 0)}
        for s in sorted(set(app_qty) | set(held)):
            if s in busy:
                continue
            a, b = app_qty.get(s, 0), held.get(s, 0)
            if s not in app_qty:
                if b > 0 and s not in opts["ignore"]:
                    out["not_in_app"].append({"symbol": s, "zerodha": b})
            elif abs(a - b) > 1e-6:
                out["qty_mismatch"].append({"symbol": s, "app": a, "zerodha": b})
    out["no_gtt"] = sorted(set(out["no_gtt"]))
    return out


# ── sync ───────────────────────────────────────────────────────────────────
def _lock(on=True):
    k = "gtt:kite:lock"
    if on:
        if frappe.cache().get_value(k):
            return False
        frappe.cache().set_value(k, 1, expires_in_sec=120)
        return True
    frappe.cache().delete_value(k)


def sync(trigger="manual"):
    m = mcfg_for(MKT)
    if not int(_get("kite_enabled") or 0):
        return {"ok": False, "message": "Zerodha sync is off (GTT Broker Settings)."}
    key, secret = _creds()
    if not key or not secret:
        return {"ok": False, "message": "Add the Kite API key and secret in GTT Broker Settings."}
    tok = _token()
    if not tok:
        return {"ok": False, "message": "Not connected today — Connect Zerodha (Kite needs a login every morning)."}
    if not _lock():
        return {"ok": False, "message": "A sync is already running."}
    c = sb()
    opts = _opts()
    try:
        try:
            trades = _kite("/trades", key, tok) or []
            gtts = _kite("/gtt/triggers", key, tok) if opts["gtt"] else []
            holdings = _kite("/portfolio/holdings", key, tok) or []
            day = (_kite("/portfolio/positions", key, tok) or {}).get("day") or []
        except TokenError as e:
            _set_pw("kite_access_token", "")
            return {"ok": False, "message": f"{e} — connect again."}
        try:
            n_new = _store_fills(c, [_fill_from_kite(t, m["user_id"]) for t in trades])
        except Exception as e:
            return {"ok": False, "message": f"Couldn't save fills — run supabase_broker.sql in Supabase once. ({e})"}
        stops = gtt_stops(gtts, opts["exchanges"]) if opts["gtt"] else {}
        msgs = process(c, m, opts, stops, rebuild=False)
        if opts["gtt"]:
            msgs += apply_stops(c, m, stops)
        traded = {_sym(t.get("tradingsymbol")) for t in trades}
        ck = checks(c, m, opts, stops, holdings, day, traded)
        frappe.cache().set_value(f"gtt:kite:checks:{m['user_id']}", ck)
        res = f"{len(trades)} fill{'' if len(trades) == 1 else 's'} today ({n_new} new)" + (" · " + " · ".join(msgs) if msgs else " · positions up to date")
        _set(kite_last_sync=str(now_ist()), kite_last_result=res[:1000])
        return {"ok": True, "message": res, "changed": bool(msgs)}
    finally:
        _lock(False)


def scheduled_sync():
    """Cron every 15 min on weekdays: 09:15–16:15 IST, then once more in the evening. Quiet when not connected."""
    try:
        now = now_ist()
        if now.weekday() >= 5 or not int(_get("kite_enabled") or 0) or not _token():
            return
        t = now.time()
        last = _dt(_get("kite_last_sync"))
        in_mkt = (9, 15) <= (t.hour, t.minute) <= (16, 15)
        evening = (t.hour, t.minute) > (16, 15) and (not last or last < now.replace(hour=16, minute=15, second=0))
        if in_mkt or evening:
            sync("schedule")
    except Exception:
        frappe.log_error(title="GTT Kite sync")


# ── what the Positions tab shows ───────────────────────────────────────────
def status():
    m = mcfg_for(MKT)
    key, secret = _creds()
    tok_ok = bool(_token())
    t = _dt(_get("kite_token_date"))
    out = {"broker": "Zerodha", "enabled": bool(int(_get("kite_enabled") or 0)), "configured": bool(key and secret),
           "has_key": bool(key), "has_secret": bool(secret), "secret_saved": bool(_get("kite_api_secret")),
           "connected": tok_ok, "user": _get("kite_user_id") if tok_ok else None,
           "valid_until": str(_session_start() + timedelta(days=1))[:16] if tok_ok else None,
           "connected_at": str(t)[:16] if t else None, "last_sync": _get("kite_last_sync"),
           "last_result": _get("kite_last_result"), "settings_url": "/app/gtt-broker-settings",
           "checks": frappe.cache().get_value(f"gtt:kite:checks:{m['user_id']}") or {}, "unmatched": [],
           "intraday": "MIS" in _opts()["products"]}
    if out["configured"]:
        try:
            out["unmatched"] = clean(sb().table("broker_fills").select("id,symbol,side,qty,price,filled_at,note,status")
                                     .eq("user_id", m["user_id"]).eq("broker", BROKER).in_("status", ["unmatched", "partial", "error"])
                                     .order("filled_at", desc=True).limit(50).execute().data or [])
            first = sb().table("broker_fills").select("filled_at").eq("user_id", m["user_id"]).eq("broker", BROKER) \
                .order("filled_at").limit(1).execute().data
            out["history_from"] = str(first[0]["filled_at"])[:10] if first else None
        except Exception:
            out["needs_sql"] = True
    return out


# ── endpoints ──────────────────────────────────────────────────────────────
@frappe.whitelist(methods=["POST"])
def kite_login():
    core.require_user()
    key, secret = _creds()
    if not key or not secret:
        frappe.throw("Add the Kite API key and secret in GTT Broker Settings first.")
    state = secrets.token_urlsafe(16)
    frappe.cache().set_value(f"gtt:kite:state:{state}", frappe.session.user, expires_in_sec=600)
    return {"url": f"{LOGIN}?" + urlencode({"v": 3, "api_key": key, "redirect_params": urlencode({"state": state})})}


def _redirect(tag):
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = f"/scanner/positions?kite={tag}"


@frappe.whitelist(methods=["GET"])
def kite_callback(request_token=None, status=None, state=None, **kw):
    """Kite redirects here after login (set this as the app's Redirect URL in the Kite developer console)."""
    core.require_user()
    who = frappe.cache().get_value(f"gtt:kite:state:{state}") if state else None
    if who != frappe.session.user:
        return _redirect("badstate")
    frappe.cache().delete_value(f"gtt:kite:state:{state}")
    if status != "success" or not request_token:
        return _redirect("cancelled")
    key, secret = _creds()
    try:
        chk = hashlib.sha256(f"{key}{request_token}{secret}".encode()).hexdigest()
        d = _kite("/session/token", key, None, "POST", {"api_key": key, "request_token": request_token, "checksum": chk})
    except Exception:
        frappe.log_error(title="GTT Kite login")
        return _redirect("failed")
    _set_pw("kite_access_token", d["access_token"])
    first = _get("kite_first_connect")
    _set(kite_user_id=d.get("user_id") or "", kite_token_date=str(now_ist()),
         **({} if first else {"kite_first_connect": now_ist().date().isoformat()}))
    return _redirect("ok")


@frappe.whitelist(methods=["POST"])
def kite_disconnect():
    core.require_user()
    key, _ = _creds()
    tok = _token()
    if tok:
        try:
            _kite("/session/token?" + urlencode({"api_key": key, "access_token": tok}), key, tok, "DELETE")
        except Exception:
            pass
    _set_pw("kite_access_token", "")
    _set(kite_token_date="")
    return {"ok": True}


@frappe.whitelist(methods=["POST"])
def sync_now():
    core.require_user()
    return sync("manual")


@frappe.whitelist(methods=["POST"])
def import_tradebook(csv_text=""):
    """Zerodha Console → Reports → Tradebook (Equity) → CSV. Safe to import the same file twice."""
    core.require_user()
    m = mcfg_for(MKT)
    rows = list(csv.DictReader(io.StringIO((csv_text or "").lstrip("﻿"))))
    if not rows or "trade_id" not in {k.strip().lower() for k in rows[0]}:
        frappe.throw("That doesn't look like a Zerodha tradebook CSV (needs a trade_id column).")
    rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items() if k} for r in rows]
    rows = [r for r in rows if (r.get("segment") or "EQ").upper() == "EQ"]
    c = sb()
    fills = [_fill_from_tradebook(r, m["user_id"]) for r in rows]
    # the tradebook has no product: a stock bought AND sold on the same day was an intraday (MIS) trade
    sides = {}
    for f in fills:
        sides.setdefault((f["symbol"], str(f["filled_at"])[:10]), set()).add(f["side"])
    for f in fills:
        if len(sides[(f["symbol"], str(f["filled_at"])[:10])]) == 2:
            f["product"] = "MIS"
    try:
        n_new = _store_fills(c, fills)
    except Exception as e:
        frappe.throw(f"Couldn't save fills — run supabase_broker.sql in Supabase once. ({e})")
    msgs = process(c, m, _opts(), {}, rebuild=True)
    return {"message": f"{len(rows)} trades in the file, {n_new} new · " + " · ".join(msgs), "changed": True}


@frappe.whitelist(methods=["POST"])
def set_products(intraday=0):
    """Include intraday (MIS) trades or not — then replay every fill so the trades and stats match."""
    core.require_user()
    prods = {x.strip().upper() for x in str(_get("kite_products") or "CNC").split(",") if x.strip()} | {"CNC"}
    prods = (prods | {"MIS"}) if int(intraday or 0) else (prods - {"MIS"})
    _set(kite_products=",".join(sorted(prods)))
    return rebuild()


@frappe.whitelist(methods=["POST"])
def rebuild():
    core.require_user()
    if not _lock():
        return {"ok": False, "message": "A sync is already running — try again in a minute."}
    try:
        m = mcfg_for(MKT)
        o = _opts()
        msgs = process(sb(), m, o, {}, rebuild=True)
        return {"ok": True, "message": " · ".join(msgs), "changed": True}
    finally:
        _lock(False)


@frappe.whitelist(methods=["POST"])
def dismiss_all():
    core.require_user()
    m = mcfg_for(MKT)
    sb().table("broker_fills").update({"status": "dismissed"}).eq("user_id", m["user_id"]).eq("broker", BROKER) \
        .in_("status", ["unmatched", "partial", "error"]).execute()
    return {"ok": True}


@frappe.whitelist(methods=["POST"])
def dismiss_fill(id=None):
    core.require_user()
    sb().table("broker_fills").update({"status": "dismissed"}).eq("id", int(id)).execute()
    return {"ok": True}
