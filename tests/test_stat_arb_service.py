# -*- coding: utf-8 -*-
"""
Tests for Statistical Arbitrage & Black-Scholes Greeks Service
"""

import unittest
import numpy as np
import pandas as pd
from src.services.stat_arb_service import StatArbService, PairSpreadResult, OptionGreeksResult


class TestStatArbService(unittest.TestCase):
    """Test suite for StatArbService."""

    def setUp(self):
        self.service = StatArbService(zscore_entry_threshold=2.0)

    def test_cointegrated_pair_spread_and_zscore(self):
        """Verify synthetic cointegrated series generates valid spread and signals."""
        np.random.seed(42)
        n = 100
        # Common random walk
        rw = np.cumsum(np.random.normal(0, 1, n)) + 100.0
        prices_b = pd.Series(rw)
        # Stationary mean-reverting spread
        spread_noise = np.random.normal(0, 0.5, n)
        prices_a = pd.Series(rw * 1.5 + spread_noise)

        res = self.service.calculate_pair_spread(prices_a, prices_b, "TCS.NS", "INFY.NS")
        self.assertIsInstance(res, PairSpreadResult)
        self.assertEqual(res.asset_a, "TCS.NS")
        self.assertEqual(res.asset_b, "INFY.NS")
        self.assertAlmostEqual(res.hedge_ratio_beta, 1.0, delta=0.5)
        self.assertTrue(-4.0 <= res.current_zscore <= 4.0)
        self.assertIn(res.trade_signal, ["LONG_A_SHORT_B", "SHORT_A_LONG_B", "NEUTRAL"])

    def test_black_scholes_call_and_put_greeks(self):
        """Verify Black-Scholes pricing and Greeks for Calls and Puts."""
        res_call = self.service.calculate_black_scholes_greeks(
            spot_price=100.0,
            strike_price=100.0,
            time_to_expiry_days=30.0,
            implied_volatility=0.20,
            risk_free_rate=0.065,
            option_type="CALL",
        )
        self.assertIsInstance(res_call, OptionGreeksResult)
        self.assertGreater(res_call.theoretical_price, 0.0)
        self.assertGreater(res_call.delta, 0.45)
        self.assertLess(res_call.delta, 0.65)
        self.assertGreater(res_call.gamma, 0.0)
        self.assertLess(res_call.theta, 0.0)  # Time decay is negative

        res_put = self.service.calculate_black_scholes_greeks(
            spot_price=100.0,
            strike_price=100.0,
            time_to_expiry_days=30.0,
            implied_volatility=0.20,
            risk_free_rate=0.065,
            option_type="PUT",
        )
        self.assertIsInstance(res_put, OptionGreeksResult)
        self.assertLess(res_put.delta, 0.0)
        self.assertGreater(res_put.delta, -1.0)

    def test_expiry_boundary_singularity_clamp(self):
        """Ensure zero division does not occur at near-zero expiry (T -> 0)."""
        res_near_zero = self.service.calculate_black_scholes_greeks(
            spot_price=105.0,
            strike_price=100.0,
            time_to_expiry_days=0.0001,  # Near zero
            implied_volatility=0.20,
            option_type="CALL",
        )
        self.assertGreaterEqual(res_near_zero.theoretical_price, 5.0)
        self.assertFalse(np.isnan(res_near_zero.delta))
        self.assertFalse(np.isnan(res_near_zero.gamma))


if __name__ == "__main__":
    unittest.main()
