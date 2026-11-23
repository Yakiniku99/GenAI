"""History of Kalshi KPI forecasts and stock prices across scans, and how
strongly the two move together.

Every scan appends one record (each ladder's median and the stock price, plus
the S&P 500 ETF as the market). From that history this works out, per day,
how much a KPI median moved and how much the stock moved beyond the market,
and measures the link with Spearman rank correlation. It is robust to
outliers and to the many days a KPI doesn't move at all.

Three readings, from fastest to slowest to fill in:
  * cross-section: across companies, over the latest 24h (available at once);
  * pooled: every company-day in the history (needs about three weeks);
  * per company: that company's own days (needs about two weeks).

A correlation says the two moved together, not that one caused the other.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

from .prices import ET

VERSION = 1
MIN_GAP = timedelta(minutes=20)      # scans closer than this are not recorded again
KEEP_FULL = timedelta(days=7)        # every scan is kept this long, then one per day
KEEP_MAX = timedelta(days=180)
MAX_GAP_DAYS = 4                     # a weekend plus a holiday still counts as consecutive
MIN_CROSS, MIN_POOLED, MIN_ITEM = 8, 15, 10   # observations needed before showing a number


def empty() -> dict:
    return {"version": VERSION, "scans": []}


def load(path: str | None) -> dict:
    """The saved history, or an empty one if the file is missing or unreadable."""
    try:
        with open(path) as f:
            h = json.load(f)
        if h.get("version") == VERSION and isinstance(h.get("scans"), list):
            return h
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return empty()


def save(hist: dict, path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(hist, f, separators=(",", ":"))
    os.replace(tmp, path)


def _day(t: int) -> date:
    return datetime.fromtimestamp(t, ET).date()


def record(hist: dict, earn: dict, now: datetime) -> bool:
    """Append this scan. Returns False if the last one was too recent."""
    scans, t = hist["scans"], int(now.timestamp())
    if scans and t - scans[-1]["t"] < MIN_GAP.total_seconds():
        return False
    market = (earn.get("prices") or {}).get("market")
    rows = {}
    for it in earn.get("items") or []:
        d, px = it["dist"], it.get("price")
        rows[it["event_ticker"]] = [round(d["median"], 6), round(d["p90"] - d["p10"], 6),
                                    px["price"] if px else None, bool(px and px["stale"])]
    scans.append({"t": t, "mkt": [market["price"], bool(market["stale"])] if market else None, "items": rows})
    cut_full, cut_max = int((now - KEEP_FULL).timestamp()), int((now - KEEP_MAX).timestamp())
    kept, last_of_day = [], {}
    for s in scans:
        if s["t"] < cut_max:
            continue
        if s["t"] >= cut_full:
            kept.append(s)
        else:
            last_of_day[_day(s["t"])] = s   # scans are in time order, so the last one wins
    hist["scans"] = sorted(last_of_day.values(), key=lambda s: s["t"]) + kept
    return True


# --------------------------------------------------------------- statistics --

def _ranks(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=v.__getitem__)
    ranks, i = [0.0] * len(v), 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, or None if either side never varies."""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    vx, vy = sum((a - mx) ** 2 for a in rx), sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / math.sqrt(vx * vy)


def strength(r: float) -> str:
    a = abs(r)
    word = "negligible" if a < 0.2 else "weak" if a < 0.4 else "moderate" if a < 0.6 else "strong"
    return word if word == "negligible" else f"{word} {'positive' if r > 0 else 'negative'}"


def describe(xs: list[float], ys: list[float], needed: int) -> dict:
    """r, n, a strength word and whether it is distinguishable from chance (about 95%)."""
    n = len(xs)
    out = {"n": n, "needed": needed, "r": None, "strength": None, "significant": False}
    if n < needed:
        return out
    r = spearman(xs, ys)
    if r is None:
        out["flat"] = True   # one side never moved: no correlation to measure
        return out
    z = math.atanh(max(-0.9999, min(0.9999, r))) * math.sqrt((n - 3) / 1.06)
    out.update(r=round(r, 3), strength=strength(r), significant=abs(z) > 1.96)
    return out


# ------------------------------------------------------------------ analysis --

def daily_pairs(hist: dict) -> dict[str, list[tuple[float, float, float]]]:
    """Per ladder: (KPI median change, KPI spread, stock move beyond the market in %)
    between consecutive trading days, using each day's last fresh scan."""
    days: dict[str, dict[date, tuple]] = {}
    for s in hist["scans"]:
        m = s.get("mkt")
        if not m or m[0] is None or m[1]:
            continue   # the market itself wasn't trading
        for ticker, (median, scale, price, stale) in s["items"].items():
            if price is not None and not stale:
                days.setdefault(ticker, {})[_day(s["t"])] = (median, scale, price, m[0])
    out: dict[str, list] = {}
    for ticker, by_day in days.items():
        ds = sorted(by_day)
        for a, b in zip(ds, ds[1:]):
            if (b - a).days > MAX_GAP_DAYS:
                continue
            (m0, _, p0, s0), (m1, sc1, p1, s1) = by_day[a], by_day[b]
            if p0 and s0:
                out.setdefault(ticker, []).append((m1 - m0, sc1, (p1 / p0 - s1 / s0) * 100))
    return out


def analyze(earn: dict, hist: dict) -> None:
    """Add `correlation` to the earnings block and `corr` to each item, in place."""
    items = earn.get("items") or []
    market = (earn.get("prices") or {}).get("market")
    xs, ys = [], []
    for it in items:
        c, px, d = it["dist"].get("change_24h"), it.get("price"), it["dist"]
        scale = d["p90"] - d["p10"]
        y = px and (px.get("excess_pct") if market else px.get("change_pct"))
        if c and px and not px["stale"] and y is not None and scale > 0:
            xs.append(c["median"] / scale)
            ys.append(y)
    pairs = daily_pairs(hist)
    pooled = [p for rows in pairs.values() for p in rows if p[1] > 0]
    for it in items:
        rows = pairs.get(it["event_ticker"], [])
        it["corr"] = describe([r[0] for r in rows], [r[2] for r in rows], MIN_ITEM)
    scans = hist["scans"]
    earn["correlation"] = {
        "cross_section": describe(xs, ys, MIN_CROSS),
        "pooled": describe([p[0] / p[1] for p in pooled], [p[2] for p in pooled], MIN_POOLED),
        "days": len({_day(s["t"]) for s in scans}),
        "scans": len(scans),
        "since": datetime.fromtimestamp(scans[0]["t"], timezone.utc).isoformat() if scans else None,
        "adjusted_for": market["symbol"] if market else None,
    }


def track(earn: dict, path: str | None, now: datetime | None = None) -> None:
    """Record this scan in the history file (if `path`), then analyze. Never raises on I/O."""
    now = now or datetime.now(timezone.utc)
    hist = load(path) if path else empty()
    try:
        if record(hist, earn, now) and path:
            save(hist, path)
    except OSError as e:
        earn["errors"] = [*earn.get("errors", []), f"history: {type(e).__name__}: {e}"]
    analyze(earn, hist)
