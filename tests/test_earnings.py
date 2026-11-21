import unittest
from datetime import datetime, timezone

from kalshi_arb.earnings import (MIN_OPEN_INTEREST, build_earnings, expected_month, match_report,
                                 summarize_event, symbol_candidates, tier_of)

NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)


def rung(strike, bid, ask, oi=1000, label="Above"):
    return {"ticker": f"T-{strike}", "status": "active", "strike_type": "greater", "floor_strike": strike,
            "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
            "previous_yes_bid_dollars": str(bid), "previous_yes_ask_dollars": str(ask),
            "open_interest_fp": str(oi), "volume_24h_fp": "10",
            "yes_sub_title": f"{label} {strike}%", "expected_expiration_time": "2027-03-01T00:00:00Z",
            "title": f"Will Walmart Inc. report Above {strike}% comparable sales in Q1 2028?"}


def event(ticker="KXWMT-27MAYCOMP", series="KXWMT", markets=None, title="Walmart comparable sales in Q1"):
    markets = markets if markets is not None else [
        rung(1.0, 0.89, 0.91), rung(2.0, 0.69, 0.71), rung(3.0, 0.49, 0.51),
        rung(4.0, 0.29, 0.31), rung(5.0, 0.09, 0.11)]
    return {"event_ticker": ticker, "series_ticker": series, "title": title,
            "sub_title": "comparable sales in Q1 2028", "markets": markets}


class SummaryTests(unittest.TestCase):
    def test_liquid_ladder_is_summarized(self):
        it = summarize_event(event())
        self.assertEqual(it["company"], "Walmart Inc.")
        self.assertEqual(it["period"], "Q1 2028")
        self.assertEqual(it["metric"], "Walmart comparable sales")
        self.assertEqual(it["unit"], "%")
        self.assertEqual(it["open_interest"], 5000)
        self.assertEqual(it["tier"], "high")
        self.assertEqual(it["strikes_used"], 5)
        self.assertAlmostEqual(it["dist"]["median"], 3.0, places=1)

    def test_thin_open_interest_is_dropped(self):
        ms = [rung(s, b, a, oi=MIN_OPEN_INTEREST / 10) for s, b, a in
              [(1.0, 0.89, 0.91), (2.0, 0.69, 0.71), (3.0, 0.49, 0.51), (4.0, 0.29, 0.31), (5.0, 0.09, 0.11)]]
        self.assertIsNone(summarize_event(event(markets=ms)))

    def test_wide_spreads_do_not_count_as_strikes(self):
        # Two strikes quote 40c wide, leaving 3 usable: below the 4-strike minimum.
        ms = [rung(1.0, 0.89, 0.91), rung(2.0, 0.50, 0.90), rung(3.0, 0.49, 0.51),
              rung(4.0, 0.10, 0.50), rung(5.0, 0.09, 0.11)]
        self.assertIsNone(summarize_event(event(markets=ms)))

    def test_not_a_reported_figure_is_dropped(self):
        ms = event()["markets"]
        for m in ms:
            m["title"] = "How much will deliveries grow this year?"
        self.assertIsNone(summarize_event(event(markets=ms)))

    def test_counts_are_not_negative(self):
        ms = [rung(s, b, a) for s, b, a in [(10, 0.97, 0.99), (20, 0.90, 0.92), (30, 0.50, 0.52),
                                            (40, 0.10, 0.12)]]
        for m in ms:
            m["yes_sub_title"] = f"Above {m['floor_strike']}"
        it = summarize_event(event(markets=ms))
        self.assertGreaterEqual(it["dist"]["p10"], 0)

    def test_tiers(self):
        self.assertEqual([tier_of(n) for n in (100, 1500, 5000)], ["low", "medium", "high"])


class CalendarTests(unittest.TestCase):
    def test_symbol_candidates(self):
        self.assertEqual(symbol_candidates("KXWMT"), ["WMT", "WM"])
        self.assertEqual(symbol_candidates("KXWMTA"), ["WMTA", "WMT"])

    def test_expected_month(self):
        self.assertEqual(expected_month("KXWMT-27MAYCOMP", NOW), "2027-05")
        self.assertEqual(expected_month("KXTSLAA-28JANDEL", NOW), None)   # generic January, 14 months out
        self.assertEqual(expected_month("KXTESLADELIVERYBY-27", NOW), None)

    def test_match_report_takes_the_entry_near_the_ticker_month(self):
        it = summarize_event(event())
        rows = {"WMT": [{"date": "2027-02-19", "symbol": "WMT", "hour": ""},
                        {"date": "2027-05-14", "symbol": "WMT", "hour": "bmo"}]}
        r = match_report(it, rows)
        self.assertEqual((r["date"], r["hour"], r["symbol"]), ("2027-05-14", "bmo", "WMT"))

    def test_match_report_ignores_a_report_for_another_quarter(self):
        it = summarize_event(event())   # May 2027
        self.assertIsNone(match_report(it, {"WMT": [{"date": "2027-02-19"}]}))
        self.assertIsNone(match_report(it, {}))


class BuildTests(unittest.TestCase):
    def test_keeps_most_liquid_and_orders_by_date(self):
        big = event("KXWMT-27MAYCOMP", "KXWMT")
        small = event("KXTGT-27MAYCOMP", "KXTGT", [rung(s, b, a, oi=50) for s, b, a in
                                                   [(1.0, 0.89, 0.91), (2.0, 0.69, 0.71), (3.0, 0.49, 0.51),
                                                    (4.0, 0.29, 0.31), (5.0, 0.09, 0.11)]])
        out = build_earnings([small, big], NOW, fetch_calendar=lambda start, end: ([], 0))
        self.assertEqual(out["ladders_found"], 1)
        self.assertEqual([i["event_ticker"] for i in out["items"]], ["KXWMT-27MAYCOMP"])
        self.assertEqual(out["items"][0]["expected_month"], "2027-05")

    def test_calendar_dates_are_attached(self):
        rows = [{"symbol": "WMT", "date": "2027-05-14", "hour": "bmo"}]
        out = build_earnings([event()], NOW, fetch_calendar=lambda start, end: (rows, 0))
        self.assertEqual(out["calendar"]["matched"], 1)
        self.assertEqual(out["items"][0]["report"]["date"], "2027-05-14")

    def test_calendar_failure_is_reported_not_raised(self):
        def boom(start, end):
            raise OSError("offline")
        out = build_earnings([event()], NOW, fetch_calendar=boom)
        self.assertEqual(out["calendar"]["entries"], 0)
        self.assertEqual(len(out["items"]), 1)
        self.assertTrue(any("offline" in e for e in out["errors"]))

    def test_failed_days_are_counted(self):
        out = build_earnings([event()], NOW, fetch_calendar=lambda start, end: ([], 3))
        self.assertEqual(out["calendar"]["failed_days"], 3)


if __name__ == "__main__":
    unittest.main()
