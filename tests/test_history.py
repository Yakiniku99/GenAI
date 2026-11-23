import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from kalshi_arb.history import (MIN_CROSS, MIN_ITEM, analyze, daily_pairs, describe, empty, load, record,
                                spearman, strength, track)

UTC = timezone.utc
T0 = datetime(2026, 11, 2, 21, 0, tzinfo=UTC)   # a Monday after the close


def px(price, stale=False, excess=0.0, change=0.0):
    return {"price": price, "stale": stale, "excess_pct": excess, "change_pct": change}


def item(ticker, median=10.0, change=None, price=None, p10=8.0, p90=12.0):
    return {"event_ticker": ticker, "dist": {"median": median, "p10": p10, "p90": p90,
                                             "change_24h": {"median": change} if change is not None else None},
            "price": price}


def earn(items, market=True):
    return {"items": items, "prices": {"market": {"symbol": "SPY", "price": 500.0, "stale": False} if market else None}}


class StatsTests(unittest.TestCase):
    def test_spearman(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertIsNone(spearman([0, 0, 0, 0], [1, 2, 3, 4]))   # a KPI that never moved
        self.assertIsNone(spearman([1, 2], [1, 2]))

    def test_ties_are_ranked_together(self):
        self.assertAlmostEqual(spearman([0, 0, 1, 1], [1, 2, 3, 4]), 0.894, places=3)

    def test_strength_words(self):
        self.assertEqual([strength(r) for r in (0.1, 0.3, -0.5, 0.8)],
                         ["negligible", "weak positive", "moderate negative", "strong positive"])

    def test_needs_enough_observations(self):
        d = describe([1, 2, 3], [1, 2, 3], MIN_ITEM)
        self.assertIsNone(d["r"])
        self.assertEqual((d["n"], d["needed"]), (3, MIN_ITEM))

    def test_significance(self):
        xs = list(range(30))
        strong = describe(xs, xs, 10)
        self.assertTrue(strong["significant"])
        noisy = describe(xs, [(7 * v) % 11 for v in xs], 10)
        self.assertFalse(noisy["significant"])

    def test_flat_series_reports_flat_not_error(self):
        d = describe([0.0] * 12, list(range(12)), 10)
        self.assertTrue(d["flat"])
        self.assertIsNone(d["r"])


class HistoryTests(unittest.TestCase):
    def test_record_skips_close_scans_and_keeps_data(self):
        h = empty()
        e = earn([item("A", 10.0, price=px(100.0))])
        self.assertTrue(record(h, e, T0))
        self.assertFalse(record(h, e, T0 + timedelta(minutes=5)))
        self.assertTrue(record(h, e, T0 + timedelta(minutes=30)))
        self.assertEqual(h["scans"][0]["items"]["A"], [10.0, 4.0, 100.0, False])
        self.assertEqual(h["scans"][0]["mkt"], [500.0, False])

    def test_old_scans_thin_to_one_per_day(self):
        h = empty()
        e = earn([item("A", price=px(100.0))])
        for n in range(4):   # four scans on one day, 20 days ago
            record(h, e, T0 + timedelta(minutes=30 * n))
        later = T0 + timedelta(days=20)
        record(h, e, later)
        old = [s for s in h["scans"] if s["t"] < int((later - timedelta(days=7)).timestamp())]
        self.assertEqual(len(old), 1)
        self.assertEqual(len(h["scans"]), 2)

    def test_bad_file_gives_empty_history(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "h.json")
            with open(p, "w") as f:
                f.write("<html>404</html>")
            self.assertEqual(load(p)["scans"], [])
            self.assertEqual(load(os.path.join(d, "missing.json"))["scans"], [])

    def test_track_saves_and_continues(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "h.json")
            e = earn([item("A", change=0.5, price=px(100.0, excess=1.0))])
            track(e, p, T0)
            track(earn([item("A", change=0.5, price=px(101.0, excess=1.0))]), p, T0 + timedelta(hours=1))
            self.assertEqual(len(load(p)["scans"]), 2)

    def test_track_survives_unwritable_path(self):
        e = earn([item("A", price=px(100.0))])
        track(e, "/nonexistent-dir/h.json", T0)
        self.assertIn("correlation", e)
        self.assertTrue(any(x.startswith("history:") for x in e["errors"]))


def scan_on(day, items, mkt):
    return {"t": int(day.timestamp()), "mkt": [mkt, False], "items": items}


class AnalysisTests(unittest.TestCase):
    def test_daily_pairs_measure_excess_over_the_market(self):
        h = empty()
        d1, d2 = T0, T0 + timedelta(days=1)
        h["scans"] = [scan_on(d1, {"A": [10.0, 4.0, 100.0, False]}, 500.0),
                      scan_on(d2, {"A": [11.0, 4.0, 106.0, False]}, 505.0)]   # stock +6%, market +1%
        (dx, scale, excess), = daily_pairs(h)["A"]
        self.assertEqual((dx, scale), (1.0, 4.0))
        self.assertAlmostEqual(excess, 5.0, places=6)

    def test_stale_days_and_closed_market_are_skipped(self):
        h = empty()
        h["scans"] = [scan_on(T0, {"A": [10.0, 4.0, 100.0, True]}, 500.0),
                      scan_on(T0 + timedelta(days=1), {"A": [11.0, 4.0, 106.0, False]}, 505.0)]
        self.assertEqual(daily_pairs(h), {})
        h["scans"][1]["mkt"] = [505.0, True]
        self.assertEqual(daily_pairs(h), {})

    def test_gaps_longer_than_a_long_weekend_are_not_a_day(self):
        h = empty()
        h["scans"] = [scan_on(T0, {"A": [10.0, 4.0, 100.0, False]}, 500.0),
                      scan_on(T0 + timedelta(days=9), {"A": [11.0, 4.0, 106.0, False]}, 505.0)]
        self.assertEqual(daily_pairs(h), {})

    def test_cross_section_needs_enough_companies_and_finds_the_link(self):
        few = earn([item(f"T{i}", change=float(i), price=px(100.0, excess=float(i))) for i in range(MIN_CROSS - 1)])
        analyze(few, empty())
        self.assertIsNone(few["correlation"]["cross_section"]["r"])
        enough = earn([item(f"T{i}", change=float(i), price=px(100.0, excess=float(i))) for i in range(MIN_CROSS + 10)])
        analyze(enough, empty())
        cs = enough["correlation"]["cross_section"]
        self.assertAlmostEqual(cs["r"], 1.0)
        self.assertTrue(cs["significant"])
        self.assertEqual(enough["correlation"]["adjusted_for"], "SPY")

    def test_stale_prices_stay_out_of_the_cross_section(self):
        e = earn([item(f"T{i}", change=float(i), price=px(100.0, stale=True, excess=float(i))) for i in range(12)])
        analyze(e, empty())
        self.assertEqual(e["correlation"]["cross_section"]["n"], 0)

    def test_per_item_correlation_from_history(self):
        h = empty()
        scans = []
        for n in range(MIN_ITEM + 2):   # KPI up on days the stock beats the market
            kpi = 10.0 + n + (n % 3)
            stock = 100.0 * (1 + 0.01 * (n + (n % 3)))
            scans.append(scan_on(T0 + timedelta(days=n), {"A": [kpi, 4.0, stock, False]}, 500.0))
        h["scans"] = scans
        e = earn([item("A", price=px(100.0))])
        analyze(e, h)
        c = e["items"][0]["corr"]
        self.assertGreaterEqual(c["n"], MIN_ITEM)
        self.assertIsNotNone(c["r"])
        self.assertEqual(e["correlation"]["days"], MIN_ITEM + 2)


if __name__ == "__main__":
    unittest.main()
