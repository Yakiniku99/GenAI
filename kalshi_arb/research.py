"""Study: which Kalshi price moves carry information, and do large trades win?

    python -m kalshi_arb research --markets 60 --days 14

Two tests on the most active economics markets:

1. Trade markouts by size. For every trade, the signed change in mid price
   1h / 6h / 24h later, from the taker's point of view, in cents. Positive
   means the taker was right. If large takers are informed ("sharks"), their
   markouts should beat small takers' after the spread.

2. Move persistence. Every 6-hour move of >= 3c is an event. Its features
   (flow imbalance, share of big trades, volume vs normal, trade count,
   spread, path retention, confirmation by sibling strikes) are measured
   using only data up to the end of the move. The outcome is continuation
   over the next 24h. Informed moves should continue or hold; noise should
   revert. Comparing feature terciles shows which features separate the two.

Writes research/informed_flow.json and research/informed_flow.md.
"""

from __future__ import annotations

import json
import math
import pickle
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .client import KalshiClient
from .flow import MidSeries, big_trade_threshold, move_features, parse_trades
from .scanner import fetch_open_events

HORIZONS_H = (1, 6, 24)
SIZE_BUCKETS = ((0, 10, "<10"), (10, 100, "10-99"), (100, 1000, "100-999"), (1000, math.inf, "1,000+"))
MOVE_WINDOW_H = 6
MOVE_MIN = 0.03
FORWARD_H = 24


def _mid(m: dict) -> float | None:
    try:
        b, a = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
    except (KeyError, TypeError, ValueError):
        return None
    return (a + b) / 2 if a > b else None


def pick_universe(events: list[dict], n: int) -> list[dict]:
    rows = []
    for e in events:
        for m in e.get("markets") or []:
            mid = _mid(m)
            if m.get("status") != "active" or mid is None or not 0.05 <= mid <= 0.95:
                continue
            rows.append({"ticker": m["ticker"], "series": e["series_ticker"], "event": e["event_ticker"],
                         "strike_type": m.get("strike_type"), "mutually_exclusive": e.get("mutually_exclusive"),
                         "title": e.get("title", ""), "label": m.get("yes_sub_title", ""),
                         "volume_24h": float(m.get("volume_24h_fp") or 0)})
    rows.sort(key=lambda r: -r["volume_24h"])
    return rows[:n]


def _mean_se(xs: list[float]) -> tuple[float | None, float | None]:
    if not xs:
        return None, None
    m = statistics.fmean(xs)
    se = statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else None
    return m, se


def markout_table(data: dict, end_ts: float) -> list[dict]:
    """Average taker markout (cents) by trade size bucket and horizon."""
    rows = []
    for lo, hi, label in SIZE_BUCKETS:
        row = {"bucket": label}
        for h in HORIZONS_H:
            xs, ws = [], []
            for d in data.values():
                for t in d["trades"]:
                    if not lo <= t.size < hi or t.ts + h * 3600 > end_ts:
                        continue
                    later = d["mids"].at(t.ts + h * 3600)
                    if later is None:
                        continue
                    xs.append(t.sign * (later - t.price) * 100)
                    ws.append(t.size)
            m, se = _mean_se(xs)
            row[f"{h}h"] = None if m is None else round(m, 2)
            row[f"{h}h_se"] = None if se is None else round(se, 2)
            # contract-weighted: where the money actually went
            row[f"{h}h_w"] = round(sum(x * w for x, w in zip(xs, ws)) / sum(ws), 2) if ws else None
            row["n"] = len(xs)
        row["markets"] = sum(1 for d in data.values() if any(lo <= t.size < hi for t in d["trades"]))
        rows.append(row)
    return rows


def find_moves(data: dict, universe: list[dict], end_ts: float, window_h: int = MOVE_WINDOW_H) -> list[dict]:
    by_event: dict[str, list[str]] = {}
    for u in universe:
        by_event.setdefault(u["event"], []).append(u["ticker"])
    meta = {u["ticker"]: u for u in universe}
    moves = []
    for tk, d in data.items():
        mids, trades = d["mids"], d["trades"]
        if len(mids) < 48 or not trades:
            continue
        big = big_trade_threshold(trades)
        last_event = -math.inf
        for t1 in mids.ts:
            t0 = t1 - window_h * 3600
            if t1 - last_event < max(12, window_h) * 3600 or t1 + FORWARD_H * 3600 > end_ts or t0 < mids.ts[0] + 3 * 86400:
                continue
            a, b = mids.at(t0), mids.at(t1)
            if a is None or b is None or abs(b - a) < MOVE_MIN or not 0.03 < b < 0.97:
                continue
            direction = 1 if b > a else -1
            f = move_features(trades, mids, t0, t1, direction, big)
            # Sibling strikes of a ladder should move the same way if this is real news.
            sibs = [s for s in by_event.get(meta[tk]["event"], []) if s != tk and s in data]
            if sibs and not meta[tk]["mutually_exclusive"]:
                def moved(s):
                    a0, a1 = data[s]["mids"].at(t0), data[s]["mids"].at(t1)
                    return a0 is not None and a1 is not None and (a1 - a0) * direction >= 0.01
                agree = sum(1 for s in sibs if moved(s))
                f["sibling_confirmation"] = round(agree / len(sibs), 3)
            else:
                f["sibling_confirmation"] = None
            fut = mids.at(t1 + FORWARD_H * 3600)
            f.update(ticker=tk, t=t1, direction=direction,
                     continuation_24h=round((fut - b) * direction * 100, 2),
                     continuation_6h=round(((mids.at(t1 + 6 * 3600) or b) - b) * direction * 100, 2))
            moves.append(f)
            last_event = t1
    return moves


