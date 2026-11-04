"""Arbitrage detection for Kalshi binary markets.

Every strategy here builds a basket of contracts whose *minimum* payout at
settlement is known in advance, then compares it to what the basket costs to
buy right now at the top of the book (plus Kalshi taker fees).

Strategies
----------
``me_all_yes``
    Mutually exclusive event: buy YES on every outcome. Exactly one outcome
    pays $1 *if the listed outcomes are exhaustive*, so this is flagged unless
    the strikes provably cover the whole number line.
``me_all_no``
    Mutually exclusive event: buy NO on every outcome. At most one outcome can
    resolve YES, so k NO contracts always pay at least $(k-1). Needs only
    mutual exclusivity, not exhaustiveness.
``ladder``
    Threshold ladder ("above 3.0%", "above 3.25%" ...) in one event. If
    ``X > b`` then ``X > a`` for ``a < b``, so YES(a) + NO(b) always pays at
    least $1. Profitable when YES bid on the higher strike exceeds YES ask on
    the lower one (and the mirror image for "below" ladders).

Prices are in dollars (0-1). On Kalshi the NO ask is ``1 - YES bid`` and its
displayed depth is the YES-bid depth.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Iterable

TAKER_FEE_RATE = 0.07
EPS = 1e-9

GREATER_TYPES = ("greater", "greater_or_equal")
LESS_TYPES = ("less", "less_or_equal")

STRATEGY_LABELS = {
    "me_all_yes": "Buy every YES (bucket set)",
    "me_all_no": "Buy every NO (bucket set)",
    "ladder": "Strike-ladder inversion",
}


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def taker_fee(price: float, count: int, multiplier: float = 1.0) -> float:
    """Kalshi taker fee for one order: ceil_to_cent(0.07 * m * C * P * (1-P))."""
    if count <= 0 or multiplier <= 0:
        return 0.0
    raw = TAKER_FEE_RATE * multiplier * count * price * (1 - price)
    # round() guards against float noise pushing e.g. 0.0700000001 up a cent
    return math.ceil(round(raw * 100, 6)) / 100


@dataclass
class Quote:
    ticker: str
    label: str
    yes_bid: float
    yes_ask: float
    yes_bid_size: float
    yes_ask_size: float
    strike_type: str | None
    floor: float | None
    cap: float | None
    status: str
    close_time: str | None
    volume_24h: float

    @classmethod
    def from_market(cls, m: dict) -> "Quote":
        return cls(
            ticker=m["ticker"],
            label=m.get("yes_sub_title") or m.get("subtitle") or m["ticker"],
            yes_bid=_f(m.get("yes_bid_dollars")),
            yes_ask=_f(m.get("yes_ask_dollars"), 1.0),
            yes_bid_size=_f(m.get("yes_bid_size_fp")),
            yes_ask_size=_f(m.get("yes_ask_size_fp")),
            strike_type=m.get("strike_type"),
            floor=m.get("floor_strike"),
            cap=m.get("cap_strike"),
            status=m.get("status", ""),
            close_time=m.get("expected_expiration_time") or m.get("close_time"),
            volume_24h=_f(m.get("volume_24h_fp")),
        )

    @property
    def active(self) -> bool:
        return self.status == "active"

    # Buying YES lifts the YES ask; buying NO lifts the NO ask (= 1 - YES bid).
    def can_buy(self, side: str) -> bool:
        if side == "yes":
            return self.yes_ask_size > 0 and 0 < self.yes_ask < 1
        return self.yes_bid_size > 0 and 0 < self.yes_bid < 1

    def ask(self, side: str) -> float:
        return self.yes_ask if side == "yes" else round(1 - self.yes_bid, 4)

    def depth(self, side: str) -> float:
        return self.yes_ask_size if side == "yes" else self.yes_bid_size


@dataclass
class Leg:
    ticker: str
    label: str
    side: str
    price: float
    size: float


@dataclass
class Opportunity:
    strategy: str
    event_ticker: str
    event_title: str
    series_ticker: str
    legs: list[Leg]
    guaranteed_payout: float  # per basket
    cost: float               # per basket, before fees
    max_baskets: int          # limited by top-of-book depth
    fees: float               # total taker fees at max_baskets (or 1 if no depth)
    net_profit: float         # total, after fees, at max_baskets
    net_edge: float           # per basket, after fees
    gross_edge: float         # per basket, before fees
    roi: float                # net_profit / capital deployed
    annualized_roi: float | None
    days_to_close: float | None
    close_time: str | None
    flags: list[str] = field(default_factory=list)

    @property
    def is_arb(self) -> bool:
        return self.net_edge > EPS and self.max_baskets >= 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["strategy_label"] = STRATEGY_LABELS[self.strategy]
        d["is_arb"] = self.is_arb
        d["url"] = f"https://kalshi.com/markets/{self.series_ticker.lower()}"
        return d


def _days_until(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max((t - now).total_seconds() / 86400, 0.0)


def price_basket(
    strategy: str,
    event: dict,
    quotes: list[Quote],
    sides: list[str],
    payout: float,
    fee_multiplier: float,
    now: datetime,
    flags: Iterable[str] = (),
) -> Opportunity:
    legs = [Leg(q.ticker, q.label, s, q.ask(s), q.depth(s)) for q, s in zip(quotes, sides)]
    cost = round(sum(l.price for l in legs), 4)
    max_baskets = int(min(l.size for l in legs) + EPS)
    n = max(max_baskets, 1)
    fees = round(sum(taker_fee(l.price, n, fee_multiplier) for l in legs), 2)
    net_profit = round((payout - cost) * n - fees, 4)
    capital = cost * n + fees
    close = max((q.close_time for q in quotes if q.close_time), default=None)
    days = _days_until(close, now)
    roi = net_profit / capital if capital > 0 else 0.0
    ann = None
    if days is not None and days > 0 and roi > -1:
        ann = (1 + roi) ** (365 / max(days, 1)) - 1 if roi > 0 else roi * 365 / max(days, 1)
    flags = list(flags)
    if max_baskets < 1:
        flags.append("no_depth")
    return Opportunity(
        strategy=strategy,
        event_ticker=event["event_ticker"],
        event_title=event.get("title", ""),
        series_ticker=event.get("series_ticker", ""),
        legs=legs,
        guaranteed_payout=payout,
        cost=cost,
        max_baskets=max_baskets,
        fees=fees,
        net_profit=net_profit,
        net_edge=round(net_profit / n, 4),
        gross_edge=round(payout - cost, 4),
        roi=round(roi, 6),
        annualized_roi=round(ann, 6) if ann is not None else None,
        days_to_close=round(days, 2) if days is not None else None,
        close_time=close,
        flags=flags,
    )


def strike_coverage_is_exhaustive(quotes: list[Quote]) -> bool:
    """True when strikes chain from an open-ended low bucket to an open-ended
    high bucket with no gaps (e.g. "<2%", "2-2.4%", "2.5-2.9%", ">=3%")."""
    lows = [q for q in quotes if q.strike_type in LESS_TYPES]
    highs = [q for q in quotes if q.strike_type in GREATER_TYPES]
    mids = [q for q in quotes if q.strike_type == "between"]
    if len(lows) != 1 or len(highs) != 1 or len(mids) + 2 != len(quotes):
        return False
    if any(q.floor is None or q.cap is None for q in mids):
        return False
    mids.sort(key=lambda q: q.floor)
    edges = [(None, lows[0].cap)] + [(q.floor, q.cap) for q in mids] + [(highs[0].floor, None)]
    if any(e is None for e in (lows[0].cap, highs[0].floor)):
        return False
    gaps = [nxt[0] - prev[1] for prev, nxt in zip(edges, edges[1:])]
    if any(g < -EPS for g in gaps):
        return False
    # Discrete reporting leaves a uniform tick between buckets (e.g. 2.4 -> 2.5
    # on a 0.1 grid). Accept that, but not a lone wider hole in the ladder.
    positive = [g for g in gaps if g > EPS]
    if not positive:
        return True
    tick = min(positive)
    widths = [q.cap - q.floor for q in mids]
    return all(abs(g - tick) <= 1e-6 for g in positive) and (not widths or tick <= min(widths) + EPS)


def analyze_event(event: dict, fee_multiplier: float = 1.0, now: datetime | None = None) -> list[Opportunity]:
    """Return every candidate basket for one event (profitable or not)."""
    now = now or datetime.now(timezone.utc)
    all_quotes = [Quote.from_market(m) for m in event.get("markets") or []]
    quotes = [q for q in all_quotes if q.active]
    out: list[Opportunity] = []
    if len(quotes) < 2:
        return out

    if event.get("mutually_exclusive"):
        # Buy every YES: only an arb if exactly one outcome must happen.
        if all(q.can_buy("yes") for q in quotes):
            flags = []
            if len(quotes) != len(all_quotes):
                flags.append("inactive_outcomes")
            if not strike_coverage_is_exhaustive(quotes):
                flags.append("verify_exhaustive")
            out.append(price_basket("me_all_yes", event, quotes, ["yes"] * len(quotes),
                                    1.0, fee_multiplier, now, flags))
        # Buy every NO: k legs pay >= k-1 whenever at most one resolves YES.
        no_legs = [q for q in quotes if q.can_buy("no")]
        if len(no_legs) >= 2:
            out.append(price_basket("me_all_no", event, no_legs, ["no"] * len(no_legs),
                                    float(len(no_legs) - 1), fee_multiplier, now))
        return out

    # Ladders: same event, same strike direction, distinct strikes.
    for st in GREATER_TYPES + LESS_TYPES:
        is_greater = st in GREATER_TYPES
        strike = (lambda q: q.floor) if is_greater else (lambda q: q.cap)
        ladder = sorted((q for q in quotes if q.strike_type == st and strike(q) is not None), key=strike)
        best: Opportunity | None = None
        for lo, hi in combinations(ladder, 2):
            if strike(hi) - strike(lo) <= EPS:
                continue
            # "above hi" implies "above lo": YES(lo) + NO(hi) pays >= $1.
            # "below lo" implies "below hi": YES(hi) + NO(lo) pays >= $1.
            yes_q, no_q = (lo, hi) if is_greater else (hi, lo)
            if not (yes_q.can_buy("yes") and no_q.can_buy("no")):
                continue
            opp = price_basket("ladder", event, [yes_q, no_q], ["yes", "no"],
                               1.0, fee_multiplier, now)
            if best is None or (opp.net_edge, opp.net_profit) > (best.net_edge, best.net_profit):
                best = opp
        if best:
            out.append(best)
    return out


def analyze_events(events: list[dict], fee_multipliers: dict[str, float] | None = None,
                   now: datetime | None = None) -> list[Opportunity]:
    fee_multipliers = fee_multipliers or {}
    now = now or datetime.now(timezone.utc)
    opps: list[Opportunity] = []
    for ev in events:
        opps.extend(analyze_event(ev, fee_multipliers.get(ev.get("series_ticker", ""), 1.0), now))
    opps.sort(key=lambda o: (o.is_arb, o.net_edge, o.net_profit), reverse=True)
    return opps
