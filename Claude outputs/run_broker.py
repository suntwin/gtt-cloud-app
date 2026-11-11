"""Zerodha sync end to end with a fake Kite: python -m tests.run_broker"""
import types
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from tests import harness

frappe, db = harness.install()
frappe.local.response = {}
from gtt_scanner.api import broker, positions, scan  # noqa: E402

# ── fake settings + fake Kite ──
S, PW = {"kite_enabled": 1, "kite_api_key": "kkey", "kite_gtt_stops": 1, "kite_products": "CNC", "kite_exchanges": "NSE,BSE"}, \
    {"kite_api_secret": "ssecret"}
broker._get = lambda f: S.get(f)
broker._set = lambda **kw: S.update(kw)
broker._pw = lambda f: PW.get(f)


def _set_pw(f, v):
    PW[f] = v; S[f] = "*" * 8 if v else ""
broker._set_pw = _set_pw

TODAY = broker.now_ist().date()
D = lambda n: (TODAY - timedelta(days=n)).isoformat()
K = {"trades": [], "gtts": [], "holdings": [], "day": [], "expired": False}


class R:
    def __init__(self, j, code=200): self.j, self.status_code = j, code
    def json(self): return self.j


class FakeKite:
    def _r(self, url, data=None):
        path = urlparse(url).path
        if K["expired"] and path != "/session/token":
            return R({"status": "error", "error_type": "TokenException", "message": "Token expired"}, 403)
        if path == "/session/token":
            import hashlib
            assert data["checksum"] == hashlib.sha256(b"kkeyRTOKssecret").hexdigest()
            return R({"status": "success", "data": {"access_token": "AT1", "user_id": "AB1234"}})
        data = {"/trades": K["trades"], "/gtt/triggers": K["gtts"], "/portfolio/holdings": K["holdings"],
                "/portfolio/positions": {"net": [], "day": K["day"]}}[path]
        return R({"status": "success", "data": data})
    def get(self, url, **k): return self._r(url)
    def post(self, url, data=None, **k): return self._r(url, data)
    def delete(self, url, **k): return R({"status": "success", "data": True})


broker._http = FakeKite()
n = iter(range(1000, 9999))


def trade(sym, side, qty, price, days_ago=0, order=None, product="CNC"):
    return {"trade_id": str(next(n)), "order_id": order or str(next(n)), "exchange": "NSE", "tradingsymbol": sym,
            "transaction_type": side, "quantity": qty, "average_price": price, "product": product,
            "fill_timestamp": f"{D(days_ago)} 10:0{len(K['trades']) % 10}:00"}


def gtt(sym, lo, hi=None, lp=500):
    return {"id": next(n), "type": "two-leg" if hi else "single", "status": "active",
            "condition": {"exchange": "NSE", "tradingsymbol": sym, "trigger_values": [lo] + ([hi] if hi else []), "last_price": lp},
            "orders": [{"transaction_type": "SELL", "quantity": 1, "product": "CNC"}]}


def opn(sym):
    return [p for p in db.tables.get("positions", []) if p["symbol"] == sym and p["status"] == "open"]


