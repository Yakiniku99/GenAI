# Kalshi Economics Arbitrage Dashboard

A live dashboard that scans every open **Economics** market on [Kalshi](https://kalshi.com)
and flags baskets of contracts whose *guaranteed* settlement payout is worth more than
what they cost to buy right now, after trading fees. **Fees default to Robinhood's schedule**
(for trading Kalshi contracts through Robinhood); Robinhood Gold and Kalshi-direct are also available.

Pure Python standard library, so there's nothing to `pip install` (Python 3.10+). It uses only Kalshi's public
market-data API, so no account or API key is needed.

## Run the demo

There are three ways to run it, from quickest to most live:

| | Command | What you get |
|---|---|---|
| **1. Instant demo** | `python -m kalshi_arb demo` | Opens the dashboard in your browser on bundled sample data from a real full scan. No network needed, starts in about a second. |
| **2. Live, on your machine** | `python -m kalshi_arb serve --open` | Scans Kalshi live (first full scan takes ~3 min), then rescans every 3 min. |
| **3. Hosted link** | GitHub Pages (below) | A public URL that rebuilds from live Kalshi data every 30 minutes. Good for sharing. |

```bash
git clone https://github.com/Yakiniku99/GenAI.git && cd GenAI
python -m kalshi_arb demo
```

Faster live demo: watch a handful of popular series so scans take seconds:

```bash
python -m kalshi_arb serve --open --series KXFED KXFEDDECISION KXCPIYOY KXPAYROLLS KXRATECUTCOUNT
```

With Docker:

```bash
docker build -t kalshi-arb . && docker run --rm -p 8000:8000 kalshi-arb
```

### Hosted on GitHub Pages

`.github/workflows/pages.yml` runs the tests, does a live scan, and publishes `index.html` +
`snapshot.json` as a static site on every push and every 30 minutes.

1. In the repo, open **Settings → Pages** and set **Source** to **GitHub Actions**.
2. Open **Actions → Publish dashboard → Run workflow**, or just push.
3. The site appears at `https://<user>.github.io/<repo>/`.

On the hosted page, **↻ Refresh** loads the newest published scan. **Run new scan ↗** opens the workflow
so the repo owner can start a fresh Kalshi scan; it shows up about 4 minutes later. (The browser can't
query Kalshi directly because Kalshi's API blocks cross-site requests, so scanning runs in GitHub
Actions or the local server.) Scheduled runs only fire on the repo's default branch. To build the same static site locally, run
`python -m kalshi_arb build --out _site`.

Fees default to Robinhood without Gold. Add `--fees robinhood-gold` or `--fees kalshi` to any command.

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

### Stale and outdated baskets are filtered out

Some baskets look like arbitrage only because their quotes can't be trusted. These are hidden by default
(a "Show stale / outdated" toggle brings them back with the reason):

| Filter | Rule | Example |
|---|---|---|
| **Prices imply a missing outcome** | "Buy every YES" set whose asks sum below 80¢, with no strike coverage proving exhaustiveness | *When will the next US recession start?* YES asks summed to $0.16, meaning the market priced roughly 84% on "no recession", which isn't listed |
| **Outcome periods already over** | An outcome names a quarter, month or year that has ended | The same set listed Q4 2024 to Q1 2026. These stay open until Dec 31, 2026 only because NBER dates recessions after the fact |
| **Stale quotes** | Any leg with no trade in 14+ days (from Kalshi's trade history, checked for the top candidates) | Q1 2025 last traded 40 days before the scan |

## Dashboard

* **Summary tiles**: guaranteed arbitrage count, capturable profit, best net edge, coverage.
* **Arbitrage opportunities**: every profitable basket with the exact legs to buy, cost, min payout,
  net edge per basket, max size, net profit, return, annualized return and settle date. You can filter it
  and sort any column.
* **Closest to arbitrage**: the 15 best fully guaranteed baskets by net edge, as a chart or a table. This
  shows how far prices need to move, which is useful because real arbitrage on Kalshi is rare and short-lived.
* **Liquidity**: every leg shows its bid-ask spread, 24h volume and open interest. Each basket
  gets a tier (High ≤ 2¢ spread & ≥ $1,000 at best prices; Medium ≤ 5¢ & ≥ $100; Low ≤ 10¢ & ≥ $10;
  otherwise Thin) and can be filtered by it. For the 40 best candidates (`--depth N`), the full
  order book is read to show contracts within 5¢ of the best price and a **full-book fill**: how many
  baskets can be bought down the book while each one stays profitable after fees.
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
