"""
NSE price bands (circuit %) from NSE's own daily security list, instead of MarketInOut's _circuit
(which is often stale or wrong).

    https://nsearchives.nseindia.com/content/equities/sec_list_DDMMYYYY.csv
    columns: Symbol, Series, Security Name, Band, Remarks   (Band = 2/5/10/20 or "No Band")

Once per session (when the NSE scanner runs) the newest file is downloaded — today's if NSE has published it,
otherwise the previous weekday, and so on back up to 10 weekdays (holidays have no file) — and stored in the
Supabase table nse_price_bands (supabase_nse_bands.sql). The scan's _circuit column is then taken from that table.
"""
import io
from datetime import date, datetime, timedelta

import pandas as pd

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

URL = "https://nsearchives.nseindia.com/content/equities/sec_list_{d:%d%m%Y}.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/128.0 Safari/537.36",
           "Accept": "text/csv,application/octet-stream,*/*", "Referer": "https://www.nseindia.com/"}
NO_BAND = 100.0            # shown as 100 in Circuit % = no price band (F&O stocks move freely)
SERIES_ORDER = ["EQ", "BE", "BZ", "SM", "ST"]
MAX_BACK = 10              # weekdays to look back for a published file


def _today_ist():
    return datetime.now(ZoneInfo("Asia/Kolkata")).date() if ZoneInfo else date.today()


def weekdays_back(start, n=MAX_BACK):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def parse_sec_list(text):
    df = pd.read_csv(io.StringIO(text), dtype=str)
    df.columns = [str(c).strip().strip('"') for c in df.columns]
    low = {c.lower(): c for c in df.columns}
    sym = low.get("symbol")
    band = next((low[c] for c in low if "band" in c), None)
    if not sym or not band:
        raise ValueError(f"unexpected columns {list(df.columns)}")
    ser = low.get("series")
    name = next((low[c] for c in low if "name" in c), None)
    rem = next((low[c] for c in low if "remark" in c), None)
    out = pd.DataFrame({
        "symbol": df[sym].str.strip().str.upper(),
        "series": df[ser].str.strip().str.upper() if ser else "EQ",
        "name": df[name].str.strip() if name else None,
        "band_text": df[band].str.strip(),
        "remarks": df[rem].str.strip() if rem else None,
    })
    out["band"] = pd.to_numeric(out["band_text"], errors="coerce")
    out = out[out["symbol"].notna() & (out["symbol"] != "")]
    return out.drop_duplicates(["symbol", "series"])


def fetch_sec_list(d, timeout=20):
    """DataFrame for that date's file, or None if NSE has no file (holiday / not published yet)."""
    import requests
    r = requests.get(URL.format(d=d), headers=HEADERS, timeout=timeout)
    if r.status_code != 200 or "<html" in r.text[:300].lower() or len(r.text) < 500:
        return None
    return parse_sec_list(r.text)


def latest_stored_date(sb):
    rows = sb.table("nse_price_bands").select("file_date").order("file_date", desc=True).limit(1).execute().data
    return date.fromisoformat(str(rows[0]["file_date"])[:10]) if rows else None


def store(sb, df, d):
    rows = [{"symbol": r.symbol, "series": r.series, "name": r.name if isinstance(r.name, str) else None,
             "band": None if pd.isna(r.band) else float(r.band), "band_text": r.band_text,
             "remarks": r.remarks if isinstance(r.remarks, str) else None, "file_date": d.isoformat()}
            for r in df.itertuples(index=False)]
    for i in range(0, len(rows), 500):
        sb.table("nse_price_bands").upsert(rows[i:i + 500], on_conflict="symbol,series").execute()
    sb.table("nse_price_bands").delete().lt("file_date", d.isoformat()).execute()   # securities no longer listed
    return len(rows)


def refresh_bands(sb, today=None, fetch=fetch_sec_list):
    """Download the newest file not yet stored. Returns (file_date in DB, message)."""
    have = latest_stored_date(sb)
    for d in weekdays_back(today or _today_ist()):
        if have and d <= have:
            return have, f"price bands up to date ({have:%d-%b})"
        try:
            df = fetch(d)
        except Exception as e:
            return have, f"couldn't reach NSE ({type(e).__name__}) — using bands from {have:%d-%b}" if have else \
                f"couldn't reach NSE ({type(e).__name__}) — using MarketInOut's circuit %"
        if df is not None and not df.empty:
            n = store(sb, df, d)
            return d, f"price bands updated from NSE's {d:%d-%b} list ({n} securities)"
    return have, "no NSE security list found in the last 10 weekdays"


def load_bands(sb):
    """symbol → band % (NO_BAND for 'No Band'), preferring the EQ series."""
    rows, start = [], 0
    while True:
        chunk = (sb.table("nse_price_bands").select("symbol,series,band,band_text")
                 .range(start, start + 999).execute().data or [])
        rows += chunk
        if len(chunk) < 1000:
            break
        start += 1000
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    df["_o"] = df["series"].map({s: i for i, s in enumerate(SERIES_ORDER)}).fillna(len(SERIES_ORDER))
    df = df.sort_values("_o").drop_duplicates("symbol")
    nb = df["band_text"].astype(str).str.lower().str.contains("no band")
    df["v"] = pd.to_numeric(df["band"], errors="coerce")
    df.loc[nb, "v"] = NO_BAND
    return {s: v for s, v in zip(df["symbol"], df["v"]) if pd.notna(v)}


def apply_nse_bands(st, sb, mcfg, df):
    """NSE page: refresh the table once per session, then overwrite df['_circuit'] with NSE's band."""
    if mcfg.get("market") != "NSE" or sb is None or df is None or df.empty or "Symbol" not in df.columns:
        return df
    key = f"nse_bands_checked_{_today_ist()}"
    try:
        if not st.session_state.get(key):
            _, msg = refresh_bands(sb)
            st.session_state[key] = msg
        bands = load_bands(sb)
    except Exception as e:
        st.caption(f"NSE price bands unavailable ({e}). Run supabase_nse_bands.sql once — using MarketInOut's circuit %.")
        return df
    if not bands:
        return df
    df = df.copy()
    mio = pd.to_numeric(df["_circuit"], errors="coerce") if "_circuit" in df.columns else pd.Series(index=df.index, dtype=float)
    nse = df["Symbol"].astype(str).str.upper().map(bands)
    df["_circuit"] = nse.where(nse.notna(), mio)
    diff = int(((nse.notna()) & (mio.notna()) & (nse != mio)).sum())
    st.caption(f"Circuit %: {st.session_state.get(key)} · {int(nse.notna().sum())} of {len(df)} scanned matched"
               + (f" · {diff} differ from MarketInOut" if diff else "") + " · 100 = no band (F&O)")
    return df
