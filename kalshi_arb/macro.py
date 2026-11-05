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
                        scale: float = 1.0, only: set[str] | None = None,
                        discrete_step: float = 0.1) -> dict | None:
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
    # Strike values are exact outcomes only when the grid matches the reporting
    # precision (CPI to 0.1, Fed in 25bp); a coarser grid is a range.
    discrete = discrete and step <= discrete_step + 1e-9

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


FED_SERIES = "KXFED"
HORIZON_DAYS = 460  # a year ahead, plus the lag before next year's year-end contracts resolve


def ind(key, label, unit, decimals, series, discrete=False, scale=1.0, note="", discrete_step=0.1):
    return {"key": key, "label": label, "unit": unit, "decimals": decimals, "series": series,
            "discrete": discrete, "scale": scale, "note": note, "discrete_step": discrete_step}


# Each indicator joins its monthly/quarterly release markets with any year-end
# contract for the same quantity, which gives a path up to a year out.
SECTIONS = (
    ("rates", "Interest rates", (
        ind("fedfunds_ye", "Fed funds rate at year-end", "%", 2, ["KXFEDFUNDSYEAR"], discrete=True, discrete_step=0.25,
            note="Year-end contract; cross-check for the path above"),
        ind("effr", "Effective fed funds rate, end of quarter", "%", 2, ["KXEFFR"]),
    )),
    ("housing", "Housing", (
        ind("mortgage_week", "30-year mortgage rate (this week)", "%", 2, ["KX30YMORTW"]),
        ind("mortgage_high", "Highest 30-year mortgage rate this year", "%", 2, ["KXMORTGAGERATE"],
            note="The year's peak, not its level"),
        ind("existing_sales", "Existing home sales (annualized)", "M", 2, ["KXEHSALES"]),
        ind("new_sales", "New home sales (annualized)", "k", 0, ["KXNHSALES"], scale=0.001),
        ind("starts", "Housing starts (annualized)", "M", 2, ["KXHOUSINGSTART"]),
        ind("permits", "Building permits (annualized)", "M", 2, ["KXBUILDPERMS"]),
        ind("existing_price", "Median existing home price", "$k", 0, ["KXEXUSHOME"], scale=0.001),
        ind("new_price", "Median new home price", "$k", 0, ["KXUSHOME"], scale=0.001),
        ind("listings", "Active housing listings", "k", 0, ["KXUSHOMEINVENT"], scale=0.001),
    )),
    ("inflation", "Inflation", (
        ind("cpi_yoy", "CPI, year over year", "%", 1, ["KXCPIYOY", "KXUSCPIYEAR"], discrete=True,
            note="Monthly releases, anchored by the December-next-year contract"),
        ind("core_yoy", "Core CPI, year over year", "%", 1, ["KXCPICOREYOY"], discrete=True),
        ind("cpi_mom", "CPI, month over month", "%", 1, ["KXCPI"], discrete=True),
        ind("core_mom", "Core CPI, month over month", "%", 1, ["KXCPICORE"], discrete=True),
        ind("ppi_mom", "PPI, month over month", "%", 1, ["KXUSPPI"], discrete=True),
        ind("ppi_yoy", "PPI, year over year", "%", 1, ["KXUSPPIYOY"], discrete=True),
        ind("pce", "PCE inflation, month over month", "%", 1, ["KXPCEHEAD"], discrete=True),
        ind("core_pce", "Core PCE, month over month", "%", 1, ["KXPCECORE"], discrete=True),
    )),
    ("employment", "Employment", (
        ind("unemployment", "Unemployment rate", "%", 1, ["KXU3", "KXU3EOY"], discrete=True,
            note="Monthly releases plus year-end contracts"),
        ind("payrolls", "Nonfarm payrolls, monthly change", "k", 0, ["KXPAYROLLS"], scale=0.001),
        ind("claims", "Initial jobless claims (weekly)", "k", 0, ["KXJOBLESSCLAIMS"], scale=0.001),
        ind("cont_claims", "Continuing jobless claims (weekly)", "k", 0, ["KXCONTCLAIMS"]),
        ind("earnings", "Average hourly earnings, month over month", "%", 1, ["KXAHEMOM"], discrete=True),
        ind("lfpr", "Labor force participation, year-end", "%", 2, ["KXLFPRATEEOY"]),
    )),
    ("growth", "Growth", (
        ind("gdp", "Real GDP growth (quarterly, annualized)", "%", 1, ["KXGDP"]),
        ind("nominal_gdp", "Nominal GDP growth (calendar year)", "%", 1, ["KXNOMGDPGROWTH"]),
    )),
)


def _event_time(e: dict) -> str:
    # Active markets only: ladder strikes that already closed early (e.g. a
    # "how high this year" level that was hit) would date the event in the past.
    closes = [m.get("close_time") or "" for m in e.get("markets") or [] if m.get("status") == "active"]
    return e.get("strike_date") or (min(closes) if closes else "")


def _upcoming(events: list[dict], series: str, now: datetime) -> list[dict]:
    evs = [e for e in events if e.get("series_ticker") == series
           and any(m.get("status") == "active" for m in e.get("markets") or [])]
    evs = [e for e in evs if _event_time(e) >= now.isoformat()[:19]]
    return sorted(evs, key=_event_time)


def _liquid(m: dict, previous: bool) -> bool:
    return market_mid(m, previous, MAX_SPREAD) is not None


