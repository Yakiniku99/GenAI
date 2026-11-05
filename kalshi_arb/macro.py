"""Macro expectations implied by Kalshi prices.

Kalshi lists most economic numbers as a ladder of "above X" contracts, and
each ladder is a market-implied probability distribution. From each one this
module derives the expected value, a 10-90% range, the most likely outcome,
and the same figures 24h ago (from Kalshi's previous bid/ask) so the
dashboard can show how expectations moved.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


MAX_SPREAD = 0.10  # wider quotes say little about the probability


def market_mid(m: dict, previous: bool = False, max_spread: float | None = None) -> float | None:
    """Mid of the YES book, or of the book 24h ago with previous=True. With
    max_spread, books wider than that return None."""
    pre = "previous_" if previous else ""
    bid, ask = _f(m.get(f"{pre}yes_bid_dollars")), _f(m.get(f"{pre}yes_ask_dollars"))
    if bid is None or ask is None or ask <= 0 or ask < bid:
        return None
    if max_spread is not None and ask - bid > max_spread + 1e-9:
        return None
    return (bid + ask) / 2


def _non_increasing(ps: list[float]) -> list[float]:
    """Pool-adjacent-violators: closest non-increasing sequence. P(X > s) must
    fall as the strike rises; quotes on illiquid strikes sometimes don't."""
    blocks = [[p, 1] for p in ps]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] < blocks[i + 1][0] - 1e-12:
            a, b = blocks[i], blocks.pop(i + 1)
            a[0] = (a[0] * a[1] + b[0] * b[1]) / (a[1] + b[1])
            a[1] += b[1]
            i = max(i - 1, 0)
        else:
            i += 1
    out = []
    for v, n in blocks:
        out += [min(max(v, 0.0), 1.0)] * n
    return out


