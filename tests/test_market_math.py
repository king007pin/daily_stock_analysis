# -*- coding: utf-8 -*-
"""Tests for src/utils/market_math.py."""

import unittest

from src.utils.market_math import compute_circuit_buffer_pct


class TestComputeCircuitBufferPct(unittest.TestCase):
    def test_ns_suffix_uses_5pct_band(self):
        pct = compute_circuit_buffer_pct(100.0, "HAL.NS")
        self.assertAlmostEqual(pct, 5.0, places=6)

    def test_bo_suffix_uses_5pct_band(self):
        pct = compute_circuit_buffer_pct(100.0, "MYSORPETRO.BO")
        self.assertAlmostEqual(pct, 5.0, places=6)

    def test_sub_20_rupee_non_indian_uses_5pct_band(self):
        pct = compute_circuit_buffer_pct(15.0, "AAPL")
        self.assertAlmostEqual(pct, 5.0, places=6)

    def test_us_stock_above_20_uses_10pct_band(self):
        pct = compute_circuit_buffer_pct(150.0, "AAPL")
        self.assertAlmostEqual(pct, 10.0, places=6)

    def test_zero_price_does_not_divide_by_zero(self):
        self.assertEqual(compute_circuit_buffer_pct(0.0, "IDEA.NS"), 0.0)


if __name__ == "__main__":
    unittest.main()