def flow_signal_table(data: dict, end_ts: float, lookback_h: int = 24, step_h: int = 6) -> list[dict]:
    """Does net taker flow predict the next 24h? Split big vs small trades.

    Every `step_h` hours, sum signed contracts from big trades (market's own
    top decile, >= 25) and from small trades over the past `lookback_h`, then
    measure the mid change over the next FORWARD_H hours in the direction of
    that flow."""
    rows: dict[str, list[tuple[float, float]]] = {"big": [], "small": []}
    for d in data.values():
        mids, trades = d["mids"], d["trades"]
        if len(mids) < 48 or not trades:
            continue
        big = big_trade_threshold(trades)
        t = trades[0].ts + lookback_h * 3600
        while t + FORWARD_H * 3600 <= end_ts:
            now_mid, fut = mids.at(t), mids.at(t + FORWARD_H * 3600)
            if now_mid is not None and fut is not None and 0.03 < now_mid < 0.97:
                w = [x for x in trades if t - lookback_h * 3600 < x.ts <= t]
                for kind, sel in (("big", [x for x in w if x.size >= big]), ("small", [x for x in w if x.size < big])):
                    net = sum(x.sign * x.notional for x in sel)
                    if abs(net) >= 1:
                        rows[kind].append((abs(net), (fut - now_mid) * (1 if net > 0 else -1) * 100))
            t += step_h * 3600
    out = []
    for kind, pts in rows.items():
        if len(pts) < 15:
            continue
        pts.sort()
        k = len(pts) // 3
        for name, part in (("smallest third", pts[:k]), ("middle third", pts[k:2 * k]), ("largest third", pts[2 * k:])):
            vals = [p[1] for p in part]
            mean, se = _mean_se(vals)
            out.append({"flow": kind, "net_notional": name, "range_usd": [round(part[0][0]), round(part[-1][0])],
                        "n": len(vals), "mean_fwd_24h": round(mean, 2), "se": round(se, 2) if se else None,
                        "hit_pct": round(100 * sum(v > 0 for v in vals) / len(vals), 1)})
    return out


FEATURES = ("flow_imbalance", "big_share", "big_with_share", "volume_ratio", "n_trades",
            "retention", "spread", "sibling_confirmation")


def tercile_table(moves: list[dict]) -> list[dict]:
    """Continuation by feature tercile: does a high value predict follow-through?"""
    out = []
    for feat in FEATURES:
        rows = [m for m in moves if m.get(feat) is not None]
        if len(rows) < 15:
            continue
        rows.sort(key=lambda m: m[feat])
        k = len(rows) // 3
        parts = {"low": rows[:k], "mid": rows[k:2 * k], "high": rows[2 * k:]}
        entry = {"feature": feat, "n": len(rows)}
        for name, part in parts.items():
            cont = [m["continuation_24h"] for m in part]
            mean, se = _mean_se(cont)
            entry[name] = {"range": [part[0][feat], part[-1][feat]], "mean_cont_24h": round(mean, 2),
                           "se": round(se, 2) if se else None,
                           "held_pct": round(100 * sum(c >= 0 for c in cont) / len(cont), 1)}
        entry["high_minus_low"] = round(entry["high"]["mean_cont_24h"] - entry["low"]["mean_cont_24h"], 2)
        out.append(entry)
    return sorted(out, key=lambda e: -abs(e["high_minus_low"]))


def fit_score_model(moves: list[dict]) -> dict:
    """Score weights = each feature's high-minus-low tercile gap in 24h
    continuation (sign says which end is "informed"); reference quantiles let
    the live page turn a raw feature into a percentile."""
    model = {}
    for t in tercile_table(moves):
        f = t["feature"]
        if f not in SCORE_FEATURES or abs(t["high_minus_low"]) < 0.5:
            continue
        vals = sorted(m[f] for m in moves if m.get(f) is not None)
        qs = [vals[min(int(q / 20 * len(vals)), len(vals) - 1)] for q in range(1, 20)]
        model[f] = {"weight": t["high_minus_low"], "quantiles": qs}
    return model


