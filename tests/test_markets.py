import unittest
from datetime import datetime, timedelta, timezone

from kalshi_arb.markets import (build_instrument, build_markets, event_distribution, fetch_quotes, prob_above,
                                summarize_quote, trading_days)

UTC = timezone.utc
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)   # a Monday


def mk(ticker, strike_type, floor=None, cap=None, bid=0.0, ask=0.0, pbid=None, pask=None, vol=10):
    return {"ticker": ticker, "status": "active", "strike_type": strike_type, "floor_strike": floor, "cap_strike": cap,
            "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
            "previous_yes_bid_dollars": str(bid if pbid is None else pbid),
            "previous_yes_ask_dollars": str(ask if pask is None else pask),
            "volume_24h_fp": str(vol), "open_interest_fp": "100"}


def above_event(strikes_probs, when, ticker="KXWTI-X", shift=0.0):
    """A ladder of "above X" markets with a 2-cent spread; `shift` moves the 24h-ago probabilities."""
    ms = [mk(f"{ticker}-T{s}", "greater", s, bid=p - 0.01, ask=p + 0.01, pbid=p - 0.01 + shift, pask=p + 0.01 + shift)
          for s, p in strikes_probs]
    return {"event_ticker": ticker, "title": "t", "strike_date": when.isoformat().replace("+00:00", "Z"), "markets": ms}


def range_event(edges, probs, when, ticker="KXINX-X"):
    """Mutually exclusive buckets: below edges[0], between each pair, above edges[-1]."""
    ms = [mk(f"{ticker}-L", "less", cap=edges[0], bid=probs[0] - 0.01, ask=probs[0] + 0.01)]
    for i in range(len(edges) - 1):
        ms.append(mk(f"{ticker}-B{i}", "between", edges[i], edges[i + 1] - 0.01, bid=probs[i + 1] - 0.01, ask=probs[i + 1] + 0.01))
    ms.append(mk(f"{ticker}-G", "greater", edges[-1], bid=probs[-1] - 0.01, ask=probs[-1] + 0.01))
    return {"event_ticker": ticker, "title": "t", "strike_date": when.isoformat().replace("+00:00", "Z"), "markets": ms}


LADDER = [(80, 0.97), (82, 0.85), (84, 0.5), (86, 0.15), (88, 0.03)]   # a distribution centred on 84
CLOSE = NOW + timedelta(days=3)


class DistributionTests(unittest.TestCase):
    def test_above_ladder_is_read_directly(self):
        d = event_distribution(above_event(LADDER, CLOSE))
        self.assertAlmostEqual(d["median"], 84.0, places=1)
        self.assertLess(d["p10"], 82.0)
        self.assertGreater(d["p90"], 86.0)

    def test_range_set_gives_the_same_picture_as_the_ladder(self):
        # the same belief written as buckets: P(<80)=.03, 80-82=.12, 82-84=.35, 84-86=.35, 86-88=.12, >88=.03
        d = event_distribution(range_event([80, 82, 84, 86, 88], [0.03, 0.12, 0.35, 0.35, 0.12, 0.03], CLOSE))
        self.assertAlmostEqual(d["median"], 84.0, places=1)

    def test_wide_range_buckets_are_dropped(self):
        ev = range_event([80, 82, 84, 86, 88], [0.03, 0.12, 0.35, 0.35, 0.12, 0.03], CLOSE)
        for m in ev["markets"]:
            m["yes_bid_dollars"], m["yes_ask_dollars"] = "0.01", "0.60"   # nothing usable
        self.assertIsNone(event_distribution(ev))

    def test_24h_change_uses_previous_quotes(self):
        d = event_distribution(above_event(LADDER, CLOSE, shift=-0.05))   # yesterday's chance of finishing high was lower
        self.assertGreater(d["change_24h"]["median"], 0)

    def test_prob_above(self):
        d = event_distribution(above_event(LADDER, CLOSE))
        self.assertAlmostEqual(prob_above(d["buckets"], 84.0), 0.5, places=1)
        self.assertGreater(prob_above(d["buckets"], 81.0), prob_above(d["buckets"], 85.0))
        self.assertIsNone(prob_above([], 1.0))


class CalendarTests(unittest.TestCase):
    def test_weekends_do_not_count(self):
        sat = datetime(2026, 11, 28, 7, 0, tzinfo=UTC)
        mon_close = datetime(2026, 11, 30, 22, 0, tzinfo=UTC)
        self.assertAlmostEqual(trading_days(sat, mon_close), 22 / 24, places=4)
        self.assertAlmostEqual(trading_days(NOW, NOW + timedelta(days=7)), 5.0, places=4)


