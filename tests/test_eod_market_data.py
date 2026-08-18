# -*- coding: utf-8 -*-
"""Tests for src/services/eod_market_data.py - all real-data calls mocked."""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from src.services.eod_market_data import (
    compute_btst_summary,
    compute_watchlist_movers,
    fetch_index_close,
)


class TestFetchIndexClose(unittest.TestCase):
    def test_unknown_index_raises(self):
        with self.assertRaises(ValueError):
            fetch_index_close("dax")

    def test_none_on_insufficient_history(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame({"Close": [100.0]})
        with patch("yfinance.Ticker", return_value=mock_ticker):
            self.assertIsNone(fetch_index_close("nifty"))

    def test_real_change_and_regime(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame({"Close": [24000.0, 24240.0]})
        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = fetch_index_close("nifty")
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.change_pct, 1.0, places=2)
        self.assertEqual(result.regime, "Risk-On")

    def test_negative_change_is_risk_off(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame({"Close": [24240.0, 24000.0]})
        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = fetch_index_close("sensex")
        self.assertEqual(result.regime, "Risk-Off")
        self.assertLess(result.change_pct, 0)


class TestComputeWatchlistMovers(unittest.TestCase):
    def test_ranks_real_changes_not_fabricated(self):
        manager = MagicMock()

        def fake_daily(symbol, days=5):
            data = {
                "GAINER.NS": [10.0, 11.0],   # +10%
                "LOSER.NS": [10.0, 9.0],     # -10%
                "FLAT.NS": [10.0, 10.0],     # 0%
            }[symbol]
            return pd.DataFrame({"close": data}), "mock"

        manager.get_daily_data.side_effect = fake_daily
        result = compute_watchlist_movers(["GAINER.NS", "LOSER.NS", "FLAT.NS"], fetcher_manager=manager)

        self.assertEqual(result["watchlist_top_gainers"][0]["symbol"], "GAINER.NS")
        self.assertEqual(result["watchlist_top_losers"][0]["symbol"], "LOSER.NS")

    def test_symbol_with_no_data_excluded_not_zero_filled(self):
        manager = MagicMock()
        manager.get_daily_data.side_effect = RuntimeError("no data source available")
        result = compute_watchlist_movers(["MISSING.NS"], fetcher_manager=manager)
        self.assertEqual(result["watchlist_top_gainers"], [])
        self.assertEqual(result["watchlist_top_losers"], [])


class TestComputeBtstSummary(unittest.TestCase):
    def test_no_signals_yet(self):
        mock_service = MagicMock()
        mock_service.get_stats.return_value = {"breakdowns": {"market": []}}
        with patch("src.services.decision_signal_outcome_service.DecisionSignalOutcomeService", return_value=mock_service):
            result = compute_btst_summary(market="in")
        self.assertEqual(result["status"], "NO_SIGNALS_YET")

    def test_unavailable_on_exception(self):
        with patch("src.services.decision_signal_outcome_service.DecisionSignalOutcomeService", side_effect=RuntimeError("db locked")):
            result = compute_btst_summary(market="in")
        self.assertEqual(result["status"], "UNAVAILABLE")

    def test_tracked_with_real_market_row(self):
        mock_service = MagicMock()
        mock_service.get_stats.return_value = {
            "breakdowns": {
                "market": [
                    {"value": "in", "total": 8, "completed": 3, "hit": 2, "miss": 1, "hit_rate_pct": 66.7, "avg_stock_return_pct": 1.1},
                    {"value": "us", "total": 100, "completed": 90, "hit": 50, "miss": 40, "hit_rate_pct": 55.6, "avg_stock_return_pct": 0.5},
                ]
            }
        }
        with patch("src.services.decision_signal_outcome_service.DecisionSignalOutcomeService", return_value=mock_service):
            result = compute_btst_summary(market="in")
        self.assertEqual(result["status"], "TRACKED")
        self.assertEqual(result["total"], 8)
        self.assertEqual(result["hit_rate_pct"], 66.7)


if __name__ == "__main__":
    unittest.main()
