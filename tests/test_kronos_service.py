# -*- coding: utf-8 -*-
"""Comprehensive unit and integration tests for Kronos Forecaster & DSA Integration."""

import unittest
from unittest.mock import patch, MagicMock
import numpy as np
import pandas as pd

from src.services.kronos_service import (
    KronosForecaster,
    KronosForecastResult,
)
from src.stock_analyzer import StockTrendAnalyzer
from src.report_language import localize_kronos_forecast
from src.agent.tools.analysis_tools import forecast_kronos_tool, _handle_forecast_kronos


def _make_dummy_ohlcv(n_days=60, base_price=100.0, trend=0.005) -> pd.DataFrame:
    """Generate mock OHLCV dataframe with a slight trend."""
    dates = pd.date_range(end=pd.Timestamp.now(), periods=n_days, freq="D")
    prices = [base_price * (1.0 + trend) ** i for i in range(n_days)]
    df = pd.DataFrame({
        "date": dates,
        "open": [p * 0.99 for p in prices],
        "high": [p * 1.02 for p in prices],
        "low": [p * 0.98 for p in prices],
        "close": prices,
        "volume": [1000000 + i * 1000 for i in range(n_days)],
    })
    return df


class TestKronosService(unittest.TestCase):
    """Test core KronosForecaster functionality."""

    def setUp(self):
        self.df = _make_dummy_ohlcv(n_days=60, base_price=100.0, trend=0.005)
        self.forecaster = KronosForecaster(horizon_days=5)

    def test_forecast_result_dataclass_and_dict(self):
        res = self.forecaster.forecast(self.df, "RELIANCE.NS", horizon_days=5)
        self.assertIsInstance(res, KronosForecastResult)
        self.assertEqual(res.stock_code, "RELIANCE.NS")
        self.assertEqual(res.horizon_days, 5)
        self.assertEqual(len(res.forecast_prices), 5)
        self.assertEqual(len(res.volatility_band_upper), 5)
        self.assertEqual(len(res.volatility_band_lower), 5)
        self.assertIn(res.trend_direction, ("BULLISH", "BEARISH", "SIDEWAYS"))
        self.assertGreaterEqual(res.confidence_score, 0)
        self.assertLessEqual(res.confidence_score, 100)

        # Test dictionary conversion
        d = res.to_dict()
        self.assertEqual(d["stock_code"], "RELIANCE.NS")
        self.assertIn("forecast_prices", d)
        self.assertIn("target_take_profit", d)
        self.assertIn("target_stop_loss", d)
        self.assertIn("risk_reward_ratio", d)

    def test_insufficient_data_handling(self):
        empty_df = pd.DataFrame()
        res = self.forecaster.forecast(empty_df, "TCS.NS")
        self.assertEqual(res.current_price, 0.0)
        self.assertIn("Insufficient", res.summary_text)

        short_df = _make_dummy_ohlcv(n_days=5)
        res_short = self.forecaster.forecast(short_df, "TCS.NS")
        self.assertIn("Insufficient", res_short.summary_text)

    def test_penny_stock_normalization_and_denormalization(self):
        # Test low-price stock (e.g. ₹1.25)
        penny_df = _make_dummy_ohlcv(n_days=60, base_price=1.25, trend=0.002)
        res = self.forecaster.forecast(penny_df, "VIKASLIFE.NS", horizon_days=5)
        self.assertAlmostEqual(res.current_price, penny_df.iloc[-1]["close"], places=2)
        self.assertGreater(res.target_stop_loss, 0.0)
        self.assertGreater(res.target_take_profit, 0.0)
        self.assertGreater(res.risk_reward_ratio, 0.0)

    def test_sidecar_api_success(self):
        forecaster_with_api = KronosForecaster(api_url="http://mock-kronos:8000", horizon_days=5)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "current_price": 100.0,
            "forecast_prices": [101.0, 102.0, 103.0, 104.0, 105.0],
            "projected_return_pct": 5.0,
            "volatility_band_upper": [102.0, 104.0, 106.0, 108.0, 110.0],
            "volatility_band_lower": [99.0, 98.0, 97.0, 96.0, 95.0],
            "trend_direction": "BULLISH",
            "confidence_score": 85,
            "target_take_profit": 110.0,
            "target_stop_loss": 95.0,
            "risk_reward_ratio": 2.0,
            "summary_text": "API Forecast Bullish",
        }

        with patch("requests.post", return_value=mock_response):
            res = forecaster_with_api.forecast(self.df, "RELIANCE.NS")
            self.assertEqual(res.engine_type, "kronos_api")
            self.assertEqual(res.trend_direction, "BULLISH")
            self.assertEqual(res.confidence_score, 85)

    def test_stock_trend_analyzer_integration(self):
        analyzer = StockTrendAnalyzer()
        res = analyzer.analyze(self.df, "LT.NS")
        self.assertIsNotNone(res.kronos_forecast)
        self.assertEqual(res.kronos_forecast["stock_code"], "LT.NS")
        self.assertIn("forecast_prices", res.kronos_forecast)

    def test_localization_formatting(self):
        mock_forecast = {
            "stock_code": "BCG.NS",
            "current_price": 9.93,
            "horizon_days": 5,
            "forecast_prices": [10.0, 10.2, 10.4, 10.5, 10.6],
            "projected_return_pct": 6.75,
            "trend_direction": "BULLISH",
            "confidence_score": 75,
            "target_take_profit": 11.20,
            "target_stop_loss": 9.40,
            "risk_reward_ratio": 2.4,
        }

        # English format
        en_text = localize_kronos_forecast(mock_forecast, language="en")
        self.assertIn("Kronos AI Forward Price Projection", en_text)
        self.assertIn("Bullish", en_text)
        self.assertIn("+6.75%", en_text)
        self.assertIn("₹11.20", en_text)
        self.assertIn("₹9.40", en_text)

        # Chinese format
        zh_text = localize_kronos_forecast(mock_forecast, language="zh")
        self.assertIn("Kronos 基础模型时间序列预测", zh_text)
        self.assertIn("看多", zh_text)

        # Empty forecast handling
        empty_text = localize_kronos_forecast(None, language="en")
        self.assertEqual(empty_text, "")

        # Multi-market currency symbol test
        us_forecast = dict(mock_forecast, stock_code="AAPL")
        us_text = localize_kronos_forecast(us_forecast, language="en")
        self.assertIn("$11.20", us_text)

        hk_forecast = dict(mock_forecast, stock_code="00700")
        hk_text = localize_kronos_forecast(hk_forecast, language="en")
        self.assertIn("HK$11.20", hk_text)

        cn_forecast = dict(mock_forecast, stock_code="600519")
        cn_text = localize_kronos_forecast(cn_forecast, language="zh")
        self.assertIn("¥11.20", cn_text)

    def test_nan_and_inf_resilience(self):
        dirty_df = self.df.copy()
        dirty_df.loc[10, "close"] = np.nan
        dirty_df.loc[20, "close"] = -5.0
        dirty_df.loc[30, "close"] = np.inf
        res = self.forecaster.forecast(dirty_df, "RELIANCE.NS", horizon_days=5)
        self.assertIsInstance(res, KronosForecastResult)
        self.assertGreater(res.current_price, 0.0)
        self.assertEqual(len(res.forecast_prices), 5)

    def test_agent_forecast_tool(self):
        self.assertEqual(forecast_kronos_tool.name, "forecast_kronos")
        with patch("src.agent.tools.analysis_tools._fetch_trend_data", return_value=self.df):
            result = _handle_forecast_kronos("RELIANCE.NS", horizon_days=5)
            self.assertIn("stock_code", result)
            self.assertEqual(result["stock_code"], "RELIANCE.NS")
            self.assertIn("forecast_prices", result)

    def test_analyze_trend_tool_includes_kronos_forecast(self):
        from src.agent.tools.analysis_tools import _handle_analyze_trend
        with patch("src.agent.tools.analysis_tools._fetch_trend_data", return_value=self.df):
            result = _handle_analyze_trend("RELIANCE.NS")
            self.assertIn("kronos_forecast", result)
            self.assertIsNotNone(result["kronos_forecast"])
            self.assertEqual(result["kronos_forecast"]["stock_code"], "RELIANCE.NS")

    def test_quantile_probabilistic_bands_and_non_crossing(self):
        res = self.forecaster.forecast(self.df, "RELIANCE.NS", horizon_days=5)
        self.assertEqual(len(res.quantile_p10), 5)
        self.assertEqual(len(res.quantile_p50), 5)
        self.assertEqual(len(res.quantile_p90), 5)
        # Strict Monotonic Non-crossing constraint: P10 <= P50 <= P90
        for p10, p50, p90 in zip(res.quantile_p10, res.quantile_p50, res.quantile_p90):
            self.assertLessEqual(p10, p50 + 1e-6)
            self.assertLessEqual(p50, p90 + 1e-6)

    def test_circuit_buffer_and_risk_flag(self):
        # Normal stock should have healthy buffer > 1.5%
        res_normal = self.forecaster.forecast(self.df, "RELIANCE.NS", horizon_days=5)
        self.assertGreater(res_normal.circuit_buffer_pct, 0.0)

        # Extreme high-volatility microcap
        penny_df = _make_dummy_ohlcv(n_days=60, base_price=3.5, trend=-0.03)
        res_penny = self.forecaster.forecast(penny_df, "IDEA.NS", horizon_days=5)
        self.assertIsInstance(res_penny.circuit_risk_flag, bool)

    def test_fractional_kelly_sizing_bounds(self):
        res = self.forecaster.forecast(self.df, "TCS.NS", horizon_days=5)
        self.assertGreaterEqual(res.kelly_fraction, 0.0)
        self.assertLessEqual(res.kelly_fraction, 0.25)
        self.assertGreaterEqual(res.recommended_position_pct, 0.0)
        self.assertLessEqual(res.recommended_position_pct, 25.0)

    def test_multi_horizon_summary(self):
        res = self.forecaster.forecast(self.df, "AAPL", horizon_days=10)
        self.assertIn("3d", res.multi_horizon_summary)
        self.assertIn("5d", res.multi_horizon_summary)
        self.assertIn("10d", res.multi_horizon_summary)


if __name__ == "__main__":
    unittest.main()

