"""Earnings tab: company KPI ladders on Kalshi, with an earnings calendar.

Kalshi has no EPS or revenue markets. What it lists around earnings are
"above X" ladders on a figure the company reports with its results
(comparable sales, deliveries, subscribers, ...), tagged "KPIs" in Kalshi's
series metadata. Each ladder is a probability distribution (see macro.py).

Only the most liquid ladders are kept: enough tight strikes and enough open
interest. Report dates come from Nasdaq's public earnings calendar, which
needs no key; projected dates may move until the company confirms.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from .macro import MAX_SPREAD, _dist_pair, _f, market_mid

KPI_TAG = "KPIs"
GREATER = ("greater", "greater_or_equal")
MIN_STRIKES = 4          # strikes with a usable spread (<= MAX_SPREAD)
MIN_OPEN_INTEREST = 500  # contracts outstanding across those strikes
MAX_ITEMS = 30           # keep the most liquid ladders only
TIER_HIGH, TIER_MEDIUM = 5000, 1500   # open interest, contracts
CALENDAR_LOOKBACK = 14   # ladders stay open after a report, until the figure is confirmed
CALENDAR_DAYS = 120      # how far ahead to ask Nasdaq for report dates
NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
NASDAQ_HEADERS = {   # the endpoint rejects requests without browser-like headers
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
NASDAQ_HOURS = {"time-pre-market": "bmo", "time-after-hours": "amc"}

_COMPANY = re.compile(r"^Will (.+?) report ", re.I)
_PERIOD = re.compile(r"\bin\s+(Q[1-4](?:\s+\d{4})?|fiscal\s+\d{4}|\d{4})\s*$", re.I)
_TICKER_MONTH = re.compile(r"-(\d{2})([A-Z]{3})")
_MONTHS = {m: i + 1 for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}


def _num(v) -> float:
    """Like macro._f, but missing or malformed values count as 0."""
    return _f(v) or 0.0


def tier_of(open_interest: float) -> str:
    return "high" if open_interest >= TIER_HIGH else "medium" if open_interest >= TIER_MEDIUM else "low"


def _unit_of(markets: list[dict]) -> str:
    """'%' or '$' when the strike labels carry one ("Above 3.2%"), else ''."""
    for m in markets:
        label = m.get("yes_sub_title") or ""
        if "%" in label:
            return "%"
        if "$" in label:
            return "$"
    return ""


def _report_month(event_ticker: str) -> date | None:
    """Month in the event ticker (KXWMT-27MAY...), roughly when the company reports."""
    m = _TICKER_MONTH.search(event_ticker)
    if not m or m.group(2) not in _MONTHS:
        return None
    return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], 15)


def expected_month(event_ticker: str, now: datetime) -> str | None:
    """'YYYY-MM' of the month in the event ticker, if it is within the next 13
    months. Quarterly tickers carry the report month; annual ones often carry a
    generic January a year or more out, which this drops."""
    d = _report_month(event_ticker)
    if not d:
        return None
    months_ahead = (d.year - now.year) * 12 + d.month - now.month
    return f"{d.year}-{d.month:02d}" if -1 <= months_ahead <= 13 else None


def _clamped(dist: dict, ladder: list[dict], unit: str) -> dict:
    """Counts and volumes can't go below zero, but the ladder's lower tail is
    extrapolated past its first strike and can."""
    if unit != "%" and min(_f(m.get("floor_strike")) for m in ladder) >= 0:
        for k in ("mean", "p10", "median", "p90"):
            dist[k] = max(dist[k], 0.0)
    return dist


def summarize_event(ev: dict) -> dict | None:
    """One KPI ladder with its liquidity and implied distribution, or None if
    the event isn't a liquid "above X" ladder."""
    ladder = [m for m in ev.get("markets") or []
              if m.get("status") == "active" and m.get("strike_type") in GREATER
              and _f(m.get("floor_strike")) is not None]
    usable = [m for m in ladder if market_mid(m, False, MAX_SPREAD) is not None]
    if len(usable) < MIN_STRIKES:
        return None
    open_interest = sum(_num(m.get("open_interest_fp")) for m in usable)
    if open_interest < MIN_OPEN_INTEREST:
        return None
    dist = _dist_pair(ladder, False, 1.0)
    if not dist:
        return None
    company = _COMPANY.match(ladder[0].get("title") or "")
    if not company:   # not "Will <company> report ...": not a figure the company reports
        return None
    title = ev.get("title") or ""
    period = _PERIOD.search(ev.get("sub_title") or "") or _PERIOD.search(title)
    spreads = sorted(_num(m.get("yes_ask_dollars")) - _num(m.get("yes_bid_dollars")) for m in usable)
    return {
        "event_ticker": ev["event_ticker"],
        "series_ticker": ev.get("series_ticker", ""),
        "url": f"https://kalshi.com/markets/{(ev.get('series_ticker') or '').lower()}",
        "company": company.group(1),
        "title": title,
        "metric": _PERIOD.sub("", title).strip() or title,
        "period": period.group(1) if period else "",
        "unit": _unit_of(ladder),
        "settles_by": min((m.get("expected_expiration_time") or m.get("close_time") or "" for m in ladder), default=""),
        "strikes_used": len(usable),
        "strikes_total": len(ladder),
        "open_interest": round(open_interest, 2),
        "volume_24h": round(sum(_num(m.get("volume_24h_fp")) for m in usable), 2),
        "median_spread": round(spreads[len(spreads) // 2], 4),
        "tier": tier_of(open_interest),
        "dist": _clamped({k: dist[k] for k in ("mean", "p10", "median", "p90", "low_confidence", "liquid_strikes",
                                               "change_24h")}, ladder, _unit_of(ladder)),
        "ladder": [{"strike": _f(m.get("floor_strike")), "mid": round(market_mid(m, False) or 0.0, 4),
                    "spread": round(_num(m.get("yes_ask_dollars")) - _num(m.get("yes_bid_dollars")), 4),
                    "open_interest": _num(m.get("open_interest_fp")), "used": m in usable}
                   for m in sorted(ladder, key=lambda m: _f(m.get("floor_strike")))],
        "expected_month": None,
        "report": None,
    }


# ------------------------------------------------------------------- Nasdaq --

def _nasdaq_day(day: date, timeout: float = 20.0) -> list[dict]:
    req = urllib.request.Request(f"{NASDAQ_URL}?date={day.isoformat()}", headers=NASDAQ_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        rows = ((json.loads(resp.read()).get("data") or {}).get("rows")) or []
    return [{"symbol": r.get("symbol", ""), "date": day.isoformat(),
             "hour": NASDAQ_HOURS.get(r.get("time"), "")} for r in rows if r.get("symbol")]


def fetch_nasdaq_calendar(start: date, end: date, workers: int = 4) -> tuple[list[dict], int]:
    """Every company reporting between start and end (weekdays only), from
    Nasdaq's public calendar. Returns (rows, number of days that failed)."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]

    def one(day: date):
        try:
            return _nasdaq_day(day)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, days))
    return [r for rows in results if rows for r in rows], sum(1 for rows in results if rows is None)


def symbol_candidates(series_ticker: str) -> list[str]:
    """Kalshi series tickers are KX + stock symbol, with an A appended for the annual series."""
    s = series_ticker[2:] if series_ticker.startswith("KX") else series_ticker
    return [s, s[:-1]] if len(s) > 1 else [s]


def match_report(item: dict, by_symbol: dict[str, list[dict]]) -> dict | None:
    """The calendar entry that belongs to this ladder: the symbol's report
    closest to the month in the event ticker, within a window around it."""
    month = _report_month(item["event_ticker"])
    if not month:
        return None
    best = None
    for sym in symbol_candidates(item["series_ticker"]):
        for row in by_symbol.get(sym, []):
            try:
                d = date.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            gap = (d - month).days
            if -25 <= gap <= 40 and (best is None or abs(gap) < best[0]):
                best = (abs(gap), sym, row)
        if best:
            break
    if not best:
        return None
    _, sym, row = best
    return {"symbol": sym, "date": row["date"], "hour": row.get("hour") or ""}


# -------------------------------------------------------------------- build --

def build_earnings(events: list[dict], now: datetime | None = None,
                   fetch_calendar=fetch_nasdaq_calendar) -> dict:
    """`events` are open events with nested markets from series tagged KPIs."""
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []
    items = [it for it in (summarize_event(ev) for ev in events) if it]
    ladders_found = len(items)
    items.sort(key=lambda i: -i["open_interest"])
    items = items[:MAX_ITEMS]

    cal = {"source": "Nasdaq", "entries": 0, "matched": 0, "failed_days": 0}
    by_symbol: dict[str, list[dict]] = {}
    try:
        rows, cal["failed_days"] = fetch_calendar(now.date() - timedelta(days=CALENDAR_LOOKBACK),
                                                  now.date() + timedelta(days=CALENDAR_DAYS))
        for row in rows:
            by_symbol.setdefault(row["symbol"], []).append(row)
        cal["entries"] = len(rows)
        if cal["failed_days"]:
            errors.append(f"nasdaq calendar: {cal['failed_days']} days could not be fetched")
    except Exception as e:  # the calendar is a nicety; keep the ladders
        errors.append(f"nasdaq calendar: {type(e).__name__}: {e}")
    for it in items:
        it["expected_month"] = expected_month(it["event_ticker"], now)
        it["report"] = match_report(it, by_symbol)
        cal["matched"] += bool(it["report"])
    items.sort(key=lambda i: ((i["report"] or {}).get("date") or i["expected_month"] or "9999", -i["open_interest"]))
    return {
        "generated_at": now.isoformat(),
        "ladders_found": ladders_found,
        "thresholds": {"min_strikes": MIN_STRIKES, "min_open_interest": MIN_OPEN_INTEREST,
                       "max_spread": MAX_SPREAD, "max_items": MAX_ITEMS},
        "calendar": cal,
        "items": items,
        "errors": errors,
    }
