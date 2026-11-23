"""Stock prices for the Earnings tab: the last 24 hours, extended hours included.

Source is Robinhood's public market-data endpoints, which need no login. They
are not a documented API and could change or rate-limit; every failure here
degrades to "no price" and never breaks a scan.

For each company with a Kalshi KPI ladder this finds the listed stock, takes
the 24/7 five-minute bars for the last day (pre-market, regular, after-hours
and overnight sessions), and reports the move from 24 hours ago to the latest
trade. A price is flagged stale when the last real trade is old, with the
reason (weekend, market holiday) taken from the NYSE calendar.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # no tz database: fall back to standard time
    ET = timezone(timedelta(hours=-5))

from .earnings import symbol_candidates

RH = "https://api.robinhood.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept": "application/json"}
STALE_AFTER = timedelta(hours=12)   # an overnight gap is ~8h; weekends and holidays are far longer
WINDOW = timedelta(hours=24)
MARKET = "SPY"   # the S&P 500 ETF: the market move to measure each stock against
SYMBOL_OVERRIDES = {"KXTESLASEMI": "TSLA"}   # series whose ticker isn't KX + stock symbol
SESSIONS = {"pre": "pre-market", "reg": "regular hours", "post": "after-hours"}
_SUFFIX = {"inc", "corp", "corporation", "company", "co", "group", "holdings", "ltd", "limited", "the", "com", "plc"}


def _get(path: str, params: dict | None = None, timeout: float = 20.0) -> dict:
    url = RH + path + ("?" + urllib.parse.urlencode(params, safe=",") if params else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as resp:
        return json.loads(resp.read())


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _time(s) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except (AttributeError, ValueError):
        return None


# ------------------------------------------------------------------ calendar --

def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(y, month, day + 1)


def _nth(y: int, m: int, weekday: int, n: int) -> date:
    first = date(y, m, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last(y: int, m: int, weekday: int) -> date:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return nxt - timedelta(days=(nxt.weekday() - weekday) % 7 or 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def nyse_holidays(y: int) -> dict[date, str]:
    """Full-day NYSE closures in year y (early closes are not holidays)."""
    out = {_nth(y, 1, 0, 3): "Martin Luther King Jr. Day", _nth(y, 2, 0, 3): "Presidents' Day",
           _easter(y) - timedelta(days=2): "Good Friday", _last(y, 5, 0): "Memorial Day",
           _observed(date(y, 6, 19)): "Juneteenth", _observed(date(y, 7, 4)): "Independence Day",
           _nth(y, 9, 0, 1): "Labor Day", _nth(y, 11, 3, 4): "Thanksgiving Day",
           _observed(date(y, 12, 25)): "Christmas Day"}
    if date(y, 1, 1).weekday() != 5:   # a Saturday New Year isn't observed on the Friday
        out[_observed(date(y, 1, 1))] = "New Year's Day"
    return out


def market_gap_reason(last_trade: datetime, now: datetime) -> str:
    """Why nothing has traded since `last_trade`: weekend, market holiday, or neither."""
    a, b = last_trade.astimezone(ET).date(), now.astimezone(ET).date()
    holidays = {**nyse_holidays(a.year), **nyse_holidays(b.year)}
    days = [a + timedelta(days=n) for n in range((b - a).days + 1)]
    named = [holidays[d] for d in days if d in holidays and d.weekday() < 5]
    weekend = any(d.weekday() >= 5 for d in days[1:] if d <= b)
    if named and weekend:
        return f"weekend and market holiday ({named[0]})"
    if named:
        return f"market holiday ({named[0]})"
    return "weekend" if weekend else "no recent trades"


# --------------------------------------------------------------------- price --

def summarize_price(symbol: str, name: str, quote: dict, bars: list[dict], now: datetime) -> dict | None:
    """24h move and staleness for one stock from its quote and 24/7 five-minute bars."""
    real = [b for b in bars if not b.get("interpolated")]
    quotes = [(_time(quote.get("venue_last_trade_time")), _num(quote.get("last_trade_price")), False),
              (_time(quote.get("venue_last_non_reg_trade_time")), _num(quote.get("last_extended_hours_trade_price")), True)]
    quotes = [(t, p, ext) for t, p, ext in quotes if t and p]
    bar_time = _time(real[-1]["begins_at"]) if real else None
    if quotes:
        last_at, price, _ = max(quotes, key=lambda q: q[0])
        if bar_time and bar_time > last_at:   # a bar newer than the quote: take the bar
            last_at, price = bar_time, _num(real[-1]["close_price"])
    elif bar_time:
        last_at, price = bar_time, _num(real[-1]["close_price"])
    else:
        return None
    if not price:
        return None
    first = bars[0] if bars else None
    ref_at = _time(first["begins_at"]) if first else None
    ref = _num(first["close_price"]) if first else None
    if ref_at and abs((now - WINDOW) - ref_at) > timedelta(hours=1):   # bars don't reach back a full day
        ref = ref_at = None
    prev_close = _num(quote.get("previous_close"))
    age = now - last_at
    stale = age > STALE_AFTER
    return {
        "symbol": symbol, "name": name, "source": "Robinhood",
        "price": round(price, 4), "price_at": last_at.isoformat(),
        "ref_price": round(ref, 4) if ref else None, "ref_at": ref_at.isoformat() if ref_at else None,
        "change_abs": round(price - ref, 4) if ref else None,
        "change_pct": round((price / ref - 1) * 100, 3) if ref else None,
        "prev_close": prev_close,
        "day_change_pct": round((price / prev_close - 1) * 100, 3) if prev_close else None,
        "session": SESSIONS.get(real[-1].get("session")) if real else None,
        "halted": bool(quote.get("trading_halted")),
        "stale": stale, "age_hours": round(age.total_seconds() / 3600, 1),
        "stale_reason": market_gap_reason(last_at, now) if stale else None,
    }


def _words(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", name.lower().replace("'", "").replace("’", ""))


def same_company(kalshi_name: str, listed_name: str) -> bool:
    """Guards against a wrong stock behind a similar ticker (Kalshi's KXFA is Ford, but FA is First Advantage)."""
    a, b = _words(kalshi_name), _words(listed_name)
    if not a or not b or a[0] != b[0]:
        return False
    if len(a) > 1 and len(b) > 1 and a[1] not in _SUFFIX and b[1] not in _SUFFIX:
        return a[1] == b[1]
    return True


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def add_prices(items: list[dict], now: datetime, get=_get, workers: int = 4) -> tuple[list[str], dict | None]:
    """Attach `price` (or None) to every item. Returns (error strings, the market's own move)."""
    errors: list[str] = []
    cands: dict[str, list[str]] = {}
    for it in items:
        series = it["series_ticker"]
        found = [SYMBOL_OVERRIDES.get(series), (it.get("report") or {}).get("symbol"), *symbol_candidates(series)]
        cands[series] = list(dict.fromkeys(s for s in found if s))
    symbols = sorted({s for c in cands.values() for s in c} | {MARKET})
    quotes: dict[str, dict] = {}
    try:
        for chunk in _chunks(symbols, 50):
            for q in get("/quotes/", {"symbols": ",".join(chunk)}).get("results") or []:
                if q and q.get("symbol"):
                    quotes[q["symbol"]] = q
    except (urllib.error.URLError, OSError, ValueError) as e:
        errors.append(f"prices: quotes: {type(e).__name__}: {e}")
        for it in items:
            it["price"] = None
        return errors, None

    def name_of(sym: str):
        try:
            rows = get("/instruments/", {"symbol": sym}).get("results") or []
            return sym, (rows[0].get("simple_name") or rows[0].get("name") or "") if rows else ""
        except (urllib.error.URLError, OSError, ValueError):
            return sym, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        names = dict(pool.map(name_of, [s for s in symbols if s in quotes and s != MARKET]))

    chosen: dict[str, tuple[str, str]] = {}   # series -> (symbol, listed name)
    for it in items:
        for sym in cands[it["series_ticker"]]:
            listed = names.get(sym)
            if listed and same_company(it["company"], listed):
                chosen[it["series_ticker"]] = (sym, listed)
                break
    wanted = sorted({s for s, _ in chosen.values()} | {MARKET})
    bars: dict[str, list[dict]] = {}
    try:
        for chunk in _chunks(wanted, 20):
            res = get("/marketdata/historicals/", {"symbols": ",".join(chunk), "interval": "5minute",
                                                   "span": "day", "bounds": "24_7"}).get("results") or []
            for r in res:
                if r and r.get("symbol"):
                    bars[r["symbol"]] = r.get("historicals") or []
    except (urllib.error.URLError, OSError, ValueError) as e:
        errors.append(f"prices: history: {type(e).__name__}: {e}")
    market = summarize_price(MARKET, "S&P 500 (SPY)", quotes[MARKET], bars.get(MARKET, []), now) if MARKET in quotes else None
    for it in items:
        hit = chosen.get(it["series_ticker"])
        p = it["price"] = summarize_price(hit[0], hit[1], quotes[hit[0]], bars.get(hit[0], []), now) if hit else None
        if p:   # the move beyond the market's, over the same 24 hours
            p["excess_pct"] = round(p["change_pct"] - market["change_pct"], 3) \
                if market and p["change_pct"] is not None and market["change_pct"] is not None else None
    return errors, market


def refresh(earn: dict, now: datetime | None = None, get=_get) -> None:
    """Add or update stock prices on a finished earnings block, in place."""
    now = now or datetime.now(timezone.utc)
    items = earn.get("items") or []
    errs, market = add_prices(items, now, get)
    priced = [i["price"] for i in items if i.get("price")]
    earn["prices"] = {"source": "Robinhood", "as_of": now.isoformat(), "priced": len(priced),
                      "stale": sum(1 for p in priced if p["stale"]), "unavailable": len(items) - len(priced), "market": market}
    earn["errors"] = [e for e in earn.get("errors", []) if not e.startswith("prices:")] + errs
