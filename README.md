# Kalshi Economics Arbitrage Dashboard

A live dashboard that scans every open **Economics** market on [Kalshi](https://kalshi.com)
and flags baskets of contracts whose *guaranteed* settlement payout is worth more than
what they cost to buy right now, after trading fees. **Fees default to Robinhood's schedule**
(for trading Kalshi contracts through Robinhood); Robinhood Gold and Kalshi-direct are also available.

Pure Python standard library, so there's nothing to `pip install` (Python 3.10+). It uses only Kalshi's public
market-data API, so no account or API key is needed.

```bash
python -m kalshi_arb serve                         # Robinhood fees, open http://127.0.0.1:8000
python -m kalshi_arb serve --fees robinhood-gold    # if you have Robinhood Gold
```

## What it detects

| Strategy | Applies to | Basket | Guaranteed payout | Arbitrage when |
|---|---|---|---|---|
| **Buy every NO** | Mutually exclusive events (Fed decision, CPI buckets, …) | NO on each of *k* outcomes | ≥ $(k−1), since at most one outcome is YES | Σ NO asks < k−1, i.e. Σ YES bids > $1 |
| **Buy every YES** | Mutually exclusive *and exhaustive* events | YES on every outcome | exactly $1 | Σ YES asks < $1 |
| **Strike-ladder inversion** | Threshold ladders in one event ("above 3.00%", "above 3.25%" …) | YES on the easier strike + NO on the harder one | ≥ $1 | YES bid (harder) > YES ask (easier) |

Every basket is costed at the top of the book (a NO ask is $1 − YES bid, with YES-bid depth).
Size is capped by the thinnest leg's top-level depth. Each leg is one order and pays the fees of
the venue you pick with `--fees`:

| `--fees` | Per-order fee | Source |
|---|---|---|
| `robinhood` (default) | `min(ceil_to_cent(0.10 × P × (1−P) × C), $0.01 × C)` commission **+ $0.01 × C** Kalshi exchange fee | Robinhood event-contract fee schedule (effective June 1, 2026) |
| `robinhood-gold` | same, with 0.05 instead of 0.10 | 〃 |
| `kalshi` | `ceil_to_cent(0.07 × series_multiplier × C × P × (1−P))` | Kalshi fee schedule |

Fees are charged on the buy; holding to settlement involves no closing trade. On Robinhood every
leg costs at least 1¢ per contract, so a 2-leg ladder needs about 3–4¢ of mispricing to break even
and a 6-leg bucket set more than 6¢. Robinhood doesn't list every Kalshi market, so check each leg is
tradable there. Fee schedules change, so check Robinhood's current schedule before trading.

**"Buy every YES" is only risk-free if the listed outcomes are exhaustive.** The scanner verifies
this automatically when strike ranges chain from an open-ended low bucket to an open-ended high bucket
with no holes. Otherwise it tags the basket **⚠ verify exhaustive** and keeps it out of the
"Guaranteed arbitrages" count. Example: a *"When will the next US recession start?"* set
looks like an 80¢ arbitrage but has no "no recession" outcome.

## Dashboard

* **Summary tiles**: guaranteed arbitrage count, capturable profit, best net edge, coverage.
* **Arbitrage opportunities**: every profitable basket with the exact legs to buy, cost, min payout,
  net edge per basket, max size, net profit, return, annualized return and settle date. You can filter it
  and sort any column.
* **Closest to arbitrage**: the 15 best fully guaranteed baskets by net edge, as a chart or a table. This
  shows how far prices need to move, which is useful because real arbitrage on Kalshi is rare and short-lived.
* **Bucket-set pricing**: Σ YES ask / Σ YES bid for each mutually exclusive event, with book width
  and whether strikes cover every outcome.

## Usage

```bash
# Live dashboard; rescans every 3 minutes (full series discovery every 10th scan)
python -m kalshi_arb serve --port 8000 --interval 180

# Only watch a few series (much faster)
python -m kalshi_arb serve --series KXFED KXFEDDECISION KXCPIYOY KXPAYROLLS

# One-off scan to the terminal / a JSON file
python -m kalshi_arb scan
python -m kalshi_arb scan -o snapshot.json

# Replay a saved snapshot in the dashboard (no network)
python -m kalshi_arb serve --snapshot snapshot.json
```

`kalshi_arb/static/index.html` also works as a static page. Without the server it loads
`snapshot.json` (or `?data=<url>`) from next to itself.

A full scan of all ~870 Economics series takes about 2 minutes at the default 12 requests/sec. Use `--rate`
and `--workers` to tune it. Kalshi rate-limits aggressive clients, and the client backs off on HTTP 429.

## Layout

```
kalshi_arb/
  client.py      rate-limited Kalshi REST client with retries
  arbitrage.py   fee model + detection strategies (pure functions)
  scanner.py     fetches series → open events/markets → opportunities snapshot
  server.py      stdlib HTTP server + background rescan loop
  static/index.html  the dashboard (no external dependencies)
tests/test_arbitrage.py
```

```bash
python -m unittest discover -s tests
```

## Caveats

This is a screener, not an execution engine. Quotes can move between legs, only the top
price level is read, and settlement rules sometimes differ from market titles. Confirm the
rules and the live order book before trading. Not investment advice.
