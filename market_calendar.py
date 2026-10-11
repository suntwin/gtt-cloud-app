"""market_calendar.py — exchange holidays and session dates for NSE and USA (NYSE/Nasdaq).

Put this file at the repo root (next to gtt_process.py, NOT in pages/).

Used for:
  1. The market-note gate: ask for the note of the LAST COMPLETED session only.
     The session rolls over after the close (+ buffer), never at the open, and
     weekends/holidays are skipped, so the same day is never asked for twice.
  2. A banner showing upcoming holidays / early closes in advance.

Sources (checked 2026-10-11):
  NSE  : NSE circular for 2026 (equity + equity derivatives). NSE publishes the
         next year's list in late December. Add NSE 2027 then.
  NYSE : nyse.com/markets/hours-calendars (2026, 2027, 2028 published).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- holidays
HOLIDAYS: dict[str, dict[date, str]] = {
    "NSE": {
        date(2026, 1, 15): "Municipal Corporation Election (Maharashtra)",
        date(2026, 1, 26): "Republic Day",
        date(2026, 3, 3): "Holi",
        date(2026, 3, 26): "Shri Ram Navami",
        date(2026, 3, 31): "Shri Mahavir Jayanti",
        date(2026, 4, 3): "Good Friday",
        date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
        date(2026, 5, 1): "Maharashtra Day",
        date(2026, 5, 28): "Bakri Id",
        date(2026, 6, 26): "Muharram",
        date(2026, 9, 14): "Ganesh Chaturthi",
        date(2026, 10, 2): "Mahatma Gandhi Jayanti",
        date(2026, 10, 20): "Dussehra",
        date(2026, 11, 10): "Diwali Balipratipada",
        date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
        date(2026, 12, 25): "Christmas",
    },
    "USA": {
        # 2026
        date(2026, 1, 1): "New Year's Day",
        date(2026, 1, 19): "Martin Luther King Jr. Day",
        date(2026, 2, 16): "Washington's Birthday",
        date(2026, 4, 3): "Good Friday",
        date(2026, 5, 25): "Memorial Day",
        date(2026, 6, 19): "Juneteenth",
        date(2026, 7, 3): "Independence Day (observed)",
        date(2026, 9, 7): "Labor Day",
        date(2026, 11, 26): "Thanksgiving Day",
        date(2026, 12, 25): "Christmas Day",
        # 2027
        date(2027, 1, 1): "New Year's Day",
        date(2027, 1, 18): "Martin Luther King Jr. Day",
        date(2027, 2, 15): "Washington's Birthday",
        date(2027, 3, 26): "Good Friday",
        date(2027, 5, 31): "Memorial Day",
        date(2027, 6, 18): "Juneteenth (observed)",
        date(2027, 7, 5): "Independence Day (observed)",
        date(2027, 9, 6): "Labor Day",
        date(2027, 11, 25): "Thanksgiving Day",
        date(2027, 12, 24): "Christmas Day (observed)",
        # 2028 (no New Year's Day holiday: Jan 1 2028 is a Saturday)
        date(2028, 1, 17): "Martin Luther King Jr. Day",
        date(2028, 2, 21): "Washington's Birthday",
        date(2028, 4, 14): "Good Friday",
        date(2028, 5, 29): "Memorial Day",
        date(2028, 6, 19): "Juneteenth",
        date(2028, 7, 4): "Independence Day",
        date(2028, 9, 4): "Labor Day",
        date(2028, 11, 23): "Thanksgiving Day",
        date(2028, 12, 25): "Christmas Day",
    },
}

# Early closes: date -> local close time
EARLY_CLOSES: dict[str, dict[date, time]] = {
    "NSE": {},
    "USA": {
        date(2026, 11, 27): time(13, 0),
        date(2026, 12, 24): time(13, 0),
        date(2027, 11, 26): time(13, 0),
        date(2028, 7, 3): time(13, 0),
        date(2028, 11, 24): time(13, 0),
    },
}

# Special weekend sessions (shown in the banner, NOT counted as sessions,
# so they never ask for a market note).
SPECIAL_SESSIONS: dict[str, dict[date, str]] = {
    "NSE": {date(2026, 11, 8): "Muhurat trading (Diwali, special 1-hour session)"},
    "USA": {},
}

# Last year each list is complete for (warn when we run past it).
COVERED_UNTIL = {"NSE": 2026, "USA": 2028}

TZ = {"NSE": ZoneInfo("Asia/Kolkata"), "USA": ZoneInfo("America/New_York")}
CLOSE = {"NSE": time(15, 30), "USA": time(16, 0)}
BUFFER = timedelta(minutes=30)  # breadth screens settle after the close


def _mkt(market: str) -> str:
    m = market.upper()
    return "USA" if m in ("US", "USA", "NYSE", "NASDAQ") else m


def is_trading_day(market: str, d: date) -> bool:
    m = _mkt(market)
    return d.weekday() < 5 and d not in HOLIDAYS.get(m, {})


def close_time(market: str, d: date) -> time:
    m = _mkt(market)
    return EARLY_CLOSES.get(m, {}).get(d, CLOSE[m])


def previous_trading_day(market: str, d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(market, d):
        d -= timedelta(days=1)
    return d


def last_completed_session(market: str, now: datetime | None = None) -> date:
    """The session whose closing data (and breadth counts) is final.

    Rolls over at close + BUFFER in the exchange's own time zone, so:
    weekend, Monday before the close and a weekday holiday all return the
    previous trading day (e.g. Friday) and never ask for a duplicate note.
    """
    m = _mkt(market)
    now = (now or datetime.now(TZ[m])).astimezone(TZ[m])
    today = now.date()
    if is_trading_day(m, today):
        cutoff = datetime.combine(today, close_time(m, today), TZ[m]) + BUFFER
        if now >= cutoff:
            return today
    return previous_trading_day(m, today)


def upcoming_events(market: str, today: date | None = None, days: int = 30):
    """[(date, kind, name)] within `days`; kind is HOLIDAY / EARLY CLOSE / SPECIAL."""
    m = _mkt(market)
    today = today or datetime.now(TZ[m]).date()
    end = today + timedelta(days=days)
    ev = [(d, "HOLIDAY", n) for d, n in HOLIDAYS[m].items() if today <= d <= end]
    ev += [(d, "EARLY CLOSE", f"closes {t.strftime('%H:%M')} local")
           for d, t in EARLY_CLOSES[m].items() if today <= d <= end]
    ev += [(d, "SPECIAL", n) for d, n in SPECIAL_SESSIONS[m].items() if today <= d <= end]
    return sorted(ev)


def calendar_warning(market: str, today: date | None = None) -> str | None:
    m = _mkt(market)
    today = today or datetime.now(TZ[m]).date()
    if today.year > COVERED_UNTIL[m] or (today.month == 12 and today.year == COVERED_UNTIL[m]):
        return (f"{m} holiday list only covers up to {COVERED_UNTIL[m]}. "
                f"Add next year's holidays to market_calendar.py.")
    return None


def holiday_banner_lines(market: str, today: date | None = None, days: int = 30) -> list[str]:
    """Plain-text lines for st.info / st.caption."""
    lines = [f"{d.strftime('%a %d %b')}: {kind.title()}, {name}"
             for d, kind, name in upcoming_events(market, today, days)]
    w = calendar_warning(market, today)
    if w:
        lines.append("⚠️ " + w)
    return lines


def is_duplicate_breadth(prev: dict | None, new: dict, keys=None) -> bool:
    """True when all filled counts equal the previous session's (holiday repeat)."""
    if not prev:
        return False
    keys = keys or ["up45", "down45", "up20", "down20",
                    "above20", "below20", "above50", "below50"]
    pairs = [(prev.get(k), new.get(k)) for k in keys if new.get(k) is not None]
    return bool(pairs) and all(a == b for a, b in pairs)


# Streamlit helper ---------------------------------------------------------
def render_holiday_banner(market: str, days: int = 30) -> None:
    import streamlit as st
    lines = holiday_banner_lines(market, days=days)
    if lines:
        st.info(f"📅 Upcoming {_mkt(market)} market days off (next {days} days)\n\n"
                + "\n".join(f"- {l}" for l in lines))