class QuoteTests(unittest.TestCase):
    def chart(self, price, at, bars):
        ts = [int((at - timedelta(hours=h)).timestamp()) for h in bars]
        return {"chart": {"result": [{"meta": {"regularMarketPrice": price, "regularMarketTime": int(at.timestamp())},
                                      "timestamp": ts, "indicators": {"quote": [{"close": [100.0 + i for i in range(len(ts))]}]}}]}}

    def test_move_over_the_24h_before_the_last_trade(self):
        at = NOW - timedelta(minutes=5)
        q = summarize_quote("^GSPC", self.chart(110.0, at, [30, 25, 24, 10, 1]), NOW)   # bars are 30h..1h old
        self.assertEqual(q["ref_price"], 102.0)        # the 24h-old bar, not the 25h one
        self.assertAlmostEqual(q["change_pct"], (110 / 102 - 1) * 100, places=2)
        self.assertFalse(q["stale"])

    def test_weekend_is_stale(self):
        sat = datetime(2026, 11, 28, 7, 0, tzinfo=UTC)
        fri_close = datetime(2026, 11, 27, 21, 0, tzinfo=UTC)
        q = summarize_quote("^GSPC", self.chart(100.0, fri_close, [30, 24, 1]), sat)
        self.assertTrue(q["stale"])
        self.assertEqual(q["stale_reason"], "weekend")

    def test_garbage_gives_none_and_errors_are_reported(self):
        self.assertIsNone(summarize_quote("X", {"chart": {"result": None}}, NOW))
        def down(sym):
            raise OSError("offline")
        quotes, errs = fetch_quotes(["A", "B"], NOW, down)
        self.assertEqual(quotes, {"A": None, "B": None})
        self.assertEqual(len(errs), 2)


class InstrumentTests(unittest.TestCase):
    cfg = {"key": "wti", "label": "WTI", "group": "Commodities", "symbol": "CL=F", "gauge": "^OVX", "gauge_label": "OVX",
           "decimals": 2, "unit": "$", "series": [("KXWTI", "daily"), ("KXWTIH", "hourly")]}

    def quotes(self, spot=84.0):
        q = lambda s, p: {"symbol": s, "price": p, "change_pct": 1.0, "stale": False}
        return {"CL=F": q("CL=F", spot), "^OVX": q("^OVX", 40.0)}

    def test_metrics_against_spot(self):
        out = build_instrument(self.cfg, {"KXWTI": [above_event(LADDER, CLOSE)]}, self.quotes(83.0), NOW)
        p = out["points"][0]
        self.assertAlmostEqual(p["spot_gap_pct"], (84 / 83 - 1) * 100, delta=0.3)
        self.assertGreater(p["prob_above_spot"], 0.5)
        self.assertGreater(p["sigma_pct"], 0)
        self.assertIsNotNone(p["ann_vol"])
        self.assertEqual(out["implied_vol"]["gauge"]["label"], "OVX")
        self.assertAlmostEqual(out["implied_vol"]["gap"], p["ann_vol"] - 40.0, places=1)

    def test_mismatched_spot_is_not_compared(self):
        out = build_instrument(self.cfg, {"KXWTI": [above_event(LADDER, CLOSE)]}, self.quotes(300.0), NOW)
        self.assertIsNone(out["points"][0]["spot_gap_pct"])
        self.assertIsNone(out["implied_vol"])

    def test_no_spot_still_shows_the_distribution(self):
        out = build_instrument(self.cfg, {"KXWTI": [above_event(LADDER, CLOSE)]}, {}, NOW)
        self.assertEqual(len(out["points"]), 1)
        self.assertIsNone(out["points"][0]["prob_above_spot"])

    def test_past_events_and_extra_hourly_ones_are_dropped(self):
        past = above_event(LADDER, NOW - timedelta(hours=1), "KXWTI-OLD")
        hours = [above_event(LADDER, NOW + timedelta(hours=h), f"KXWTIH-{h}") for h in range(1, 8)]
        out = build_instrument(self.cfg, {"KXWTI": [past], "KXWTIH": hours}, self.quotes(), NOW)
        self.assertEqual(len(out["points"]), 3)
        self.assertTrue(all(p["kind"] == "hourly" for p in out["points"]))

    def test_build_markets_survives_outages(self):
        class Down:
            def open_events_with_markets(self, s):
                raise OSError("kalshi down")
        def no_prices(sym):
            raise OSError("yahoo down")
        m = build_markets(Down(), NOW, fetch=no_prices)
        self.assertEqual(m["instruments"], [])
        self.assertTrue(all(e.startswith("markets:") for e in m["errors"]))


if __name__ == "__main__":
    unittest.main()
