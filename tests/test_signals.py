import unittest
from datetime import datetime, timezone

from kalshi_arb.flow import MidSeries, Trade, big_trade_threshold, move_features, parse_trades
from kalshi_arb.macro import _non_increasing, build_macro, decision_change, ladder_distribution
from kalshi_arb.signals import find_movers, informed_score

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def rung(ticker, strike, bid, ask, pbid=None, pask=None):
    return {"ticker": ticker, "status": "active", "strike_type": "greater", "floor_strike": strike,
            "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
            "previous_yes_bid_dollars": str(bid if pbid is None else pbid),
            "previous_yes_ask_dollars": str(ask if pask is None else pask),
            "close_time": "2026-12-10T00:00:00Z", "volume_24h_fp": "100"}


class LadderTests(unittest.TestCase):
    def test_monotone_fix(self):
        self.assertEqual(_non_increasing([0.9, 0.5, 0.6, 0.2]), [0.9, 0.55, 0.55, 0.2])

    def test_discrete_cpi_ladder(self):
        # P(>3.0)=0.9, P(>3.1)=0.5, P(>3.2)=0.1  -> 3.0:10%, 3.1:40%, 3.2:40%, 3.3:10%
        ms = [rung("a", 3.0, 0.89, 0.91), rung("b", 3.1, 0.49, 0.51), rung("c", 3.2, 0.09, 0.11)]
        d = ladder_distribution(ms, discrete=True)
        self.assertEqual([round(b["p"], 2) for b in d["buckets"]], [0.10, 0.40, 0.40, 0.10])
        self.assertEqual([round(b["value"], 1) for b in d["buckets"]], [3.0, 3.1, 3.2, 3.3])
        self.assertAlmostEqual(d["mean"], 3.15)
        self.assertEqual(d["median"], 3.1)
        self.assertEqual(d["p10"], 3.0)

    def test_wide_spreads_are_ignored(self):
        ms = [rung("a", 1, 0.95, 0.97), rung("b", 2, 0.60, 0.62), rung("c", 3, 0.0, 0.50), rung("d", 4, 0.05, 0.07)]
        d = ladder_distribution(ms)
        self.assertEqual(d["liquid_strikes"], 3)

    def test_decision_labels(self):
        self.assertEqual(decision_change("Hike 25bps"), 0.25)
        self.assertEqual(decision_change("Hike >25bps"), 0.50)
        self.assertEqual(decision_change("Cut 25bps"), -0.25)
        self.assertEqual(decision_change("Fed maintains rate"), 0.0)
        self.assertIsNone(decision_change("Something else"))


class FedPathTests(unittest.TestCase):
    def test_path_is_cumulative_expected_change(self):
        date1, date2 = "2026-12-09T19:00:00Z", "2027-01-27T19:00:00Z"

        def decision(et, date, hike, hold):
            return {"event_ticker": et, "series_ticker": "KXFEDDECISION", "strike_date": date, "mutually_exclusive": True,
                    "markets": [{"ticker": et + "-H", "status": "active", "yes_sub_title": "Hike 25bps",
                                 "yes_bid_dollars": str(hike - .005), "yes_ask_dollars": str(hike + .005),
                                 "previous_yes_bid_dollars": str(hike - .005), "previous_yes_ask_dollars": str(hike + .005)},
                                {"ticker": et + "-M", "status": "active", "yes_sub_title": "Fed maintains rate",
                                 "yes_bid_dollars": str(hold - .005), "yes_ask_dollars": str(hold + .005),
                                 "previous_yes_bid_dollars": str(hold - .005), "previous_yes_ask_dollars": str(hold + .005)}]}
        # Rate is 4.00 today; 80% hike in Dec -> ladder P(>4.00) = 0.8
        ladder = {"event_ticker": "KXFED-26DEC", "series_ticker": "KXFED", "strike_date": date1, "mutually_exclusive": False,
                  "markets": [rung(f"r{s}", s, p - .005, p + .005) for s, p in
                              ((3.5, 0.995), (3.75, 0.99), (4.0, 0.80), (4.25, 0.01), (4.5, 0.005))]}
        events = [decision("D1", date1, 0.8, 0.2), decision("D2", date2, 0.5, 0.5), ladder]
        fp = build_macro(events, NOW)["fed_path"]
        self.assertEqual(fp["current_upper_bound"], 4.0)
        exp = [m["expected"] for m in fp["meetings"]]
        self.assertAlmostEqual(exp[0], 4.20, places=2)
        self.assertAlmostEqual(exp[1], 4.325, places=2)


class FlowTests(unittest.TestCase):
    def candles(self, mids, start=0):
        return [{"end_period_ts": start + 3600 * (i + 1), "yes_bid": {"close_dollars": str(m - 0.005)},
                 "yes_ask": {"close_dollars": str(m + 0.005)}, "volume_fp": "10"} for i, m in enumerate(mids)]

    def test_parse_and_features(self):
        raw = [{"created_time": "1970-01-01T05:30:00Z", "yes_price_dollars": "0.55", "count_fp": "500", "taker_side": "yes"},
               {"created_time": "1970-01-01T05:40:00Z", "yes_price_dollars": "0.56", "count_fp": "5", "taker_side": "no"}]
        trades = parse_trades(raw)
        self.assertEqual([t.sign for t in trades], [1, -1])
        mids = MidSeries(self.candles([0.50] * 4 + [0.55, 0.60]))
        f = move_features(trades, mids, 4 * 3600, 6 * 3600, 1, big=100)
        self.assertEqual(f["n_trades"], 2)
        self.assertAlmostEqual(f["flow_imbalance"], (2 * 500 - 505) / 505, places=3)
        self.assertAlmostEqual(f["big_share"], 500 / 505, places=3)
        self.assertAlmostEqual(f["move"], 0.10, places=3)

    def test_big_threshold_has_floor(self):
        self.assertEqual(big_trade_threshold([Trade(0, 0.5, 2, 1)] * 50), 25.0)


class ScoreTests(unittest.TestCase):
    MODEL = {"features": {
        "spread": {"weight": -2.5, "quantiles": [0.01, 0.02, 0.03, 0.05, 0.08]},
        "volume_ratio": {"weight": 2.0, "quantiles": [0.5, 1, 2, 4, 8]},
    }}

    def test_informed_vs_noise(self):
        hi = informed_score({"spread": 0.01, "volume_ratio": 10}, self.MODEL)
        lo = informed_score({"spread": 0.12, "volume_ratio": 0.2}, self.MODEL)
        self.assertGreaterEqual(hi["score"], 60)
        self.assertEqual(hi["label"], "informed")
        self.assertLess(lo["score"], 40)
        self.assertEqual(lo["label"], "noise")

    def test_missing_features_are_skipped(self):
        self.assertEqual(len(informed_score({"spread": 0.01}, self.MODEL)["components"]), 1)


class MoverTests(unittest.TestCase):
    def test_one_mover_per_event_with_sibling_agreement(self):
        ms = [rung("a", 3.0, 0.70, 0.72, 0.40, 0.42), rung("b", 3.1, 0.50, 0.52, 0.30, 0.32),
              rung("c", 3.2, 0.20, 0.22, 0.20, 0.22)]
        ev = {"event_ticker": "E", "series_ticker": "S", "title": "CPI", "mutually_exclusive": False, "markets": ms}
        [m] = find_movers([ev])
        self.assertEqual(m["ticker"], "a")
        self.assertAlmostEqual(m["change"], 0.30)
        self.assertEqual((m["siblings_agree"], m["siblings"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
