# -*- coding: utf-8 -*-
"""
Tests for src/services/intraday_bar_fetcher.py.

Central concern: nothing here should ever fall back to a value derived
purely from live_price, matching the Zero-Hallucination fix in
06-Scripts-Bridge/run_intraday_live_scanner.py. Every real-data call is
mocked so this suite runs offline (no `network` marker needed).
"""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from src.services.intraday_bar_fetcher import (
    compute_rsi,
    compute_volume_surge_ratio,
    fetch_intraday_bars,
    is_fno_eligible,
)


def _make_daily_df(closes, volumes):
    return pd.DataFrame({"close": closes, "volume": volumes})


class TestFetchIntradayBars(unittest.TestCase):
    def test_returns_none_on_empty_history(self):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        with patch("yfinance.Ticker", return_value=mock_ticker):
            self.assertIsNone(fetch_intraday_bars("IDEA.NS"))

    def test_returns_none_on_exception(self):
        with patch("yfinance.Ticker", side_effect=RuntimeError("boom")):
            self.assertIsNone(fetch_intraday_bars("IDEA.NS"))

    def test_real_bars_are_not_a_function_of_a_single_price(self):
        df = pd.DataFrame({
            "Open": [10.0, 10.1, 10.3, 10.2],
            "High": [10.2, 10.3, 10.5, 10.4],
            "Low": [9.9, 10.0, 10.1, 10.1],
            "Close": [10.1, 10.2, 10.4, 10.3],
            "Volume": [12000, 34000, 51000, 28000],
        })
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = df
        with patch("yfinance.Ticker", return_value=mock_ticker):
            bars = fetch_intraday_bars("SUBEXLTD.NS")

        self.assertIsNotNone(bars)
        # The old bug: volumes were a fixed [10000, 25000, 50000, 45000] list
        # regardless of symbol. Assert these are the real mocked volumes.
        self.assertEqual(bars.volumes_1m, [12000.0, 34000.0, 51000.0, 28000.0])
        self.assertEqual(bars.prices_1m, [10.1, 10.2, 10.4, 10.3])
        self.assertEqual(bars.cumulative_volume, 125000.0)


class TestComputeRsi(unittest.TestCase):
    def test_none_on_insufficient_history(self):
        manager = MagicMock()
        manager.get_daily_data.return_value = (_make_daily_df([10, 11], [100, 200]), "mock")
        self.assertIsNone(compute_rsi("IDEA.NS", fetcher_manager=manager, period=14))

    def test_none_on_fetch_exception(self):
        manager = MagicMock()
        manager.get_daily_data.side_effect = RuntimeError("no data source available")
        self.assertIsNone(compute_rsi("IDEA.NS", fetcher_manager=manager))

    def test_real_rsi_varies_with_input(self):
        manager = MagicMock()
        rising = list(range(10, 40))  # steadily rising closes -> high RSI
        manager.get_daily_data.return_value = (_make_daily_df(rising, [1000] * len(rising)), "mock")
        rsi = compute_rsi("IDEA.NS", fetcher_manager=manager, period=14)
        self.assertIsNotNone(rsi)
        self.assertGreater(rsi, 70.0)  # not the old hardcoded 62.0


class TestComputeVolumeSurgeRatio(unittest.TestCase):
    def test_none_on_empty_history(self):
        manager = MagicMock()
        manager.get_daily_data.return_value = (pd.DataFrame(), "mock")
        result = compute_volume_surge_ratio("IDEA.NS", 100000, 60, fetcher_manager=manager)
        self.assertIsNone(result)

    def test_surge_ratio_reflects_real_baseline(self):
        manager = MagicMock()
        # 10-day avg daily volume = 500,000; 60 min elapsed of 375 -> baseline ~80,000
        manager.get_daily_data.return_value = (
            _make_daily_df([10] * 10, [500000] * 10), "mock"
        )
        ratio = compute_volume_surge_ratio(
            "IDEA.NS", cumulative_volume_today=240000, minutes_elapsed_since_open=60, fetcher_manager=manager
        )
        self.assertIsNotNone(ratio)
        # 240000 / (500000 * 60/375) = 3.0 -> not the old hardcoded 2.1
        self.assertAlmostEqual(ratio, 3.0, places=2)


class TestIsFnoEligible(unittest.TestCase):
    def test_known_symbol_true(self):
        self.assertTrue(is_fno_eligible("HAL.NS"))

    def test_unknown_symbol_defaults_false(self):
        # Safe direction: unknown symbols must NOT get 5x leverage sizing.
        self.assertFalse(is_fno_eligible("RTNPOWER.NS"))

    def test_case_and_suffix_insensitive(self):
        self.assertTrue(is_fno_eligible("reliance.ns".upper()))


if __name__ == "__main__":
    unittest.main()