def _dist_pair(markets, discrete, scale, discrete_step=0.1):
    """Current distribution plus its 24h change, with the change measured on
    the strikes that were liquid both now and 24h ago (otherwise a strike
    dropping in or out of the liquid set looks like a move)."""
    now = ladder_distribution(markets, False, discrete, scale, discrete_step=discrete_step)
    if not now:
        return None
    common = {m["ticker"] for m in markets if _liquid(m, False) and _liquid(m, True)}
    a = ladder_distribution(markets, False, discrete, scale, common, discrete_step)
    b = ladder_distribution(markets, True, discrete, scale, common, discrete_step)
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


def fed_path(events: list[dict], now: datetime, max_meetings: int = 9, days: int = 366) -> dict:
    """Expected Fed upper bound after each meeting.

    The path comes from the (liquid) hike/hold/cut markets: today's rate plus
    the running sum of expected changes, which is valid whatever the
    correlation between meetings. Each meeting's own rate ladder supplies the
    10-90% range where its strikes are liquid enough."""
    decisions = {e.get("strike_date"): e for e in events if e.get("series_ticker") == "KXFEDDECISION"}
    ladders = {e.get("strike_date"): e for e in events if e.get("series_ticker") == FED_SERIES}
    horizon = datetime.fromtimestamp(now.timestamp() + days * 86400, timezone.utc).isoformat()[:19]
    dates = sorted(d for d in set(decisions) | set(ladders)
                   if d and now.isoformat()[:19] <= d <= horizon)[:max_meetings]
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
            d = _dist_pair(lad["markets"], True, 1.0, 0.25)
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


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_MONTH_RE = re.compile(r"\b(" + "|".join(m[:3] + "(?:" + m[3:] + ")?" for m in _MONTHS)
                       + r")\b\.?(?:\s+(?:\d{1,2},\s*)?(\d{4}))?", re.I)


def reference_period(title: str, resolves: str) -> tuple[str, str]:
    """(label, ISO date) of the period a contract measures, from its title:
    "CPI YoY in November" -> Nov of the latest November before it resolves;
    "...in December 2027" / "year-end 2027" -> Dec 2027; "Q4 2026" -> Q4 2026.
    Falls back to the resolution date."""
    res = datetime.fromisoformat(resolves.replace("Z", "+00:00")) if resolves else None
    t = title.lower()
    q = re.search(r"\bq([1-4])\s*(\d{4})", t)
    if q:
        qn, y = int(q.group(1)), int(q.group(2))
        return f"Q{qn} {str(y)[2:]}", datetime(y, qn * 3, 15, tzinfo=timezone.utc).isoformat()
    ye = re.search(r"(year-end|end of|in)\s+(\d{4})\b", t)
    m = _MONTH_RE.search(title)
    if m:
        month = next(i for i, name in enumerate(_MONTHS, 1) if name.startswith(m.group(1).lower()[:3]))
        if m.group(2):
            year = int(m.group(2))
        elif res:
            year = res.year if month <= res.month else res.year - 1
        else:
            return title, resolves
        d = datetime(year, month, 15, tzinfo=timezone.utc)
        return d.strftime("%b ") + str(year)[2:], d.isoformat()
    if ye:
        y = int(ye.group(2))
        label = str(y) if ye.group(1) == "in" else f"Dec {str(y)[2:]}"  # calendar year vs year-end
        return label, datetime(y, 12, 15, tzinfo=timezone.utc).isoformat()
    if res:
        return res.strftime("%b ") + str(res.year)[2:], res.isoformat()
    return title, resolves


def indicator(cfg: dict, events: list[dict], now: datetime) -> dict | None:
    """Upcoming points (one per release or year-end contract within a year),
    each with median, 10-90% range, mean and 24h change. The first point also
    carries its full distribution."""
    limit = datetime.fromtimestamp(now.timestamp() + HORIZON_DAYS * 86400, timezone.utc).isoformat()[:19]
    points = []
    for series in cfg["series"]:
        for e in _upcoming(events, series, now):
            when = _event_time(e)
            if when > limit:
                break
            d = _dist_pair(e["markets"], cfg["discrete"], cfg["scale"], cfg.get("discrete_step", 0.1))
            if not d:
                continue
            label, period = reference_period(e.get("title", ""), when)
            points.append({"event_ticker": e["event_ticker"], "title": e.get("title", ""), "date": when,
                           "period": period, "period_label": label,
                           "series": series, "volume_24h": round(sum(_f(m.get("volume_24h_fp")) or 0 for m in e["markets"]), 2),
                           **{k: d[k] for k in ("median", "p10", "p90", "mean", "mode", "low_confidence",
                                                "liquid_strikes", "change_24h", "buckets")}})
    if not points:
        return None
    points.sort(key=lambda p: (p["period"], p["date"]))
    for p in points[1:]:
        p.pop("buckets", None)  # keep the snapshot small: only the next release's histogram
    return {k: cfg[k] for k in ("key", "label", "unit", "decimals", "note")} | {"points": points}


def sections(events: list[dict], now: datetime) -> list[dict]:
    out = []
    for key, title, cfgs in SECTIONS:
        items = [x for x in (indicator(c, events, now) for c in cfgs) if x]
        out.append({"key": key, "title": title, "indicators": items})
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
    return {"fed_path": fed_path(events, now), "sections": sections(events, now), "gauges": gauges(events, now)}
