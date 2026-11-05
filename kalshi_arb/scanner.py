"""Fetch every open economics event from Kalshi and run the arbitrage checks."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

from .arbitrage import (DEFAULT_FEE_MODEL, FEE_MODELS, Opportunity, Quote, analyze_events,
                        asks_from_orderbook, depth_within, mark_stale, strike_coverage_is_exhaustive,
                        walk_book)
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


def add_book_depth(client: KalshiClient, opps: list[Opportunity], fee_model: str,
                   fee_mult: dict[str, float], limit: int, workers: int,
                   now: datetime | None = None) -> list[str]:
    """For every arbitrage plus the best `limit` other candidates, read the
    full order book (fillable size) and the last trade time (stale quotes)."""
    if limit <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    ranked = [o for o in opps if o.max_baskets >= 1]
    chosen = [o for o in ranked if o.is_arb]
    chosen += [o for o in ranked if not o.is_arb and not o.filter_reasons][:max(limit - len(chosen), 0)]
    tickers = sorted({l.ticker for o in chosen for l in o.legs})
    books: dict[str, dict] = {}
    last_trade: dict[str, str | None] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.orderbook, t): ("book", t) for t in tickers}
        futures |= {pool.submit(client.last_trade_time, t): ("trade", t) for t in tickers}
        for fut in as_completed(futures):
            kind, t = futures[fut]
            try:
                (books if kind == "book" else last_trade)[t] = fut.result()
            except Exception as e:
                errors.append(f"{kind} {t}: {e}")
    model = FEE_MODELS[fee_model]
    for o in chosen:
        if all(l.ticker in last_trade for l in o.legs):
            for leg in o.legs:
                leg.last_trade = last_trade[leg.ticker]
            mark_stale(o, now)
        if any(l.ticker not in books for l in o.legs):
            continue
        ladders = [asks_from_orderbook(books[l.ticker], l.side) for l in o.legs]
        for leg, lad in zip(o.legs, ladders):
            leg.depth_5c = depth_within(lad)
        o.liquidity["book"] = walk_book(ladders, o.guaranteed_payout,
                                        model.for_series(fee_mult.get(o.series_ticker, 1.0)))
    return errors


def scan(
    client: KalshiClient | None = None,
    category: str = "Economics",
    series_filter: list[str] | None = None,
    workers: int = 10,
    progress: ProgressFn | None = None,
    fee_model: str = DEFAULT_FEE_MODEL,
    depth_candidates: int = 40,
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
    opps = analyze_events(events, fee_mult, now, fee_model)
    book_errors = add_book_depth(client, opps, fee_model, fee_mult, depth_candidates, workers, now)
    errors += book_errors
    # Trustworthy baskets first; filtered ones (stale / outdated / missing outcome) sink.
    opps.sort(key=lambda o: (not o.filter_reasons, o.is_arb, o.net_edge, o.net_profit), reverse=True)
    summaries = [s for s in (_event_summary(e) for e in events) if s]
    n_markets = sum(len(e.get("markets") or []) for e in events)
    return {
        "generated_at": now.isoformat(),
        "scan_seconds": round(time.time() - started, 1),
        "category": category,
        "fee_model": {"key": fee_model, "label": FEE_MODELS[fee_model].label,
                      "description": FEE_MODELS[fee_model].description},
        "series_scanned": len(series),
        "series_with_open_events": len({e.get("series_ticker") for e in events}),
        "events_scanned": len(events),
        "markets_scanned": n_markets,
        "series_titles": {k: v for k, v in titles.items() if any(e.get("series_ticker") == k for e in events)},
        "depth_candidates": depth_candidates,
        "filtered_count": sum(1 for o in opps if o.filter_reasons),
        "opportunities": [o.to_dict() for o in opps],
        "mutually_exclusive_events": sorted(summaries, key=lambda s: -(s["volume_24h"] or 0)),
        "errors": errors,
    }