def validate_score(model: dict, train: list[dict], test: list[dict]) -> dict:
    from .signals import informed_score
    out = {}
    for name, part in (("train", train), ("test", test)):
        scored = sorted(((informed_score(m, model)["score"], m["continuation_24h"]) for m in part), key=lambda x: x[0])
        if len(scored) < 9:
            continue
        k = len(scored) // 3
        out[name] = {}
        for label, chunk in (("low", scored[:k]), ("mid", scored[k:2 * k]), ("high", scored[2 * k:])):
            cont = [c for _, c in chunk]
            mean, se = _mean_se(cont)
            out[name][label] = {"score_range": [round(chunk[0][0]), round(chunk[-1][0])], "n": len(cont),
                                "mean_cont_24h": round(mean, 2), "se": round(se, 2) if se else None,
                                "held_pct": round(100 * sum(c >= 0 for c in cont) / len(cont), 1)}
    return out


SCORE_FEATURES = ("flow_imbalance", "big_share", "volume_ratio", "n_trades", "spread")


def run(n_markets: int = 60, days: int = 14, out_dir: str = "research", workers: int = 6,
        client: KalshiClient | None = None, cache: str | None = None) -> dict:
    client = client or KalshiClient(rate_per_sec=8)
    if cache and Path(cache).exists():
        with open(cache, "rb") as fh:
            end, universe, data = pickle.load(fh)
        print(f"loaded cached data from {cache}", flush=True)
        return analyze(end, universe, data, days, out_dir)
    end = int(time.time())
    start_trades = end - days * 86400
    start_candles = start_trades - 7 * 86400  # baseline volume before the first move
    print("fetching open economics events…", flush=True)
    _, events, _ = fetch_open_events(client, workers=10)
    universe = pick_universe(events, n_markets)
    print(f"universe: {len(universe)} most active markets; fetching {days}d of trades + hourly candles…", flush=True)

    def load(u):
        candles = client.candlesticks(u["series"], u["ticker"], start_candles, end, 60)
        trades = parse_trades(client.trades(u["ticker"], min_ts=start_trades, max_ts=end, max_pages=15))
        return u["ticker"], {"mids": MidSeries(candles), "trades": trades}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        data = dict(pool.map(load, universe))
    if cache:
        with open(cache, "wb") as fh:
            pickle.dump((end, universe, data), fh)
    return analyze(end, universe, data, days, out_dir)


def analyze(end: float, universe: list[dict], data: dict, days: int, out_dir: str) -> dict:
    n_trades = sum(len(d["trades"]) for d in data.values())
    print(f"loaded {n_trades:,} trades", flush=True)

    markouts = markout_table(data, end)
    moves = find_moves(data, universe, end, MOVE_WINDOW_H)
    terciles = tercile_table(moves)
    # The live "What moved" view compares against 24h ago, so the score is fit on
    # 24h moves: first half of the period to fit, second half to test.
    moves24 = sorted(find_moves(data, universe, end, 24), key=lambda m: m["t"])
    half = len(moves24) // 2
    model = fit_score_model(moves24[:half])
    validation = validate_score(model, moves24[:half], moves24[half:])
    model_full = fit_score_model(moves24)
    flow_signal = flow_signal_table(data, end)
    cont = [m["continuation_24h"] for m in moves]
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {"markets": len(universe), "days": days, "move_window_h": MOVE_WINDOW_H,
                   "move_min_cents": MOVE_MIN * 100, "forward_h": FORWARD_H},
        "trades": n_trades,
        "markouts_cents": markouts,
        "moves": {"n": len(moves), "mean_cont_24h": round(statistics.fmean(cont), 2) if cont else None,
                  "held_pct": round(100 * sum(c >= 0 for c in cont) / len(cont), 1) if cont else None},
        "feature_terciles": terciles,
        "moves_24h": {"n": len(moves24), "feature_terciles": tercile_table(moves24)},
        "score_validation": validation,
        "score_model": model_full,
        "flow_signal": flow_signal,
        "universe": [{k: u[k] for k in ("ticker", "title", "label", "volume_24h")} for u in universe],
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "informed_flow.json").write_text(json.dumps(result, indent=1))
    (out / "informed_flow.md").write_text(render_markdown(result))
    model_path = Path(__file__).parent / "informed_model.json"
    model_path.write_text(json.dumps({"generated_at": result["generated_at"], "trades": n_trades,
                                      "moves": len(moves24), "features": model_full,
                                      "validation": validation, "markouts_cents": markouts}, indent=1))
    print(render_markdown(result))
    return result


