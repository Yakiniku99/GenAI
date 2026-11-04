"""Fetch every open economics event from Kalshi and run the arbitrage checks."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

from .arbitrage import analyze_events, strike_coverage_is_exhaustive, Quote
from .client import KalshiClient

ProgressFn = Callable[[int, int], None]


def _event_summary(ev: dict) -> dict | None:
    """Implied-probability totals for mutually exclusive events."""
    if not ev.get("mutually_exclusive"):
        return None
    qs = [Quote.from_market(m) for m in ev.get("markets") or []]
    qs = [q for q in qs if q.active]
    if len(qs) < 2:
        return None
    asks = [q.yes_ask for q in qs if q.can_buy("yes")]
    bids = [q.yes_bid for q in qs if q.can_buy("no")]
    return {
        "event_ticker": ev["event_ticker"],
        "title": ev.get("title", ""),
        "series_ticker": ev.get("series_ticker", ""),
        "outcomes": len(qs),
        "sum_yes_ask": round(sum(asks), 4) if len(asks) == len(qs) else None,
        "sum_yes_bid": round(sum(bids), 4),
        "exhaustive": strike_coverage_is_exhaustive(qs),
        "volume_24h": round(sum(q.volume_24h for q in qs), 2),
    }


def scan(
    client: KalshiClient | None = None,
    category: str = "Economics",
    series_filter: list[str] | None = None,
    workers: int = 10,
    progress: ProgressFn | None = None,
) -> dict:
    client = client or KalshiClient()
    started = time.time()
    series = client.series(category)
    if series_filter:
        wanted = {s.upper() for s in series_filter}
        series = [s for s in series if s["ticker"].upper() in wanted]
    fee_mult = {s["ticker"]: float(s.get("fee_multiplier", 1) or 0) for s in series}
    titles = {s["ticker"]: s.get("title", "") for s in series}

    events: list[dict] = []
    errors: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.open_events_with_markets, s["ticker"]): s["ticker"] for s in series}
        for fut in as_completed(futures):
            try:
                events.extend(fut.result())
            except Exception as e:  # keep scanning; report at the end
                errors.append(f"{futures[fut]}: {e}")
            done += 1
            if progress:
                progress(done, len(series))

    now = datetime.now(timezone.utc)
    opps = analyze_events(events, fee_mult, now)
    summaries = [s for s in (_event_summary(e) for e in events) if s]
    n_markets = sum(len(e.get("markets") or []) for e in events)
    return {
        "generated_at": now.isoformat(),
        "scan_seconds": round(time.time() - started, 1),
        "category": category,
        "series_scanned": len(series),
        "series_with_open_events": len({e.get("series_ticker") for e in events}),
        "events_scanned": len(events),
        "markets_scanned": n_markets,
        "series_titles": {k: v for k, v in titles.items() if any(e.get("series_ticker") == k for e in events)},
        "opportunities": [o.to_dict() for o in opps],
        "mutually_exclusive_events": sorted(summaries, key=lambda s: -(s["volume_24h"] or 0)),
        "errors": errors,
    }
