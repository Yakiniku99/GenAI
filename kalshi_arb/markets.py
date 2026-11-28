"""Markets tab: what Kalshi's price markets imply for indices, commodities and FX.

Kalshi lists "above X" ladders and "between X and Y" range sets on the S&P 500,
Nasdaq-100, oil, gold, silver, natural gas, copper, EUR/USD and USD/JPY, from
the next hour out to the end of next year. Each one is a probability
distribution for where the price will be at the close. This module turns them
into numbers to set beside the market itself:

- the median and 10-90% range, and the probability of finishing above today's price
- the one-standard-deviation move and the volatility it implies (annualized over trading days),
  next to the market's own gauge (VIX for the S&P 500, VXN, OVX, GVZ) where one exists
- skew: whether the downside tail is longer than the upside tail
- the same figures 24 hours ago (from Kalshi's previous quotes)

Spot prices and the volatility gauges come from Yahoo Finance's public chart
endpoint, which needs no login. It isn't a documented API; every failure
degrades to "no price" and never breaks a scan.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .macro import MAX_SPREAD, _dist_pair, _f, market_mid
from .prices import market_gap_reason

YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")   # the second one when the first rate-limits
HEADERS = {"User-Agent": "Mozilla/5.0"}   # Yahoo rate-limits longer browser strings
Z80 = 1.2816              # a 10-90% range spans +-1.2816 standard deviations of a normal distribution
STALE_AFTER = timedelta(hours=20)   # equity indices sit idle ~17.5h overnight, so only a longer gap counts on its own
MIN_VOL_HOURS = 20        # shorter horizons say little about annual volatility (strike spacing dominates)
MAX_VOL_DAYS = 120        # a vol gauge looks ~30 days ahead; compare against the horizon nearest to that
MAX_POINTS = 10           # horizons kept per instrument
MAX_HOURLY = 3            # of which hourly contracts
MIN_COVERAGE = 0.8        # liquid range buckets must carry this much of the probability
HORIZON_DAYS = 400
SPOT_MISMATCH = 0.25      # Kalshi median this far from spot: wrong reference, don't compare
RANGE_TYPES = ("between", "less", "greater")


def inst(key, label, group, symbol, series, decimals=2, gauge=None, gauge_label="", unit=""):
    return {"key": key, "label": label, "group": group, "symbol": symbol, "series": series,
            "decimals": decimals, "gauge": gauge, "gauge_label": gauge_label, "unit": unit}


# series: (Kalshi series ticker, how often it lists)
INSTRUMENTS = (
    inst("spx", "S&P 500", "Equity indices", "^GSPC",
         [("KXINXU", "hourly"), ("KXINX", "daily"), ("KXINXY", "year-end")], 0, "^VIX", "VIX"),
    inst("ndx", "Nasdaq-100", "Equity indices", "^NDX",
         [("KXNASDAQ100U", "hourly"), ("KXNASDAQ100", "daily"), ("KXNASDAQ100Y", "year-end")], 0, "^VXN", "VXN"),
    inst("wti", "WTI crude oil", "Commodities", "CL=F",
         [("KXWTIH", "hourly"), ("KXWTI", "daily"), ("KXWTIW", "weekly")], 2, "^OVX", "OVX", "$"),
    inst("brent", "Brent crude oil", "Commodities", "BZ=F",
         [("KXBRENTD", "daily"), ("KXBRENTW", "weekly"), ("KXBRENTMON", "monthly")], 2, unit="$"),
    inst("gold", "Gold", "Commodities", "GC=F",
         [("KXGOLDH", "hourly"), ("KXGOLDD", "daily"), ("KXGOLDW", "weekly"), ("KXGOLDMON", "monthly")], 0,
         "^GVZ", "GVZ", "$"),
    inst("silver", "Silver", "Commodities", "SI=F",
         [("KXSILVERH", "hourly"), ("KXSILVERD", "daily"), ("KXSILVERW", "weekly"), ("KXSILVERMON", "monthly")],
         2, unit="$"),
    inst("natgas", "Natural gas", "Commodities", "NG=F",
         [("KXNATGASD", "daily"), ("KXNATGASW", "weekly"), ("KXNATGASMON", "monthly")], 3, unit="$"),
    inst("copper", "Copper", "Commodities", "HG=F",
         [("KXCOPPERD", "daily"), ("KXCOPPERW", "weekly"), ("KXCOPPERMON", "monthly")], 3, unit="$"),
    inst("eurusd", "EUR/USD", "Currencies", "EURUSD=X",
         [("KXEURUSDH", "hourly"), ("KXEURUSD", "daily")], 4),
    inst("usdjpy", "USD/JPY", "Currencies", "JPY=X",
         [("KXUSDJPYH", "hourly"), ("KXUSDJPY", "daily")], 2),
)


# --- spot prices -------------------------------------------------------------

def _yahoo(symbol: str, timeout: float = 20.0, attempts: int = 4) -> dict:
    path = "/v8/finance/chart/" + urllib.parse.quote(symbol) + "?interval=1h&range=5d"
    for n in range(attempts):
        url = "https://" + YAHOO_HOSTS[n % len(YAHOO_HOSTS)] + path
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code != 429 or n == attempts - 1:
                raise
            time.sleep(1.5 * (n + 1))
    raise RuntimeError("unreachable")


def summarize_quote(symbol: str, data: dict, now: datetime) -> dict | None:
    """Last price and its move over the 24 hours up to the last trade."""
    try:
        res = data["chart"]["result"][0]
        meta = res["meta"]
        price, at = float(meta["regularMarketPrice"]), int(meta["regularMarketTime"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    when = datetime.fromtimestamp(at, timezone.utc)
    ref = None
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    for ts, c in zip(res.get("timestamp") or [], closes):
        if c is not None and ts <= at - 24 * 3600:
            ref = float(c)          # the last hourly close at least a day before the last trade
    age = now - when
    reason = market_gap_reason(when, now)   # a weekend or holiday since the last trade is stale however recent it was
    stale = reason != "no recent trades" or age > STALE_AFTER
    return {
        "symbol": symbol, "price": price, "price_at": when.isoformat(),
        "ref_price": ref, "change_pct": round((price / ref - 1) * 100, 3) if ref else None,
        "stale": stale, "age_hours": round(age.total_seconds() / 3600, 1),
        "stale_reason": reason if stale else None,
    }


def fetch_quotes(symbols: list[str], now: datetime, get=_yahoo, workers: int = 2) -> tuple[dict, list[str]]:
    out: dict[str, dict | None] = {}
    errors: list[str] = []

    def one(sym):
        try:
            return sym, summarize_quote(sym, get(sym), now), None
        except Exception as e:   # a failed price must never fail the scan
            return sym, None, f"{sym}: {type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sym, q, err in pool.map(one, symbols):
            out[sym] = q
            if err:
                errors.append(err)
    return out, errors


# --- distributions -----------------------------------------------------------

def _range_ladder(event_ticker: str, markets: list[dict]) -> list[dict]:
    """Turn a set of mutually exclusive price ranges into "at or above X"
    markets: P(price >= lower edge of bucket i) is the probability in bucket i
    and every bucket above it. Buckets with a wide spread are left out, and
    the rest are rescaled to sum to one."""
    side = {}
    for previous in (False, True):
        rows = []
        for m in markets:
            if m.get("status") != "active" or m.get("strike_type") not in RANGE_TYPES:
                continue
            lo = _f(m.get("floor_strike")) if m["strike_type"] != "less" else float("-inf")
            p = market_mid(m, previous, MAX_SPREAD)
            if lo is not None and p is not None:
                rows.append((lo, p))
        total = sum(p for _, p in rows)
        if total < MIN_COVERAGE or len(rows) < 4:
            continue
        rows.sort()
        above = total
        for i, (lo, p) in enumerate(rows):
            if i:   # the lowest bucket's lower edge is minus infinity: no strike there
                side.setdefault(lo, {})["previous" if previous else "now"] = above / total
            above -= p
    out = []
    for lo, v in sorted(side.items()):
        now, prev = v.get("now"), v.get("previous")
        out.append({"ticker": f"{event_ticker}-{lo}", "status": "active", "strike_type": "greater", "floor_strike": lo,
                    "yes_bid_dollars": now, "yes_ask_dollars": now,
                    "previous_yes_bid_dollars": prev, "previous_yes_ask_dollars": prev})
    return out


def event_distribution(event: dict) -> dict | None:
    markets = event.get("markets") or []
    if not any(m.get("status") == "active" for m in markets):
        return None
    if any(m.get("strike_type") == "between" for m in markets):
        markets = _range_ladder(event["event_ticker"], markets)
    return _dist_pair(markets, False, 1.0) if markets else None


def prob_above(buckets: list[dict], x: float) -> float | None:
    """P(price > x) from the bucketed distribution, linear inside a bucket and
    held at the outermost strikes beyond them."""
    if not buckets:
        return None
    below = 0.0
    for b in buckets:
        lo, hi = b["lo"], b["hi"]
        if hi is not None and x >= hi:
            below += b["p"]
            continue
        if lo is None or x <= lo:   # the open low tail: nothing is known below the first strike
            return round(1 - below, 4) if lo is not None and x <= lo else round(1 - b["p"], 4)
        if hi is None:              # the open top tail
            return round(b["p"], 4)
        return round(1 - (below + b["p"] * (x - lo) / (hi - lo)), 4)
    return 0.0


def trading_days(start: datetime, end: datetime) -> float:
    """Days from start to end that fall on a weekday (UTC), the time over which
    prices can move. Annualizing by this and 252 matches how VIX-style gauges
    are quoted, and keeps a horizon that spans a weekend from looking calm."""
    total, cur = 0.0, start
    while cur < end:
        nxt = min(datetime(cur.year, cur.month, cur.day, tzinfo=timezone.utc) + timedelta(days=1), end)
        if cur.weekday() < 5:
            total += (nxt - cur).total_seconds() / 86400
        cur = nxt
    return total


def _point(event: dict, kind: str, dist: dict, spot: float | None, now: datetime) -> dict:
    close = event.get("strike_date") or min((m.get("close_time") or "" for m in event["markets"]
                                              if m.get("status") == "active"), default="")
    when = datetime.fromisoformat(close.replace("Z", "+00:00"))
    hours = max((when - now).total_seconds() / 3600, 0.0)
    med, p10, p90 = dist["median"], dist["p10"], dist["p90"]
    sigma = (p90 - p10) / (2 * Z80)
    pt = {
        "event_ticker": event["event_ticker"], "title": event.get("title", ""), "kind": kind,
        "close": close, "hours": round(hours, 1),
        "median": med, "mean": dist["mean"], "p10": p10, "p90": p90, "sigma": round(sigma, 6),
        "skew": round(((med - p10) - (p90 - med)) / (p90 - p10), 3) if p90 > p10 and not dist["low_confidence"] else None,
        "change_24h": dist.get("change_24h"), "liquid_strikes": dist["liquid_strikes"],
        "low_confidence": dist["low_confidence"],
        "volume_24h": round(sum(_f(m.get("volume_24h_fp")) or 0 for m in event["markets"]), 0),
        "open_interest": round(sum(_f(m.get("open_interest_fp")) or 0 for m in event["markets"]), 0),
        "buckets": [[b["lo"], b["hi"], b["p"], b.get("p_prev")] for b in dist["buckets"]],
        "spot_gap_pct": None, "prob_above_spot": None, "sigma_pct": None, "ann_vol": None,
    }
    if spot and abs(med / spot - 1) <= SPOT_MISMATCH:
        pt["spot_gap_pct"] = round((med / spot - 1) * 100, 3)
        pt["prob_above_spot"] = prob_above(dist["buckets"], spot)
        pt["sigma_pct"] = round(sigma / spot * 100, 3)
        if hours >= MIN_VOL_HOURS and not dist["low_confidence"]:
            days = trading_days(now, when)
            if days >= MIN_VOL_HOURS / 24:
                pt["ann_vol"] = round(sigma / spot * math.sqrt(252 / days) * 100, 2)
    return pt


def implied_vol(points: list[dict], gauge: dict | None) -> dict | None:
    """The annualized volatility from the horizon closest to a month out,
    beside the market's own gauge."""
    pool = [p for p in points if p["ann_vol"] is not None and p["hours"] <= MAX_VOL_DAYS * 24]
    if not pool:
        return None
    p = min(pool, key=lambda q: abs(q["hours"] - 30 * 24))
    out = {"ann_vol": p["ann_vol"], "event_ticker": p["event_ticker"], "close": p["close"], "hours": p["hours"]}
    if gauge:
        out["gauge"] = gauge
        out["gap"] = round(p["ann_vol"] - gauge["price"], 2)
    return out