def ladder_distribution(markets: list[dict], previous: bool = False, discrete: bool = False,
                        scale: float = 1.0, only: set[str] | None = None) -> dict | None:
    """Distribution from "above X" markets.

    discrete=True: the outcome is reported on the strike grid (Fed rate in
    25bp steps, CPI to 0.1), so the bucket (s_i, s_i+1] is the single value
    s_i+1. Otherwise each bucket is represented by its midpoint."""
    pts = []
    for m in markets:
        if m.get("status") != "active" or m.get("strike_type") not in ("greater", "greater_or_equal"):
            continue
        if only is not None and m["ticker"] not in only:
            continue
        s, p = _f(m.get("floor_strike")), market_mid(m, previous, MAX_SPREAD)
        if s is not None and p is not None:
            pts.append((s * scale, p))
    pts.sort()
    if len(pts) < 3:
        return None
    strikes = [s for s, _ in pts]
    probs = _non_increasing([p for _, p in pts])
    gaps = sorted(b - a for a, b in zip(strikes, strikes[1:]))
    step = gaps[len(gaps) // 2]

    buckets = [{"lo": None, "hi": strikes[0], "p": 1 - probs[0],
                "value": strikes[0] if discrete else strikes[0] - step / 2}]
    for i in range(len(strikes) - 1):
        lo, hi = strikes[i], strikes[i + 1]
        buckets.append({"lo": lo, "hi": hi, "p": probs[i] - probs[i + 1],
                        "value": hi if discrete else (lo + hi) / 2})
    buckets.append({"lo": strikes[-1], "hi": None, "p": probs[-1],
                    "value": strikes[-1] + step if discrete else strikes[-1] + step / 2})
    total = sum(b["p"] for b in buckets) or 1.0
    for b in buckets:
        b["p"] = round(b["p"] / total, 4)

    mean = sum(b["p"] * b["value"] for b in buckets)

    def quantile(q: float) -> float:
        # CDF is known exactly at the strikes: F(s_i) = 1 - P(X > s_i)
        cdf = [1 - p for p in probs]
        if q <= cdf[0] + 1e-9:
            return strikes[0] if discrete else strikes[0] - step / 2
        for i in range(1, len(strikes)):
            if q <= cdf[i] + 1e-9:
                if discrete:
                    return strikes[i]
                lo_c, hi_c = cdf[i - 1], cdf[i]
                frac = (q - lo_c) / (hi_c - lo_c) if hi_c > lo_c else 1.0
                return strikes[i - 1] + frac * (strikes[i] - strikes[i - 1])
        return strikes[-1] + (step if discrete else step / 2)

    mode = max(buckets, key=lambda b: b["p"])
    return {
        "mean": round(mean, 4),
        "p10": round(quantile(0.10), 4),
        "median": round(quantile(0.50), 4),
        "p90": round(quantile(0.90), 4),
        "mode": {"value": mode["value"], "lo": mode["lo"], "hi": mode["hi"], "p": mode["p"]},
        "buckets": buckets,
        "step": step,
        "liquid_strikes": len(strikes),
        "range": [strikes[0], strikes[-1]],
        # Mass outside the liquid strikes is lumped into the tails, which biases the mean.
        "low_confidence": len(strikes) < 5 or buckets[0]["p"] > 0.15 or buckets[-1]["p"] > 0.15,
    }


# (series, label, unit, discrete grid, scale, decimals)
FED_SERIES = "KXFED"
INDICATORS = (
    ("KXCPI", "CPI, month over month", "%", True, 1.0, 1),
    ("KXCPIYOY", "CPI, year over year", "%", True, 1.0, 1),
    ("KXCPICOREYOY", "Core CPI, year over year", "%", True, 1.0, 1),
    ("KXPAYROLLS", "Nonfarm payrolls", "k jobs", False, 0.001, 0),
    ("KXU3", "Unemployment rate", "%", True, 1.0, 1),
    ("KXGDP", "Real GDP growth (annualized)", "%", False, 1.0, 1),
)


def _event_time(e: dict) -> str:
    closes = [m.get("close_time") or "" for m in e.get("markets") or []]
    return e.get("strike_date") or (min(closes) if closes else "")


def _upcoming(events: list[dict], series: str, now: datetime) -> list[dict]:
    evs = [e for e in events if e.get("series_ticker") == series
           and any(m.get("status") == "active" for m in e.get("markets") or [])]
    evs = [e for e in evs if _event_time(e) >= now.isoformat()[:19]]
    return sorted(evs, key=_event_time)


def _liquid(m: dict, previous: bool) -> bool:
    return market_mid(m, previous, MAX_SPREAD) is not None


def _dist_pair(markets, discrete, scale):
    """Current distribution plus its 24h change, with the change measured on
    the strikes that were liquid both now and 24h ago (otherwise a strike
    dropping in or out of the liquid set looks like a move)."""
    now = ladder_distribution(markets, False, discrete, scale)
    if not now:
        return None
    common = {m["ticker"] for m in markets if _liquid(m, False) and _liquid(m, True)}
    a = ladder_distribution(markets, False, discrete, scale, common)
    b = ladder_distribution(markets, True, discrete, scale, common)
    change = None
    if a and b:
        change = {k: round(a[k] - b[k], 4) for k in ("mean", "p10", "median", "p90")}
    prev = {k: round(now[k] - change[k], 4) for k in change} if change else None
    return {**now, "change_24h": change, "prev": prev}


_DECISION = re.compile(r"(hike|cut|raise|lower)s?\s*(>)?\s*(\d+)\s*bps?", re.I)


def decision_change(label: str) -> float | None:
    """Rate change (percentage points) for a Fed-decision outcome label."""
    text = label.lower()
    if "maintain" in text or "hold" in text or "no change" in text:
        return 0.0
    m = _DECISION.search(text)
    if not m:
        return None
    bp = int(m.group(3)) + (25 if m.group(2) else 0)  # ">25bps" -> assume 50bp
    return (bp if m.group(1) in ("hike", "raise") else -bp) / 100


def fed_path(events: list[dict], now: datetime, max_meetings: int = 8) -> dict:
    """Expected Fed upper bound after each meeting.

    The path comes from the (liquid) hike/hold/cut markets: today's rate plus
    the running sum of expected changes, which is valid whatever the
    correlation between meetings. Each meeting's own rate ladder supplies the
    10-90% range where its strikes are liquid enough."""
    decisions = {e.get("strike_date"): e for e in events if e.get("series_ticker") == "KXFEDDECISION"}
    ladders = {e.get("strike_date"): e for e in events if e.get("series_ticker") == FED_SERIES}
    dates = sorted(d for d in set(decisions) | set(ladders) if d and d >= now.isoformat()[:19])[:max_meetings]
    meetings, current = [], None
    for date in dates:
        row: dict = {"date": date}
        dec = decisions.get(date)
        if dec and any(m.get("status") == "active" for m in dec.get("markets") or []):
            probs = outcome_probs(dec)
            known = [(decision_change(o["label"]), o) for o in probs]
            if probs and all(c is not None for c, _ in known):
                row["decision"] = probs
                row["exp_change"] = round(sum(c * o["p"] for c, o in known), 4)
                if all(o["prev"] is not None for _, o in known):
                    row["exp_change_prev"] = round(sum(c * o["prev"] for c, o in known), 4)
                row["decision_ticker"] = dec["event_ticker"]
        lad = ladders.get(date)
        if lad:
            d = _dist_pair(lad["markets"], True, 1.0)
            if d:
                row["ladder"] = {k: d[k] for k in ("mean", "p10", "median", "p90", "mode", "low_confidence",
                                                   "liquid_strikes", "change_24h")}
                row["ladder_ticker"] = lad["event_ticker"]
        meetings.append(row)
    # Anchor: the first meeting with a confident ladder and a decision market.
    for r in meetings:
        if "ladder" in r and not r["ladder"]["low_confidence"] and "exp_change" in r:
            idx = meetings.index(r)
            before = sum(m.get("exp_change", 0) for m in meetings[:idx])
            current = round((r["ladder"]["mean"] - r["exp_change"] - before) * 4) / 4
            break
    if current is not None:
        level, level_prev = current, current
        for r in meetings:
            if "exp_change" not in r:
                break
            level += r["exp_change"]
            level_prev += r.get("exp_change_prev", r["exp_change"])
            r["expected"] = round(level, 4)
            r["expected_prev"] = round(level_prev, 4)
    return {"current_upper_bound": current, "meetings": meetings}


def outcome_probs(event: dict) -> list[dict]:
    """Normalized probabilities for a mutually exclusive event's outcomes."""
    rows = []
    for m in event.get("markets") or []:
        if m.get("status") != "active":
            continue
        p, q = market_mid(m), market_mid(m, True)
        if p is None:
            continue
        rows.append({"label": m.get("yes_sub_title") or m["ticker"], "ticker": m["ticker"], "p": p, "prev": q})
    tot = sum(r["p"] for r in rows) or 1.0
    ptot = sum(r["prev"] for r in rows if r["prev"] is not None) or 1.0
    for r in rows:
        r["p"] = round(r["p"] / tot, 4)
        r["prev"] = round(r["prev"] / ptot, 4) if r["prev"] is not None else None
    return sorted(rows, key=lambda r: -r["p"])


def indicators(events: list[dict], now: datetime) -> list[dict]:
    out = []
    for series, label, unit, discrete, scale, decimals in INDICATORS:
        for e in _upcoming(events, series, now)[:2]:
            d = _dist_pair(e["markets"], discrete, scale)
            if not d:
                continue
            out.append({"series": series, "label": label, "unit": unit, "decimals": decimals,
                        "event_ticker": e["event_ticker"], "title": e.get("title", ""),
                        "resolves": _event_time(e), **d,
                        "volume_24h": round(sum(_f(m.get("volume_24h_fp")) or 0 for m in e["markets"]), 2)})
    return out


def gauges(events: list[dict], now: datetime) -> list[dict]:
    out = []
    for e in _upcoming(events, "KXRECSSNBER", now):
        ms = [m for m in e["markets"] if m.get("status") == "active"]
        if len(ms) == 1 and market_mid(ms[0]) is not None:
            out.append({"key": e["event_ticker"], "label": e.get("title", "Recession"), "kind": "probability",
                        "value": round(market_mid(ms[0]), 4), "prev": market_mid(ms[0], True)})
    for e in _upcoming(events, "KXRATECUTCOUNT", now)[:1]:
        rows = []
        for m in e["markets"]:
            v, p, q = _f(m.get("floor_strike")), market_mid(m), market_mid(m, True)
            if m.get("status") == "active" and v is not None and p is not None:
                rows.append((v, p, q))
        if rows:
            tot = sum(p for _, p, _ in rows) or 1
            ptot = sum(q for _, _, q in rows if q is not None) or 1
            out.append({"key": e["event_ticker"], "label": e.get("title", "Rate cuts"), "kind": "count",
                        "value": round(sum(v * p for v, p, _ in rows) / tot, 3),
                        "prev": round(sum(v * q for v, _, q in rows if q is not None) / ptot, 3)})
    return out


def build_macro(events: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"fed_path": fed_path(events, now), "indicators": indicators(events, now), "gauges": gauges(events, now)}
