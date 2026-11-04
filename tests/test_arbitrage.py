import unittest
from datetime import datetime, timezone

from kalshi_arb.arbitrage import (FEE_MODELS, Quote, analyze_event, analyze_events, robinhood_fee,
                                  strike_coverage_is_exhaustive, taker_fee)

KALSHI = FEE_MODELS["kalshi"].for_series()

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def mkt(ticker, bid, ask, bid_size=100, ask_size=100, strike_type=None, floor=None, cap=None, status="active"):
    return {
        "ticker": ticker, "yes_sub_title": ticker, "status": status,
        "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
        "yes_bid_size_fp": str(bid_size), "yes_ask_size_fp": str(ask_size),
        "strike_type": strike_type, "floor_strike": floor, "cap_strike": cap,
        "expected_expiration_time": "2026-12-01T00:00:00Z",
    }


def event(markets, me=True):
    return {"event_ticker": "EV", "series_ticker": "SER", "title": "t", "mutually_exclusive": me, "markets": markets}


class FeeTests(unittest.TestCase):
    def test_fee_rounds_up_to_cent(self):
        self.assertEqual(taker_fee(0.50, 1), 0.02)     # 0.0175 -> 0.02
        self.assertEqual(taker_fee(0.50, 100), 1.75)
        self.assertEqual(taker_fee(0.10, 10), 0.07)    # exactly 0.063 -> 0.07
        self.assertEqual(taker_fee(0.50, 100, 0), 0.0)


class RobinhoodFeeTests(unittest.TestCase):
    """Figures from Robinhood's event-contract fee table (100 contracts)."""

    def test_commission_table_plus_exchange_fee(self):
        # fee = commission + $0.01/contract exchange fee ($1.00 on 100)
        cases = [  # price, gold commission, non-gold commission
            (0.01, 0.05, 0.10), (0.05, 0.24, 0.48), (0.10, 0.45, 0.90),
            (0.15, 0.64, 1.00), (0.50, 1.00, 1.00), (0.90, 0.45, 0.90), (0.99, 0.05, 0.10),
        ]
        for p, gold, std in cases:
            with self.subTest(price=p):
                self.assertAlmostEqual(robinhood_fee(p, 100, gold=True), gold + 1.00)
                self.assertAlmostEqual(robinhood_fee(p, 100, gold=False), std + 1.00)

    def test_worked_example(self):
        # 100 YES at $0.90 with Gold costs $91.45 all-in.
        self.assertAlmostEqual(90 + robinhood_fee(0.90, 100, gold=True), 91.45)

    def test_single_contract_rounds_up(self):
        self.assertAlmostEqual(robinhood_fee(0.50, 1), 0.02)  # 1c commission + 1c exchange

    def test_default_model_is_robinhood_and_ignores_kalshi_multiplier(self):
        ev = event([mkt("A", 0.40, 0.45), mkt("B", 0.40, 0.45), mkt("C", 0.40, 0.45)])
        rh = [o for o in analyze_events([ev], {"SER": 0.0}, NOW) if o.strategy == "me_all_no"][0]
        kx = [o for o in analyze_events([ev], {"SER": 0.0}, NOW, "kalshi") if o.strategy == "me_all_no"][0]
        self.assertAlmostEqual(kx.fees, 0.0)       # fee-free series on Kalshi direct
        self.assertAlmostEqual(rh.fees, 3 * 2.00)  # 3 legs x (1.00 commission + 1.00 exchange)


