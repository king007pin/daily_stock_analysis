# -*- coding: utf-8 -*-
"""
Tests for Portfolio Optimizer & Risk Parity Service
"""

import unittest
import numpy as np
import pandas as pd
from src.services.portfolio_optimizer_service import PortfolioOptimizerService, PortfolioOptimizationResult


class TestPortfolioOptimizerService(unittest.TestCase):
    """Test suite for PortfolioOptimizerService."""

    def setUp(self):
        self.service = PortfolioOptimizerService(risk_free_rate=0.065)

    def test_max_sharpe_weights_sum_to_one(self):
        """Verify weights sum to 1.0 and are non-negative."""
        np.random.seed(42)
        n_days = 100
        returns_df = pd.DataFrame({
            "RELIANCE.NS": np.random.normal(0.001, 0.015, n_days),
            "TCS.NS": np.random.normal(0.0012, 0.012, n_days),
            "HAL.NS": np.random.normal(0.002, 0.020, n_days),
            "IDEA.NS": np.random.normal(0.0005, 0.035, n_days),
        })

        res = self.service.optimize_portfolio(returns_df, method="MAX_SHARPE")
        self.assertIsInstance(res, PortfolioOptimizationResult)
        self.assertEqual(res.method, "MAX_SHARPE")
        # Check sum of weights
        total_w = sum(res.weights.values())
        self.assertAlmostEqual(total_w, 1.0, places=3)
        # All weights non-negative
        for w in res.weights.values():
            self.assertGreaterEqual(w, 0.0)
        self.assertGreater(res.annual_volatility_pct, 0.0)
        self.assertGreater(res.cvar_99_pct, 0.0)

    def test_min_variance_method(self):
        """Verify Minimum Variance optimization mode."""
        np.random.seed(42)
        returns_df = pd.DataFrame({
            "STOCK_A": np.random.normal(0.001, 0.01, 50),
            "STOCK_B": np.random.normal(0.001, 0.03, 50),
        })
        res = self.service.optimize_portfolio(returns_df, method="MIN_VARIANCE")
        self.assertAlmostEqual(sum(res.weights.values()), 1.0, places=3)
        # Lower volatility stock A should receive higher weight in min variance
        self.assertGreater(res.weights["STOCK_A"], res.weights["STOCK_B"])


if __name__ == "__main__":
    unittest.main()
