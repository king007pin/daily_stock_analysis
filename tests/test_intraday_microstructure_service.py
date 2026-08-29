# -*- coding: utf-8 -*-
"""
Tests for Intraday Microstructure & Anchored VWAP Service
"""

import unittest
from src.services.intraday_microstructure_service import IntradayMicrostructureService, VWAPBandsResult, ORBResult


class TestIntradayMicrostructureService(unittest.TestCase):
    """Test suite for IntradayMicrostructureService."""

    def setUp(self):
        self.service = IntradayMicrostructureService()

    def test_anchored_vwap_bands_ordering(self):
        """Verify VWAP bands are strictly ordered: Lower3 < Lower2 < Lower1 < VWAP < Upper1 < Upper2 < Upper3."""
        prices = [100.0, 101.0, 102.5, 101.5, 103.0, 102.0, 104.0]
        volumes = [1000, 2500, 1800, 3000, 4500, 2000, 5000]

        res = self.service.calculate_anchored_vwap_bands("TCS.NS", prices, volumes)
        self.assertIsInstance(res, VWAPBandsResult)
        self.assertEqual(res.symbol, "TCS.NS")
        self.assertLess(res.lower_band_3, res.lower_band_2)
        self.assertLess(res.lower_band_2, res.lower_band_1)
        self.assertLessEqual(res.lower_band_1, res.vwap)
        self.assertGreaterEqual(res.upper_band_1, res.vwap)
        self.assertGreater(res.upper_band_2, res.upper_band_1)
        self.assertGreater(res.upper_band_3, res.upper_band_2)

    def test_zero_volume_epsilon_floor_handling(self):
        """Verify no zero division occurs when volume is all zero."""
        prices = [100.0, 101.0]
        volumes = [0.0, 0.0]

        res = self.service.calculate_anchored_vwap_bands("IDEA.NS", prices, volumes)
        self.assertEqual(res.current_price, 101.0)
        self.assertEqual(res.vwap, 101.0)
        self.assertGreater(res.vwap_std, 0.0)

    def test_orb_breakout_detection(self):
        """Verify Opening Range Breakout classification."""
        first_15m_highs = [101.0, 102.0, 102.5]
        first_15m_lows = [99.5, 100.0, 100.2]

        # Breakout high
        res_bull = self.service.calculate_orb("RTNPOWER.NS", first_15m_highs, first_15m_lows, current_price=103.0)
        self.assertIsInstance(res_bull, ORBResult)
        self.assertEqual(res_bull.breakout_direction, "BULLISH_BREAKOUT")
        self.assertTrue(res_bull.is_orb_broken)

        # Inside range
        res_inside = self.service.calculate_orb("RTNPOWER.NS", first_15m_highs, first_15m_lows, current_price=101.0)
        self.assertEqual(res_inside.breakout_direction, "INSIDE_RANGE")
        self.assertFalse(res_inside.is_orb_broken)


if __name__ == "__main__":
    unittest.main()
