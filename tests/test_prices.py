import unittest
from datetime import date, datetime, timedelta, timezone

from kalshi_arb.prices import (add_prices, market_gap_reason, nyse_holidays, refresh, same_company,
                               summarize_price)

UTC = timezone.utc


def bar(at, close, interpolated=False, session="reg"):
    return {"begins_at": at.isoformat(), "close_price": str(close), "interpolated": interpolated, "session": session}


def quote(price, at, ext=None, ext_at=None, prev=100.0, symbol="XYZ"):
    return {"symbol": symbol, "last_trade_price": str(price), "venue_last_trade_time": at.isoformat(),
            "last_extended_hours_trade_price": str(ext) if ext else None,
            "venue_last_non_reg_trade_time": ext_at.isoformat() if ext_at else None,
            "previous_close": str(prev), "trading_halted": False}


class CalendarTests(unittest.TestCase):
    def test_2026_holidays(self):
        h = nyse_holidays(2026)
        self.assertEqual(h[date(2026, 11, 26)], "Thanksgiving Day")
        self.assertEqual(h[date(2026, 4, 3)], "Good Friday")
        self.assertEqual(h[date(2026, 7, 3)], "Independence Day")   # July 4 is a Saturday
        self.assertEqual(h[date(2026, 12, 25)], "Christmas Day")
        self.assertEqual(h[date(2026, 5, 25)], "Memorial Day")

    def test_saturday_new_year_has_no_friday_closure(self):
        self.assertNotIn(date(2027, 12, 31), nyse_holidays(2027))

    def test_reasons(self):
        fri_close = datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
        self.assertEqual(market_gap_reason(fri_close, datetime(2026, 11, 22, 15, 0, tzinfo=UTC)), "weekend")
        wed_close = datetime(2026, 11, 25, 21, 0, tzinfo=UTC)
        self.assertEqual(market_gap_reason(wed_close, datetime(2026, 11, 27, 13, 0, tzinfo=UTC)),
                         "market holiday (Thanksgiving Day)")
        fri = datetime(2026, 11, 27, 21, 0, tzinfo=UTC)
        self.assertEqual(market_gap_reason(fri, datetime(2026, 11, 30, 12, 0, tzinfo=UTC)), "weekend")
        self.assertEqual(market_gap_reason(fri, datetime(2026, 11, 27, 23, 0, tzinfo=UTC)), "no recent trades")


class SummaryTests(unittest.TestCase):
    now = datetime(2026, 11, 23, 18, 0, tzinfo=UTC)   # a Monday, midday

    def test_move_since_24h_ago_includes_extended_hours(self):
        bars = [bar(self.now - timedelta(hours=24), 100.0, True, "post"),
                bar(self.now - timedelta(minutes=5), 110.0)]
        p = summarize_price("XYZ", "Xyz", quote(110.0, self.now - timedelta(minutes=1), prev=102.0), bars, self.now)
        self.assertEqual(p["change_pct"], 10.0)
        self.assertEqual(p["change_abs"], 10.0)
        self.assertAlmostEqual(p["day_change_pct"], 7.843, places=2)
        self.assertFalse(p["stale"])
        self.assertEqual(p["session"], "regular hours")

    def test_extended_hours_trade_beats_an_older_regular_trade(self):
        close = self.now - timedelta(hours=2)
        bars = [bar(self.now - timedelta(hours=24), 100.0), bar(close, 100.0)]
        p = summarize_price("XYZ", "Xyz", quote(100.0, close, ext=104.0, ext_at=self.now - timedelta(minutes=3)),
                            bars, self.now)
        self.assertEqual(p["price"], 104.0)
        self.assertEqual(p["change_pct"], 4.0)

    def test_weekend_is_flagged_stale(self):
        sun = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
        fri_close = datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
        bars = [bar(sun - timedelta(hours=24), 105.0, True, "post"), bar(sun - timedelta(minutes=5), 105.0, True, "post")]
        p = summarize_price("XYZ", "Xyz", quote(105.0, fri_close), bars, sun)
        self.assertTrue(p["stale"])
        self.assertEqual(p["stale_reason"], "weekend")
        self.assertEqual(p["change_pct"], 0.0)

    def test_short_history_gives_no_24h_move(self):
        bars = [bar(self.now - timedelta(hours=5), 100.0), bar(self.now - timedelta(minutes=5), 101.0)]
        p = summarize_price("XYZ", "Xyz", quote(101.0, self.now - timedelta(minutes=1)), bars, self.now)
        self.assertIsNone(p["change_pct"])

    def test_nothing_traded_gives_none(self):
        self.assertIsNone(summarize_price("XYZ", "Xyz", {"symbol": "XYZ"}, [], self.now))


class MatchTests(unittest.TestCase):
    def test_same_company(self):
        self.assertTrue(same_company("Ford Motor Company", "Ford Motor"))
        self.assertTrue(same_company("Victoria's Secret & Co.", "Victoria's Secret"))
        self.assertTrue(same_company("Amazon.com, Inc.", "Amazon"))
        self.assertFalse(same_company("Ford Motor Company", "First Advantage"))
        self.assertFalse(same_company("Air Canada", "Associated Capital Group"))
        self.assertFalse(same_company("American Airlines Group", "American Express"))


class FetchTests(unittest.TestCase):
    now = datetime(2026, 11, 23, 18, 0, tzinfo=UTC)

    def fake(self, path, params=None):
        syms = (params or {}).get("symbols", "").split(",")
        if path == "/quotes/":
            return {"results": [quote(50.0, self.now - timedelta(minutes=1), symbol=s) if s in ("F", "FA") else None
                                for s in syms]}
        if path == "/instruments/":
            return {"results": [{"symbol": params["symbol"], "simple_name": {"F": "Ford Motor", "FA": "First Advantage"}[params["symbol"]]}]}
        if path == "/marketdata/historicals/":
            return {"results": [{"symbol": s, "historicals": [bar(self.now - timedelta(hours=24), 40.0),
                                                              bar(self.now - timedelta(minutes=5), 50.0)]} for s in syms]}
        raise AssertionError(path)

    def item(self, series, company):
        return {"series_ticker": series, "company": company, "report": None}

    def test_wrong_company_behind_a_similar_ticker_is_skipped(self):
        items = [self.item("KXFA", "Ford Motor Company"), self.item("KXACA", "Air Canada")]
        errs, market = add_prices(items, self.now, get=self.fake)
        self.assertEqual(errs, [])
        self.assertEqual(items[0]["price"]["symbol"], "F")   # FA is First Advantage, rejected
        self.assertEqual(items[0]["price"]["change_pct"], 25.0)
        self.assertIsNone(items[1]["price"])

    def test_outage_is_reported_not_raised(self):
        def down(path, params=None):
            raise OSError("offline")
        items = [self.item("KXFA", "Ford Motor Company")]
        errs, market = add_prices(items, self.now, get=down)
        self.assertTrue(any("offline" in e for e in errs))
        self.assertIsNone(items[0]["price"])

    def test_refresh_summarizes(self):
        earn = {"items": [self.item("KXFA", "Ford Motor Company"), self.item("KXACA", "Air Canada")],
                "errors": ["prices: old", "other"]}
        refresh(earn, self.now, get=self.fake)
        self.assertEqual((earn["prices"]["priced"], earn["prices"]["unavailable"], earn["prices"]["stale"]), (1, 1, 0))
        self.assertEqual(earn["errors"], ["other"])


if __name__ == "__main__":
    unittest.main()
