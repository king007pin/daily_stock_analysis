# -*- coding: utf-8 -*-
"""
Tests for Macro Radar & Institutional Flow Service
"""

import unittest
import numpy as np
import pandas as pd
from src.services.macro_radar_service import MacroRadarService, InstitutionalFlowResult, IntermarketCorrelationResult


class TestMacroRadarService(unittest.TestCase):
    """Test suite for MacroRadarService."""

    def setUp(self):
        self.service = MacroRadarService()

    def test_evaluate_institutional_flows_bullish_and_bearish(self):
        """Verify flow thresholds generate correct institutional bias."""
        res_bull = self.service.evaluate_institutional_flows(
            fii_cash_cr=2500.0,
            dii_cash_cr=1200.0,
            fii_long_contracts=200000,
            fii_short_contracts=80000,
        )
        self.assertIsInstance(res_bull, InstitutionalFlowResult)
        self.assertEqual(res_bull.institutional_bias, "BULLISH")
        self.assertGreater(res_bull.fii_index_futures_long_pct, 65.0)

        res_bear = self.service.evaluate_institutional_flows(
            fii_cash_cr=-3500.0,
            dii_cash_cr=500.0,
            fii_long_contracts=60000,
            fii_short_contracts=180000,
        )
        self.assertEqual(res_bear.institutional_bias, "BEARISH")
        self.assertLess(res_bear.fii_index_futures_long_pct, 30.0)

    def test_intermarket_correlation_matrix(self):
        """Verify rolling Pearson correlation and regime identification."""
        np.random.seed(42)
        n = 50
        bench = pd.Series(np.random.normal(0.001, 0.01, n))
        crude = pd.Series(-bench.values * 0.8 + np.random.normal(0, 0.005, n))
        usdinr = pd.Series(-bench.values * 0.9 + np.random.normal(0, 0.005, n))
        gold = pd.Series(np.random.normal(0, 0.01, n))

        res = self.service.calculate_intermarket_correlations(
            benchmark_returns=bench,
            macro_asset_returns={"BRENT_CRUDE": crude, "USDINR": usdinr, "GOLD": gold},
            rolling_window=30,
        )
        self.assertIsInstance(res, IntermarketCorrelationResult)
        self.assertIn("BRENT_CRUDE", res.correlations)
        self.assertIn("USDINR", res.correlations)
        self.assertLess(res.correlations["USDINR"], -0.5)


if __name__ == "__main__":
    unittest.main()