# --- the scan ----------------------------------------------------------------

def build_instrument(cfg: dict, events_by_series: dict[str, list[dict]], quotes: dict, now: datetime) -> dict:
    spot = quotes.get(cfg["symbol"])
    gauge = quotes.get(cfg["gauge"]) if cfg["gauge"] else None
    limit = (now + timedelta(days=HORIZON_DAYS)).isoformat()[:19]
    points: list[dict] = []
    for series, kind in cfg["series"]:
        for e in events_by_series.get(series, []):
            when = e.get("strike_date") or ""
            if not when or when[:19] < now.isoformat()[:19] or when[:19] > limit:
                continue
            dist = event_distribution(e)
            if dist and dist["p90"] > dist["p10"]:   # equal quantiles: the strikes are too coarse to say anything
                points.append(_point(e, kind, dist, spot["price"] if spot else None, now))
    points.sort(key=lambda p: p["close"])
    hourly = 0
    kept = []
    for p in points:
        if p["kind"] == "hourly":
            hourly += 1
            if hourly > MAX_HOURLY:
                continue
        kept.append(p)
    kept = kept[:MAX_POINTS]
    g = {"symbol": gauge["symbol"], "label": cfg["gauge_label"], "price": gauge["price"],
         "stale": gauge["stale"], "change_pct": gauge["change_pct"]} if gauge else None
    return {"key": cfg["key"], "label": cfg["label"], "group": cfg["group"], "symbol": cfg["symbol"],
            "decimals": cfg["decimals"], "unit": cfg["unit"], "spot": spot, "gauge": g,
            "implied_vol": implied_vol(kept, g), "points": kept}


