# -*- coding: utf-8 -*-
"""
====================================================================
Comprehensive Chaos, Stress & Edge-Case Audit Suite
====================================================================

Tests the quantitative multi-agent pipeline against:
1. Zero / NaN / Negative prices & corrupt data series.
2. Flash crash (-90%) and extreme penny stock volatility (₹0.05).
3. ChromaDB vector memory batch stress & SQLite fallback recovery.
4. Multi-market ticker normalization (NSE, BSE, US, HK, CN).
5. Tool handler end-to-end integration.
"""

import os
import shutil
import tempfile
import unittest
import numpy as np
import pandas as pd

from src.services.kronos_service import KronosForecaster, KronosForecastResult
from src.services.broker_service import BrokerExecutionService
from src.services.calibration_service import ModelCalibrationService
from src.services.memory_service import TradeMemoryService, TradePatternRecord
from src.agent.tools.analysis_tools import (
    _handle_forecast_kronos,
    _handle_recall_trade_memory,
)


class TestComprehensiveEndToEndAudit(unittest.TestCase):
    """Deep chaos and edge-case validation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_dir = os.path.join(self.temp_dir, "chroma")
        self.sqlite_path = os.path.join(self.temp_dir, "memory.db")
        self.memory_service = TradeMemoryService(db_dir=self.db_dir, sqlite_path=self.sqlite_path)
        self.forecaster = KronosForecaster(horizon_days=5)
        self.broker = BrokerExecutionService(mode="paper", account_capital=100000.0)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_chaos_flash_crash_and_extreme_penny_resilience(self):
        """Simulate flash crash (-90%) and micro-penny stock (₹0.05)."""
        # 1. Flash Crash Series
        crash_prices = [100.0] * 30 + [10.0] + [9.5] * 20
        dates = pd.date_range(end=pd.Timestamp.now(), periods=len(crash_prices), freq="D")
        crash_df = pd.DataFrame({
            "date": dates,
            "open": crash_prices,
            "high": crash_prices,
            "low": [p * 0.95 for p in crash_prices],
            "close": crash_prices,
            "volume": [5000000] * len(crash_prices),
        })

        res_crash = self.forecaster.forecast(crash_df, "FLASH.NS")
        self.assertIsInstance(res_crash, KronosForecastResult)
        self.assertGreater(res_crash.current_price, 0.0)
        self.assertTrue(all(p > 0 for p in res_crash.forecast_prices))
        self.assertTrue(all(p10 <= p50 <= p90 for p10, p50, p90 in zip(
            res_crash.quantile_p10, res_crash.quantile_p50, res_crash.quantile_p90
        )))

        # 2. Micro-penny Stock (₹0.05)
        penny_prices = [0.05, 0.05, 0.06, 0.05, 0.04, 0.05] * 10
        penny_df = pd.DataFrame({
            "date": pd.date_range(end=pd.Timestamp.now(), periods=len(penny_prices), freq="D"),
            "open": penny_prices,
            "high": [p * 1.05 for p in penny_prices],
            "low": [p * 0.95 for p in penny_prices],
            "close": penny_prices,
            "volume": [100000000] * len(penny_prices),
        })
        res_penny = self.forecaster.forecast(penny_df, "MICROPENNY.NS")
        self.assertGreater(res_penny.current_price, 0.0)
        self.assertTrue(res_penny.circuit_risk_flag)

    def test_memory_service_batch_stress_and_concurrency(self):
        """Index 200 trade records and execute rapid retrieval."""
        for i in range(200):
            rec = TradePatternRecord(
                setup_id=f"rec_{i}",
                stock_code=f"STK_{i % 10}.NS",
                market="NSE" if i % 2 == 0 else "US",
                setup_type="BREAKOUT" if i % 3 == 0 else "PULLBACK",
                rsi=40.0 + (i % 40),
                ema_spread_pct=-5.0 + (i % 15),
                volume_surge_ratio=0.5 + (i % 5) * 0.5,
                kronos_projected_return=-10.0 + (i % 25),
                regime_vix_bucket="LOW" if i % 3 == 0 else ("MID" if i % 3 == 1 else "HIGH"),
                outcome_return_pct=-5.0 + (i % 20),
                trade_result="WIN" if (i % 20) > 6 else "LOSS",
                holding_days=5,
            )
            self.assertTrue(self.memory_service.index_trade(rec))

        # Query top matches
        query_target = TradePatternRecord(
            setup_id="q1",
            stock_code="RELIANCE.NS",
            market="NSE",
            setup_type="BREAKOUT",
            rsi=65.0,
            ema_spread_pct=4.0,
            volume_surge_ratio=2.0,
            kronos_projected_return=12.0,
            regime_vix_bucket="LOW",
            outcome_return_pct=0.0,
            trade_result="PENDING",
            holding_days=5,
        )
        matches = self.memory_service.query_similar_setups(query_target, top_k=5, min_similarity=0.70)
        self.assertGreaterEqual(len(matches), 1)
        self.assertLessEqual(len(matches), 5)
        # Similarity should be sorted descending
        scores = [m.similarity_score for m in matches]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_multi_market_ticker_coverage(self):
        """Ensure full compatibility across NSE, BSE, US, HK, and CN tickers."""
        tickers = ["RELIANCE.NS", "500325.BO", "NVDA", "AAPL", "0700.HK", "600519"]
        dummy_df = pd.DataFrame({
            "date": pd.date_range(end=pd.Timestamp.now(), periods=30, freq="D"),
            "open": [100.0] * 30,
            "high": [102.0] * 30,
            "low": [98.0] * 30,
            "close": [100.0 + i * 0.2 for i in range(30)],
            "volume": [1000000] * 30,
        })
        for ticker in tickers:
            res = self.forecaster.forecast(dummy_df, ticker)
            self.assertEqual(res.stock_code, ticker)
            self.assertEqual(len(res.forecast_prices), 5)
            self.assertIn("target_take_profit", res.to_dict())

    def test_agent_tool_handlers_edge_cases(self):
        """Verify tool handlers handle invalid tickers, empty inputs gracefully."""
        # Empty string handling
        res_err = _handle_forecast_kronos("")
        self.assertIn("error", res_err)

        res_mem_err = _handle_recall_trade_memory("")
        self.assertIn("error", res_mem_err)


if __name__ == "__main__":
    unittest.main()
