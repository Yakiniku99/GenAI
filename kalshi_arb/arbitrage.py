"""Arbitrage detection for Kalshi binary markets.

Every strategy here builds a basket of contracts whose *minimum* payout at
settlement is known in advance, then compares it to what the basket costs to
buy right now at the top of the book, plus trading fees for the chosen
venue (Kalshi direct, or Robinhood with or without Gold; see FEE_MODELS).

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
from typing import Callable, Iterable

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


def _ceil_cent(x: float) -> float:
    # round() guards against float noise pushing e.g. 0.0700000001 up a cent
    return math.ceil(round(x * 100, 6)) / 100


def taker_fee(price: float, count: int, multiplier: float = 1.0) -> float:
    """Kalshi taker fee for one order: ceil_to_cent(0.07 * m * C * P * (1-P))."""
    if count <= 0 or multiplier <= 0:
        return 0.0
    return _ceil_cent(TAKER_FEE_RATE * multiplier * count * price * (1 - price))


# Robinhood Derivatives fee schedule (effective June 1, 2026), for Kalshi
# contracts traded through Robinhood:
#   commission    = ceil_to_cent(k * P * (1-P) * C), capped at $0.01/contract
#                   k = 0.05 with Robinhood Gold, 0.10 without
#   exchange fee  = $0.01 per contract, per side (KalshiEx via Robinhood)
# Kalshi's own taker fee and per-series multiplier do not apply on top.
ROBINHOOD_EXCHANGE_FEE = 0.01
ROBINHOOD_COMMISSION_CAP = 0.01


def robinhood_fee(price: float, count: int, gold: bool = False) -> float:
    if count <= 0:
        return 0.0
    k = 0.05 if gold else 0.10
    commission = min(_ceil_cent(k * price * (1 - price) * count), ROBINHOOD_COMMISSION_CAP * count)
    return round(commission + ROBINHOOD_EXCHANGE_FEE * count, 2)


FeeFn = Callable[[float, int], float]  # (price, contracts) -> dollars for one order


@dataclass(frozen=True)
class FeeModel:
    key: str
    label: str
    description: str

    def for_series(self, kalshi_multiplier: float = 1.0) -> FeeFn:
        if self.key == "kalshi":
            return lambda p, c: taker_fee(p, c, kalshi_multiplier)
        gold = self.key == "robinhood-gold"
        return lambda p, c: robinhood_fee(p, c, gold)


FEE_MODELS = {
    m.key: m for m in (
        FeeModel("robinhood", "Robinhood (no Gold)",
                 "10% × P × (1−P) commission (max 1¢) + 1¢ Kalshi exchange fee, per contract per order"),
        FeeModel("robinhood-gold", "Robinhood Gold",
                 "5% × P × (1−P) commission (max 1¢) + 1¢ Kalshi exchange fee, per contract per order"),
        FeeModel("kalshi", "Kalshi direct",
                 "7% × P × (1−P) taker fee × series multiplier, per order"),
    )
}
DEFAULT_FEE_MODEL = "robinhood"


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
    open_interest: float = 0.0
    volume: float = 0.0

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
            open_interest=_f(m.get("open_interest_fp")),
            volume=_f(m.get("volume_fp")),
        )

    @property
    def spread(self) -> float | None:
        """YES bid-ask spread in dollars (same for NO); None if one side is empty."""
        if self.yes_bid_size > 0 and self.yes_ask_size > 0:
            return round(self.yes_ask - self.yes_bid, 4)
        return None

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
    size: float                       # contracts at the best ask
    spread: float | None = None       # bid-ask spread, dollars
    volume_24h: float = 0.0           # contracts traded in the last 24h
    open_interest: float = 0.0        # contracts outstanding
    depth_5c: float | None = None     # contracts offered within 5c of best ask (order book)

    @classmethod
    def from_quote(cls, q: "Quote", side: str) -> "Leg":
        return cls(q.ticker, q.label, side, q.ask(side), q.depth(side),
                   q.spread, q.volume_24h, q.open_interest)


# Liquidity tiers for a basket: the widest leg spread and the dollars you can
# put to work at the best prices. 24h volume and open interest are reported
# alongside but don't set the tier: a resting book is fillable even on a day
# nobody traded.
LIQUIDITY_TIERS = (  # (tier, max spread in dollars, min top-of-book depth in dollars)
    ("high", 0.02, 1000),
    ("medium", 0.05, 100),
    ("low", 0.10, 10),
)


def liquidity_summary(legs: list[Leg], cost: float, max_baskets: int) -> dict:
    spreads = [l.spread for l in legs]
    worst_spread = None if any(s is None for s in spreads) else max(spreads)
    depth_usd = round(cost * max_baskets, 2)
    tier = "thin"
    if worst_spread is not None:
        for name, max_spread, min_depth in LIQUIDITY_TIERS:
            if worst_spread <= max_spread + EPS and depth_usd >= min_depth:
                tier = name
                break
    return {
        "tier": tier,
        "max_spread": worst_spread,
        "top_depth_usd": depth_usd,
        "min_volume_24h": round(min(l.volume_24h for l in legs), 2),
        "min_open_interest": round(min(l.open_interest for l in legs), 2),
        "book": None,  # filled by walk_book() for top candidates
    }


Level = tuple[float, float]  # (price to buy at, contracts)


def asks_from_orderbook(book: dict, side: str) -> list[Level]:
    """Kalshi books list resting *bids* for each side. Buying YES fills
    against NO bids at 1 - p, and vice versa. Returns asks, best first."""
    ob = book.get("orderbook_fp") or {}
    opposite = ob.get("no_dollars" if side == "yes" else "yes_dollars") or []
    levels = [(round(1 - _f(p), 4), _f(q)) for p, q in opposite if _f(q) > 0]
    return sorted(levels)


def walk_book(ladders: list[list[Level]], payout: float, fee_fn: FeeFn) -> dict:
    """Buy baskets level by level while each extra chunk is still profitable
    after fees. Returns fillable baskets and profit at full depth."""
    idx = [0] * len(ladders)
    left = [lad[0][1] if lad else 0.0 for lad in ladders]
    baskets = cost = fees = 0.0
    worst_cost = None
    while all(i < len(l) for i, l in zip(idx, ladders)):
        q = int(min(left) + EPS)
        prices = [l[i][0] for i, l in zip(idx, ladders)]
        if q >= 1:
            chunk_cost = sum(prices) * q
            chunk_fees = sum(fee_fn(p, q) for p in prices)
            if payout * q - chunk_cost - chunk_fees <= EPS:
                break
            baskets += q
            cost += chunk_cost
            fees += chunk_fees
            worst_cost = sum(prices)
        step = min(left)
        for k in range(len(ladders)):
            left[k] -= step
            if left[k] <= EPS:
                idx[k] += 1
                if idx[k] < len(ladders[k]):
                    left[k] = ladders[k][idx[k]][1]
        if q < 1 and step <= EPS:
            break
    profit = payout * baskets - cost - fees
    return {
        "baskets": int(baskets),
        "capital": round(cost + fees, 2),
        "fees": round(fees, 2),
        "profit": round(profit, 2),
        "worst_basket_cost": round(worst_cost, 4) if worst_cost is not None else None,
    }


def depth_within(levels: list[Level], width: float = 0.05) -> float:
    if not levels:
        return 0.0
    best = levels[0][0]
    return round(sum(q for p, q in levels if p <= best + width + EPS), 2)


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
    fees: float               # total fees at max_baskets (or 1 if no depth)
    net_profit: float         # total, after fees, at max_baskets
    net_edge: float           # per basket, after fees
    gross_edge: float         # per basket, before fees
    roi: float                # net_profit / capital deployed
    annualized_roi: float | None
    days_to_close: float | None
    close_time: str | None
    flags: list[str] = field(default_factory=list)
    liquidity: dict = field(default_factory=dict)

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
    fee_fn: FeeFn,
    now: datetime,
    flags: Iterable[str] = (),
) -> Opportunity:
    legs = [Leg.from_quote(q, s) for q, s in zip(quotes, sides)]
    cost = round(sum(l.price for l in legs), 4)
    max_baskets = int(min(l.size for l in legs) + EPS)
    n = max(max_baskets, 1)
    fees = round(sum(fee_fn(l.price, n) for l in legs), 2)
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
        liquidity=liquidity_summary(legs, cost, max_baskets),
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


def analyze_event(event: dict, fee_fn: FeeFn | None = None, now: datetime | None = None) -> list[Opportunity]:
    """Return every candidate basket for one event (profitable or not)."""
    now = now or datetime.now(timezone.utc)
    fee_fn = fee_fn or FEE_MODELS[DEFAULT_FEE_MODEL].for_series()
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
                                    1.0, fee_fn, now, flags))
        # Buy every NO: k legs pay >= k-1 whenever at most one resolves YES.
        no_legs = [q for q in quotes if q.can_buy("no")]
        if len(no_legs) >= 2:
            out.append(price_basket("me_all_no", event, no_legs, ["no"] * len(no_legs),
                                    float(len(no_legs) - 1), fee_fn, now))
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
                               1.0, fee_fn, now)
            if best is None or (opp.net_edge, opp.net_profit) > (best.net_edge, best.net_profit):
                best = opp
        if best:
            out.append(best)
    return out


def analyze_events(events: list[dict], fee_multipliers: dict[str, float] | None = None,
                   now: datetime | None = None, fee_model: str = DEFAULT_FEE_MODEL) -> list[Opportunity]:
    """fee_multipliers are Kalshi's per-series multipliers (only used by the
    "kalshi" fee model)."""
    fee_multipliers = fee_multipliers or {}
    model = FEE_MODELS[fee_model]
    now = now or datetime.now(timezone.utc)
    opps: list[Opportunity] = []
    for ev in events:
        fee_fn = model.for_series(fee_multipliers.get(ev.get("series_ticker", ""), 1.0))
        opps.extend(analyze_event(ev, fee_fn, now))
    opps.sort(key=lambda o: (o.is_arb, o.net_edge, o.net_profit), reverse=True)
    return opps