def render_markdown(r: dict) -> str:
    p = r["params"]
    lines = [
        "# Informed-flow study (Kalshi economics markets)",
        "",
        f"Generated {r['generated_at'][:16]} UTC from {r['trades']:,} trades in the {p['markets']} most active "
        f"economics markets over {p['days']} days.",
        "",
        "## 1. Do large trades win? Taker markouts by trade size",
        "",
        "Signed change in mid price after each trade, from the taker's side, in cents. "
        "Includes the half-spread the taker paid, so small negative values are normal for uninformed flow.",
        "",
        "| Trade size (contracts) | Trades | Markets | +1h | +6h | +24h | +24h, contract-weighted |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in r["markouts_cents"]:
        def cell(h):
            v, se = row.get(f"{h}h"), row.get(f"{h}h_se")
            return "—" if v is None else f"{v:+.2f}" + (f" ± {se:.2f}" if se else "")
        w = row.get("24h_w")
        lines.append(f"| {row['bucket']} | {row['n']:,} | {row.get('markets', '—')} | {cell(1)} | {cell(6)} | {cell(24)} | "
                     f"{'—' if w is None else f'{w:+.2f}'} |")
    mv = r["moves"]
    lines += [
        "",
        f"## 2. Which moves stick? {mv['n']} moves of ≥{p['move_min_cents']:.0f}¢ in {p['move_window_h']}h",
        "",
        f"On average a move continued {mv['mean_cont_24h']:+.2f}¢ over the next {p['forward_h']}h; "
        f"{mv['held_pct']}% held or extended." if mv["n"] else "No qualifying moves.",
        "",
        "Mean 24h continuation (cents, in the move's direction) by feature tercile, sorted by spread between high and low:",
        "",
        "| Feature | n | Low tercile | Mid | High tercile | High − low |",
        "|---|---|---|---|---|---|",
    ]
    for t in r["feature_terciles"]:
        def c(part):
            x = t[part]
            return f"{x['mean_cont_24h']:+.2f} ({x['held_pct']:.0f}% held)"
        lines.append(f"| {t['feature']} | {t['n']} | {c('low')} | {c('mid')} | {c('high')} | {t['high_minus_low']:+.2f} |")
    lines += [
        "",
        "## 3. Follow the flow? Net taker flow over 24h vs the next 24h",
        "",
        "Every 6h, net signed dollars from big trades (each market's top-decile size, ≥25 contracts) and from small "
        "trades over the past 24h, against the mid change over the next 24h in the flow's direction (cents).",
        "",
        "| Flow from | Net flow size | Range ($) | n | Next-24h move with flow | Hit rate |",
        "|---|---|---|---|---|---|",
    ]
    for f in r.get("flow_signal", []):
        se = f" ± {f['se']:.2f}" if f["se"] else ""
        lines.append(f"| {f['flow']} trades | {f['net_notional']} | {f['range_usd'][0]:,}–{f['range_usd'][1]:,} | "
                     f"{f['n']} | {f['mean_fwd_24h']:+.2f}{se} | {f['hit_pct']:.0f}% |")
    m24 = r.get("moves_24h") or {}
    if m24:
        lines += ["", f"## 4. Informed-move score, out of sample ({m24['n']} moves of ≥3¢ over 24h)", "",
                  "Weights come from each feature's tercile gap on the first half of the period; the score is then "
                  "tested on the second half, which it never saw.", "",
                  "| Sample | Score tercile | Score range | n | Next-24h continuation | Held or extended |",
                  "|---|---|---|---|---|---|"]
        for sample, parts in (r.get("score_validation") or {}).items():
            for label in ("low", "mid", "high"):
                x = parts[label]
                se = f" ± {x['se']:.2f}" if x["se"] else ""
                lines.append(f"| {sample} | {label} | {x['score_range'][0]}–{x['score_range'][1]} | {x['n']} | "
                             f"{x['mean_cont_24h']:+.2f}{se} | {x['held_pct']:.0f}% |")
        lines += ["", "24h-move feature terciles (all moves):", "",
                  "| Feature | n | Low | Mid | High | High − low |", "|---|---|---|---|---|---|"]
        for t in m24["feature_terciles"]:
            lines.append(f"| {t['feature']} | {t['n']} | {t['low']['mean_cont_24h']:+.2f} | {t['mid']['mean_cont_24h']:+.2f} | "
                         f"{t['high']['mean_cont_24h']:+.2f} | {t['high_minus_low']:+.2f} |")
    lines += ["", "Standard errors are large at these sample sizes. Treat differences under ~1¢ as noise.", ""]
    return "\n".join(lines)