class BucketTests(unittest.TestCase):
    def test_all_yes_arbitrage_on_exhaustive_ladder(self):
        ev = event([
            mkt("LOW", 0.28, 0.30, strike_type="less", cap=2.0),
            mkt("MID", 0.28, 0.30, strike_type="between", floor=2.0, cap=2.4),
            mkt("HIGH", 0.28, 0.30, strike_type="greater", floor=2.4),
        ])
        opps = {o.strategy: o for o in analyze_event(ev, KALSHI, now=NOW)}
        yes = opps["me_all_yes"]
        self.assertAlmostEqual(yes.cost, 0.90)
        self.assertEqual(yes.max_baskets, 100)
        self.assertEqual(yes.flags, [])
        # 3 legs x ceil(0.07*100*0.3*0.7 = 1.47) = 4.41 in fees on $10 gross
        self.assertAlmostEqual(yes.fees, 4.41)
        self.assertAlmostEqual(yes.net_profit, 10 - 4.41)
        self.assertTrue(yes.is_arb)
        self.assertFalse(opps["me_all_no"].is_arb)

    def test_all_yes_flags_non_exhaustive_sets(self):
        ev = event([mkt("A", 0.1, 0.2), mkt("B", 0.1, 0.2)])
        yes = [o for o in analyze_event(ev, now=NOW) if o.strategy == "me_all_yes"][0]
        self.assertIn("verify_exhaustive", yes.flags)

    def test_all_no_arbitrage_when_bids_overround(self):
        # YES bids sum to 1.20, so NO asks sum to 1.80 for a $2 minimum payout.
        ev = event([mkt("A", 0.40, 0.45), mkt("B", 0.40, 0.45), mkt("C", 0.40, 0.45)])
        no = [o for o in analyze_event(ev, now=NOW) if o.strategy == "me_all_no"][0]
        self.assertEqual(no.guaranteed_payout, 2.0)
        self.assertAlmostEqual(no.cost, 1.80)
        self.assertTrue(no.is_arb)
        self.assertEqual(no.flags, [])

    def test_depth_limits_size(self):
        ev = event([mkt("A", 0.40, 0.45, bid_size=3), mkt("B", 0.40, 0.45), mkt("C", 0.40, 0.45)])
        no = [o for o in analyze_event(ev, now=NOW) if o.strategy == "me_all_no"][0]
        self.assertEqual(no.max_baskets, 3)

    def test_missing_ask_skips_all_yes(self):
        ev = event([mkt("A", 0.1, 0.2), mkt("B", 0.1, 1.0, ask_size=0)])
        self.assertNotIn("me_all_yes", [o.strategy for o in analyze_event(ev, now=NOW)])


class LadderTests(unittest.TestCase):
    def test_greater_ladder_inversion(self):
        # "above 3.25" bids 0.60 while "above 3.00" asks 0.50 -> 10c gross edge.
        ev = event([
            mkt("T3.00", 0.48, 0.50, strike_type="greater", floor=3.0),
            mkt("T3.25", 0.60, 0.62, strike_type="greater", floor=3.25),
        ], me=False)
        [opp] = analyze_event(ev, now=NOW)
        self.assertEqual([(l.ticker, l.side) for l in opp.legs], [("T3.00", "yes"), ("T3.25", "no")])
        self.assertAlmostEqual(opp.gross_edge, 0.10)
        self.assertTrue(opp.is_arb)

    def test_less_ladder_inversion(self):
        # "below 2.0" bids 0.60 while "below 2.5" asks 0.50.
        ev = event([
            mkt("B2.0", 0.60, 0.62, strike_type="less", cap=2.0),
            mkt("B2.5", 0.48, 0.50, strike_type="less", cap=2.5),
        ], me=False)
        [opp] = analyze_event(ev, now=NOW)
        self.assertEqual([(l.ticker, l.side) for l in opp.legs], [("B2.5", "yes"), ("B2.0", "no")])
        self.assertTrue(opp.is_arb)

    def test_consistent_ladder_is_not_arb(self):
        ev = event([
            mkt("T3.00", 0.70, 0.72, strike_type="greater", floor=3.0),
            mkt("T3.25", 0.40, 0.42, strike_type="greater", floor=3.25),
        ], me=False)
        [opp] = analyze_event(ev, now=NOW)
        self.assertFalse(opp.is_arb)


class CoverageTests(unittest.TestCase):
    def q(self, st, floor=None, cap=None):
        return Quote.from_market(mkt("x", 0.1, 0.2, strike_type=st, floor=floor, cap=cap))

    def test_discrete_grid_is_exhaustive(self):
        qs = [self.q("less", cap=2.0), self.q("between", 2.1, 2.3), self.q("between", 2.4, 2.6), self.q("greater", floor=2.7)]
        self.assertTrue(strike_coverage_is_exhaustive(qs))

    def test_hole_is_not_exhaustive(self):
        qs = [self.q("less", cap=2.0), self.q("between", 2.1, 2.3), self.q("between", 2.9, 3.1), self.q("greater", floor=3.2)]
        self.assertFalse(strike_coverage_is_exhaustive(qs))

    def test_missing_tail_is_not_exhaustive(self):
        qs = [self.q("between", 2.0, 2.3), self.q("greater", floor=2.4)]
        self.assertFalse(strike_coverage_is_exhaustive(qs))


if __name__ == "__main__":
    unittest.main()
