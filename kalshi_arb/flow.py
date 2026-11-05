"""Trade-flow primitives shared by the live "What moved" signals and the
research study: parsing Kalshi trades/candles and measuring a price move.

Kalshi trades are anonymous, so "informed" flow has to be inferred from how
trades behave, not who placed them. Every trade has a taker side: the side
that crossed the spread. A YES taker pushes the price up (+1), a NO taker
pushes it down (-1).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


@dataclass(frozen=True)
class Trade:
    ts: float      # epoch seconds
    price: float   # YES price, dollars
    size: float    # contracts
    sign: int      # +1 YES taker (buys pushing price up), -1 NO taker

    @property
    def notional(self) -> float:
        """Dollars the taker paid."""
        return self.size * (self.price if self.sign > 0 else 1 - self.price)


def parse_trades(raw: list[dict]) -> list[Trade]:
    out = []
    for t in raw:
        side = t.get("taker_side") or t.get("taker_outcome_side")
        price, size = _f(t.get("yes_price_dollars")), _f(t.get("count_fp"))
        if side not in ("yes", "no") or price is None or not size:
            continue
        out.append(Trade(_ts(t["created_time"]), price, size, 1 if side == "yes" else -1))
    out.sort(key=lambda t: t.ts)
    return out


class MidSeries:
    """Step function of the YES mid price from hourly candles."""

    def __init__(self, candles: list[dict]):
        pts = []
        for c in candles:
            bid = _f((c.get("yes_bid") or {}).get("close_dollars"))
            ask = _f((c.get("yes_ask") or {}).get("close_dollars"))
            last = _f((c.get("price") or {}).get("close_dollars"))
            if bid is not None and ask is not None and 0 < ask and bid < ask:
                mid = (bid + ask) / 2
            elif last is not None:
                mid = last
            else:
                continue
            pts.append((float(c["end_period_ts"]), mid, _f(c.get("volume_fp"), 0.0),
                        (ask - bid) if bid is not None and ask is not None else None))
        pts.sort()
        self.ts = [p[0] for p in pts]
        self.mid = [p[1] for p in pts]
        self.vol = [p[2] for p in pts]
        self.spread = [p[3] for p in pts]

    def __len__(self):
        return len(self.ts)

    def at(self, ts: float) -> float | None:
        i = bisect.bisect_right(self.ts, ts) - 1
        return self.mid[i] if i >= 0 else None

    def spread_at(self, ts: float) -> float | None:
        i = bisect.bisect_right(self.ts, ts) - 1
        return self.spread[i] if i >= 0 else None

    def volume_between(self, t0: float, t1: float) -> float:
        lo, hi = bisect.bisect_right(self.ts, t0), bisect.bisect_right(self.ts, t1)
        return sum(self.vol[lo:hi])

    def path(self, t0: float, t1: float) -> list[float]:
        lo, hi = bisect.bisect_right(self.ts, t0), bisect.bisect_right(self.ts, t1)
        return self.mid[lo:hi]


def big_trade_threshold(trades: list[Trade], pct: float = 0.90, floor: float = 25.0) -> float:
    """Contracts at the market's own `pct` size percentile (at least `floor`)."""
    if not trades:
        return floor
    sizes = sorted(t.size for t in trades)
    return max(sizes[min(int(pct * len(sizes)), len(sizes) - 1)], floor)


def window_trades(trades: list[Trade], t0: float, t1: float) -> list[Trade]:
    return [t for t in trades if t0 < t.ts <= t1]


def move_features(trades: list[Trade], mids: MidSeries, t0: float, t1: float,
                  direction: int, big: float, baseline_days: float = 7.0) -> dict:
    """Features of a move over (t0, t1] in `direction` (+1 up / -1 down), using
    only information available at t1."""
    w = window_trades(trades, t0, t1)
    vol = sum(t.size for t in w)
    with_move = sum(t.size for t in w if t.sign == direction)
    big_with = sum(t.size for t in w if t.sign == direction and t.size >= big)
    big_all = sum(t.size for t in w if t.size >= big)
    hours = (t1 - t0) / 3600
    base_vol = mids.volume_between(t0 - baseline_days * 86400, t0)
    base_rate = base_vol / (baseline_days * 24) if base_vol else 0.0
    path = mids.path(t0, t1)
    start, end = mids.at(t0), mids.at(t1)
    move = (end - start) * direction if start is not None and end is not None else 0.0
    # Retention: how much of the furthest point reached is still held at t1.
    if path and start is not None:
        furthest = max((p - start) * direction for p in path)
        retention = move / furthest if furthest > 1e-9 else 0.0
    else:
        retention = None
    return {
        "move": round(move, 4),
        "n_trades": len(w),
        "volume": round(vol, 2),
        "notional": round(sum(t.notional for t in w), 2),
        # +1 = every contract crossed the spread in the move's direction
        "flow_imbalance": round((2 * with_move - vol) / vol, 3) if vol else 0.0,
        "big_share": round(big_all / vol, 3) if vol else 0.0,
        "big_with_share": round(big_with / big_all, 3) if big_all else None,
        "volume_ratio": round(vol / (base_rate * hours), 2) if base_rate > 0 else None,
        "retention": round(retention, 3) if retention is not None else None,
        "spread": mids.spread_at(t1),
    }
