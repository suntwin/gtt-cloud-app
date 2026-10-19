"""
Copy your active TradingView alerts into Supabase so the dashboard's Alerts tab can show them.

Runs on YOUR PC (not Streamlit Cloud): it reads TradingView Desktop through tradingview-mcp's CLI,
which talks to TradingView on the debug port 9222. TradingView must be open in debug mode.

    venv\\Scripts\\python.exe sync_tv_alerts.py            # one sync
    venv\\Scripts\\python.exe sync_tv_alerts.py --loop 15  # sync every 15 minutes until you close it

Settings (optional environment variables):
    TVMCP_DIR  folder of tradingview-mcp  (default C:\\Tools\\tradingview-mcp\\tradingview-mcp)
    NODE_EXE   node executable            (default: node on PATH, else C:\\Program Files\\nodejs\\node.exe)
Supabase URL/key come from .streamlit/secrets.toml (same as the app). Standard library only - no pip installs.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TVMCP_DIR = Path(os.environ.get("TVMCP_DIR", r"C:\Tools\tradingview-mcp\tradingview-mcp"))
CRYPTO_EXCHANGES = {"BINANCE", "BYBIT", "OKX", "COINBASE", "KRAKEN", "BITGET", "MEXC", "KUCOIN", "GATEIO",
                    "BITSTAMP", "BITFINEX", "HTX", "HUOBI", "CRYPTO", "CRYPTOCAP", "PYTH", "DELTAIN", "WOONETWORK"}
INDIA_EXCHANGES = {"NSE", "BSE"}


def log(msg):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def read_secrets():
    """SUPABASE_URL / SUPABASE_KEY from env or .streamlit/secrets.toml (simple key = "value" lines)."""
    out = {k: os.environ.get(k) for k in ("SUPABASE_URL", "SUPABASE_KEY")}
    f = HERE / ".streamlit" / "secrets.toml"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r'\s*(SUPABASE_URL|SUPABASE_KEY)\s*=\s*["\']?([^"\'#]+?)["\']?\s*(#.*)?$', line)
            if m and not out.get(m.group(1)):
                out[m.group(1)] = m.group(2).strip()
    if not out["SUPABASE_URL"] or not out["SUPABASE_KEY"]:
        sys.exit("SUPABASE_URL / SUPABASE_KEY not found (.streamlit/secrets.toml).")
    return out


class Rest:
    """Tiny Supabase (PostgREST) client using only the standard library - no pip installs needed."""
    def __init__(self, url, key):
        self.base, self.key = url.rstrip("/") + "/rest/v1/", key

    def _call(self, method, table, query="", body=None, prefer=None):
        import urllib.request, urllib.error
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        if prefer:
            h["Prefer"] = prefer
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(self.base + table + (("?" + query) if query else ""), data=data,
                                     headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = r.read().decode() or "null"
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Supabase {method} {table}: {e.code} {e.read().decode()[:300]}") from None
        return json.loads(txt) if txt.strip() else None

    def upsert(self, table, rows, on_conflict):
        return self._call("POST", table, f"on_conflict={on_conflict}", rows,
                          "resolution=merge-duplicates,return=minimal")

    def insert(self, table, row):
        return self._call("POST", table, "", row, "return=minimal")

    def select(self, table, cols):
        return self._call("GET", table, f"select={cols}") or []

    def delete(self, table, query):
        return self._call("DELETE", table, query, None, "return=minimal")


def node_exe():
    if os.environ.get("NODE_EXE"):
        return os.environ["NODE_EXE"]
    return shutil.which("node") or r"C:\Program Files\nodejs\node.exe"


def fetch_alerts():
    """Run `tv alert list` from tradingview-mcp and return the alerts list (raises on failure)."""
    cli = TVMCP_DIR / "src" / "cli" / "index.js"
    if not cli.exists():
        raise RuntimeError(f"tradingview-mcp CLI not found at {cli} (set TVMCP_DIR)")
    flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW: no console flash
    p = subprocess.run([node_exe(), str(cli), "alert", "list"], cwd=str(TVMCP_DIR), capture_output=True,
                       text=True, encoding="utf-8", timeout=90, creationflags=flags)
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip()[-400:]
        if p.returncode == 2:
            msg = "TradingView isn't reachable on port 9222 - open it in debug mode. " + msg
        raise RuntimeError(msg or f"tv alert list failed (exit {p.returncode})")
    data = json.loads(p.stdout)
    if data.get("error") and not data.get("alerts"):
        raise RuntimeError(f"TradingView returned: {data['error']} (are you logged in?)")
    return data.get("alerts") or []


def market_of(exchange):
    ex = exchange.upper()
    if ex in INDIA_EXCHANGES:
        return "NSE"
    if ex in CRYPTO_EXCHANGES:
        return "CRYPTO"
    return "USA"


def to_row(a, now_iso):
    sym = str(a.get("symbol") or "")
    ex, tk = sym.split(":", 1) if ":" in sym else ("", sym)
    cond = a.get("condition") or {}
    series = cond.get("series") or []
    line = next((s for s in series if isinstance(s, dict) and s.get("type") == "line"), {})
    p1, p2 = line.get("price1"), line.get("price2")
    if p1 is None:                                        # plain price alert: the value sits in the series
        val = next((s.get("value") for s in series if isinstance(s, dict) and "value" in s), None)
        p1 = p2 = val
    return {
        "alert_id": int(a["alert_id"]), "symbol": sym, "exchange": ex, "ticker": tk, "market": market_of(ex),
        "resolution": a.get("resolution"), "alert_type": a.get("type"),
        "line_tool": (line.get("tool") or "").replace("LineTool", "") or None,
        "price1": p1, "price2": p2, "cond_type": cond.get("type"), "frequency": cond.get("frequency"),
        "message": a.get("message"), "active": bool(a.get("active", True)),
        "created": a.get("created"), "last_fired": a.get("last_fired"), "expiration": a.get("expiration"),
        "raw": a, "synced_at": now_iso,
    }


def sync_once(sb):
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        alerts = fetch_alerts()
    except Exception as e:
        log(f"FAILED: {e}")
        try:
            sb.insert("tv_alert_sync", {"n_alerts": 0, "ok": False, "error": str(e)[:500]})
        except Exception as e2:
            log(f"(could not log the failure to Supabase: {e2})")
        return False
    rows = [to_row(a, now_iso) for a in alerts if a.get("alert_id") is not None]
    for i in range(0, len(rows), 200):
        sb.upsert("tv_alerts", rows[i:i + 200], "alert_id")
    keep = {r["alert_id"] for r in rows}
    have = [r["alert_id"] for r in sb.select("tv_alerts", "alert_id")]
    gone = [x for x in have if x not in keep]               # deleted (or expired) in TradingView
    for i in range(0, len(gone), 200):
        sb.delete("tv_alerts", "alert_id=in.(" + ",".join(str(x) for x in gone[i:i + 200]) + ")")
    sb.insert("tv_alert_sync", {"n_alerts": len(rows), "ok": True})
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
    from urllib.parse import quote
    sb.delete("tv_alert_sync", "synced_at=lt." + quote(cutoff))
    fired = sum(1 for r in rows if r["last_fired"])
    log(f"Synced {len(rows)} alerts ({fired} triggered), removed {len(gone)} that are gone from TradingView.")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", type=float, default=0, help="minutes between syncs (0 = sync once and exit)")
    args = ap.parse_args()
    s = read_secrets()
    sb = Rest(s["SUPABASE_URL"], s["SUPABASE_KEY"])
    ok = sync_once(sb)
    while args.loop > 0:
        time.sleep(args.loop * 60)
        ok = sync_once(sb)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