def ok(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    assert cond, msg


import json  # noqa: E402
scan.save_market_note("NSE", json.dumps(["Up", "Chop"]), "Normal", json.dumps({"up45": 30, "down45": 10}), "Power", "Nifty above rising 10 and 20 EMA, breadth decent, breakouts holding up nicely today.")
scan.run_scan("NSE")
m = broker.mcfg_for("NSE")

print("login / callback")
u = broker.kite_login()["url"]
state = parse_qs(parse_qs(urlparse(u).query)["redirect_params"][0])["state"][0]
broker.kite_callback(request_token="RTOK", status="success", state=state, type="login")
ok(frappe.local.response["location"].endswith("kite=ok") and PW["kite_access_token"] == "AT1", "token stored, redirected ok")
broker.kite_callback(request_token="RTOK", status="success", state="forged")
ok(frappe.local.response["location"].endswith("kite=badstate"), "forged state rejected")
ok(broker.status()["connected"] and broker.status()["user"] == "AB1234", "status: connected")
S["kite_sync_from"] = D(30)

print("1 · buy adopts the position typed in by hand")
positions.add("NSE", "AAA", D(1), 101, 0, 0, "EP", "my note")
K["trades"] = [trade("AAA", "BUY", 40, 100.5, 2, "o1"), trade("AAA", "BUY", 60, 100.0, 2, "o1")]   # one order, two fills
K["gtts"] = [gtt("AAA", 95, 120, 100)]
r = broker.sync_now(); print("   ", r["message"])
a = opn("AAA")
ok(len(a) == 1 and a[0]["qty_open"] == 100 and abs(a[0]["avg_price"] - 100.2) < 1e-6 and a[0]["broker"] == "zerodha", "adopted, VWAP 100.2 × 100")
ok(a[0]["stop"] == 95 and a[0]["init_stop"] == 95 and a[0]["note"] == "my note" and a[0]["setup"] == "EP", "stop from the OCO GTT, note/setup kept")

print("2 · same trades again → nothing twice")
r = broker.sync_now()
ok(len(db.tables["broker_fills"]) == 2 and opn("AAA")[0]["qty_open"] == 100, "no duplicates")

print("3 · new buy → new position; then add to it")
K["trades"] += [trade("BBB", "BUY", 10, 200, 3, "o2")]
K["gtts"] += [gtt("BBB", 190, None, 200)]
broker.sync_now()
b = opn("BBB")
ok(len(b) == 1 and b[0]["stop"] == 190 and b[0]["note"] == "Imported from Zerodha", "new position with GTT stop")
K["trades"] += [trade("BBB", "BUY", 10, 220, 2, "o3")]
broker.sync_now()
b = opn("BBB")
ok(b[0]["qty_open"] == 20 and abs(b[0]["avg_price"] - 210) < 1e-6, "averaged to 20 @ 210")

print("4 · partial sell splits, full sell closes")
K["trades"] += [trade("AAA", "SELL", 30, 110, 0, "o4")]
broker.sync_now()
a = opn("AAA")
ok(len(a) == 1 and a[0]["qty_open"] == 70 and positions.pos.REMAINDER in a[0]["note"], "70 left on a remainder row")
ex = [e for e in db.tables["position_exits"] if e["price"] == 110]
ok(len(ex) == 1 and ex[0]["qty"] == 30, "exit 30 @ 110 recorded")
K["trades"] += [trade("AAA", "SELL", 70, 115, 0, "o5")]
broker.sync_now()
ok(not opn("AAA"), "AAA closed")

print("5 · sells with no position, MIS ignored")
K["trades"] += [trade("ZZZ", "SELL", 5, 50, 0, "o6"), trade("MMM", "BUY", 5, 50, 0, "o7", "MIS"), trade("MMM", "SELL", 5, 51, 0, "o8", "MIS")]
broker.sync_now()
st = broker.status()
ok(any(u_["symbol"] == "ZZZ" for u_ in st["unmatched"]) and not [p for p in db.tables["positions"] if p["symbol"] == "MMM"],
   "ZZZ unmatched, intraday MMM skipped (intraday off)")
broker.dismiss_fill(st["unmatched"][0]["id"])
ok(not broker.status()["unmatched"], "dismissed")

print("6 · GTT moves → stop follows; a stop typed in the app stays while the GTT doesn't move")
bid = opn("BBB")[0]["id"]
positions.save_rows("NSE", [{"id": bid, "Stop": 200}])
broker.sync_now()
ok(opn("BBB")[0]["stop"] == 200 and opn("BBB")[0]["stop_source"] == "manual", "manual 200 kept")
ok(any(x["symbol"] == "BBB" for x in broker.status()["checks"]["stop_differs"]), "…and flagged as different from the GTT")
K["gtts"] = [gtt("BBB", 205, None, 230)]
broker.sync_now()
ok(opn("BBB")[0]["stop"] == 205 and opn("BBB")[0]["stop_source"] == "gtt", "GTT moved to 205 → stop 205")

print("7 · holdings check")
K["holdings"] = [{"tradingsymbol": "BBB", "exchange": "NSE", "quantity": 15, "t1_quantity": 0},
                 {"tradingsymbol": "LONGTERM", "exchange": "NSE", "quantity": 3, "t1_quantity": 0}]
K["trades"] = []
broker.sync_now()
ck = broker.status()["checks"]
ok(ck["qty_mismatch"] == [{"symbol": "BBB", "app": 20.0, "zerodha": 15.0}], "BBB 20 vs 15 flagged")
ok([x["symbol"] for x in ck["not_in_app"]] == ["LONGTERM"], "LONGTERM not in app")
S["kite_ignore"] = "LONGTERM"; broker.sync_now()
ok(not broker.status()["checks"]["not_in_app"], "ignored")

print("8 · tradebook back-fill")
positions.add("NSE", "CCC", D(20), 300, 10, 280, "Breakout", "")
cid = opn("CCC")[0]["id"]
positions.save_rows("NSE", [{"id": cid, "Close": True, "Sell price": 330, "Close date": D(12)}])
csv_text = ("symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n"
            f"CCC,INE1,{D(20)},NSE,EQ,EQ,buy,false,10,299.5,T1,O1,{D(20)}T09:20:00\n"
            f"CCC,INE1,{D(12)},NSE,EQ,EQ,sell,false,10,331,T2,O2,{D(12)}T11:20:00\n"
            f"DDD,INE2,{D(9)},NSE,EQ,EQ,buy,false,4,1000,T3,O3,{D(9)}T10:00:00\n"
            f"DDD,INE2,{D(7)},NSE,EQ,EQ,sell,false,4,1100,T4,O4,{D(7)}T10:00:00\n")
r = broker.import_tradebook(csv_text); print("   ", r["message"])
ccc = [p for p in db.tables["positions"] if p["symbol"] == "CCC"]
ok(len(ccc) == 1 and ccc[0]["broker"] == "zerodha", "CCC already logged → linked, not duplicated")
ok(not any(u_["symbol"] == "CCC" for u_ in broker.status()["unmatched"]), "CCC sale linked too")
ddd = [p for p in db.tables["positions"] if p["symbol"] == "DDD"]
ok(len(ddd) == 1 and ddd[0]["status"] == "closed", "DDD imported as a closed trade")
r = broker.import_tradebook(csv_text)
ok("0 new" in r["message"], "same file twice → 0 new")
aaa = sorted([p for p in db.tables["positions"] if p["symbol"] == "AAA" and p["status"] != "imported"], key=lambda p: p["qty"])
aex = sorted((e["qty"], e["price"]) for e in db.tables["position_exits"] if e["position_id"] in [p["id"] for p in aaa])
ok(len(aaa) == 1 and aaa[0]["status"] == "closed" and aex == [(100, 113.5)], "replay: AAA's two sales the same day = one exit 100 @ 113.50")
ok(all(p["note"].startswith("my note") and p["setup"] == "EP" for p in aaa), "replay kept your note and setup")
ok(len([p for p in db.tables["positions"] if p["symbol"] == "BBB" and p["status"] == "open"]) == 1, "BBB still one open position")
api_dup = ("symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n"
           f"BBB,INE3,{D(3)},NSE,EQ,EQ,buy,false,10,200,XX-OTHER-ID,O9,{D(3)}T10:02:00\n")
r = broker.import_tradebook(api_dup)
ok("0 new" in r["message"], "tradebook row already synced from the API (different id) → not added twice")

print("8b · intraday: MIS round trip + a short")
K["trades"] = [trade("III", "BUY", 50, 100, 0, "m1", "MIS"), trade("III", "SELL", 50, 103, 0, "m2", "MIS"),
               trade("SSS", "SELL", 20, 500, 0, "m3", "MIS"), trade("SSS", "BUY", 20, 490, 0, "m4", "MIS")]
for i, t in enumerate(K["trades"]):
    t["fill_timestamp"] = f"{TODAY} 11:0{i}:00"
broker.sync_now()
ok(not [p for p in db.tables["positions"] if p["symbol"] == "III"], "MIS skipped while intraday is off")
r = broker.set_products(1)
iii = [p for p in db.tables["positions"] if p["symbol"] == "III"]
sss = [p for p in db.tables["positions"] if p["symbol"] == "SSS"]
ok(len(iii) == 1 and iii[0]["status"] == "closed" and iii[0]["setup"] == "Intraday", "III: intraday long, closed")
sx = [e for e in db.tables["position_exits"] if e["position_id"] == sss[0]["id"]] if sss else []
ok(len(sss) == 1 and sss[0]["avg_price"] == 490 and sx and sx[0]["price"] == 500 and "short" in sss[0]["note"], "SSS: short 500 → 490 = +200")
pg = positions.get("NSE")
ok("net P&L" in pg["closed"]["title"] and "P&L" in pg["closed"]["table"]["columns"], "closed trades show P&L: " + pg["closed"]["title"])
broker.set_products(0)
ok(not [p for p in db.tables["positions"] if p["symbol"] in ("III", "SSS")], "intraday off again → removed")
S["kite_products"] = "CNC,MIS"                               # typed in GTT Broker Settings, not via the tick box
broker.sync_now()
ok(len([p for p in db.tables["positions"] if p["symbol"] in ("III", "SSS")]) == 2, "settings edited by hand → next sync replays")
S["kite_products"] = "CNC"; broker.sync_now()

print("8c · bought 100 intraday, sold 40 the same day, carried 60 → sold next day")
S["kite_products"] = "CNC,MIS"
csv2 = ("symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time\n"
        f"CRY,INE9,{D(6)},NSE,EQ,EQ,buy,false,100,50,C1,CO1,{D(6)}T09:20:00\n"
        f"CRY,INE9,{D(6)},NSE,EQ,EQ,sell,false,40,53,C2,CO2,{D(6)}T13:00:00\n"
        f"CRY,INE9,{D(4)},NSE,EQ,EQ,sell,false,60,58,C3,CO3,{D(4)}T10:00:00\n")
broker.import_tradebook(csv2)
cry = sorted([p for p in db.tables["positions"] if p["symbol"] == "CRY"], key=lambda p: p["qty"])
cex = {p["id"]: [(e["qty"], e["price"]) for e in db.tables["position_exits"] if e["position_id"] == p["id"]] for p in cry}
ok(len(cry) == 2 and cry[0]["setup"] == "Intraday" and cex[cry[0]["id"]] == [(40, 53)], "40 intraday @ 50 → 53")
ok(cry[1]["qty"] == 60 and cry[1]["entry_date"] == D(6) and cex[cry[1]["id"]] == [(60, 58)] and cry[1]["status"] == "closed",
   "60 carried from the same entry, sold 2 days later @ 58")
n_before = len(db.tables["positions"]) + len(db.tables["position_exits"])
broker.sync_now(); broker.sync_now()
ok(len(db.tables["positions"]) + len(db.tables["position_exits"]) == n_before, "replays that change nothing write nothing new")

print("9 · positions.get carries the broker card; token expiry")
pg = positions.get("NSE")
ok(pg["broker"]["connected"] and "checks" in pg["broker"], "positions.get → broker")
K["expired"] = True
r = broker.sync_now()
ok(not r["ok"] and not broker.status()["connected"], "expired token → disconnected: " + r["message"])
S["kite_token_date"] = D(1) + " 09:00:00"; PW["kite_access_token"] = "OLD"; S["kite_access_token"] = "****"
ok(broker._token() is None, "yesterday's token treated as expired")
print("all broker tests passed")