def build_markets(client, now: datetime | None = None, workers: int = 6, fetch=_yahoo) -> dict:
    """Fetch every instrument's Kalshi ladders and its spot price, and summarize."""
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []
    series = sorted({s for c in INSTRUMENTS for s, _ in c["series"]})
    events_by_series: dict[str, list[dict]] = {}

    def events(s):
        try:
            return s, client.open_events_with_markets(s), None
        except Exception as e:   # a series with no listing is normal; report real failures only
            return s, [], f"{s}: {e}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for s, evs, err in pool.map(events, series):
            events_by_series[s] = evs
            if err:
                errors.append(err)
    symbols = sorted({c["symbol"] for c in INSTRUMENTS} | {c["gauge"] for c in INSTRUMENTS if c["gauge"]})
    quotes, qerrs = fetch_quotes(symbols, now, fetch)
    errors += qerrs
    items = [build_instrument(c, events_by_series, quotes, now) for c in INSTRUMENTS]
    items = [i for i in items if i["points"]]
    return {
        "as_of": now.isoformat(),
        "price_source": "Yahoo Finance",
        "instruments": items,
        "listed": len(INSTRUMENTS),
        "stale": sum(1 for i in items if i["spot"] and i["spot"]["stale"]),
        "errors": [f"markets: {e}" for e in errors],
    }
