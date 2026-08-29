# -*- coding: utf-8 -*-
"""
Tests for Option Chain & Vectorized Max Pain Service
"""

import unittest
from src.services.option_chain_service import OptionChainService, OptionStrikeRecord, OptionChainAnalysisResult


class TestOptionChainService(unittest.TestCase):
    """Test suite for OptionChainService."""

    def setUp(self):
        self.service = OptionChainService()

    def test_vectorized_max_pain_calculation(self):
        """Verify vectorized Max Pain correctly identifies the minimum option loss strike."""
        strikes = [24000.0, 24100.0, 24200.0, 24300.0, 24400.0]
        # Massive Call OI at 24300 & 24400, Massive Put OI at 24000 & 24100
        call_ois = [10000, 20000, 150000, 500000, 800000]
        put_ois = [800000, 500000, 150000, 20000, 10000]

        max_pain = self.service.calculate_max_pain(strikes, call_ois, put_ois)
        self.assertEqual(max_pain, 24200.0)

    def test_option_chain_analysis_bullish_and_bearish(self):
        """Verify PCR and sentiment classification."""
        records = [
            OptionStrikeRecord(strike_price=24000.0, ce_oi=50000, ce_volume=10000, ce_iv=0.12, ce_ltp=250.0, pe_oi=200000, pe_volume=30000, pe_iv=0.14, pe_ltp=30.0),
            OptionStrikeRecord(strike_price=24200.0, ce_oi=80000, ce_volume=20000, ce_iv=0.13, ce_ltp=110.0, pe_oi=160000, pe_volume=25000, pe_iv=0.13, pe_ltp=90.0),
            OptionStrikeRecord(strike_price=24400.0, ce_oi=120000, ce_volume=40000, ce_iv=0.15, ce_ltp=30.0, pe_oi=40000, pe_volume=10000, pe_iv=0.16, pe_ltp=220.0),
        ]

        res = self.service.analyze_option_chain(
            symbol="NIFTY",
            spot_price=24250.0,
            strike_records=records,
        )

        self.assertIsInstance(res, OptionChainAnalysisResult)
        self.assertEqual(res.underlying_symbol, "NIFTY")
        self.assertGreater(res.pcr_oi, 1.2)  # More puts than calls
        self.assertEqual(res.atm_strike, 24200.0)
        self.assertEqual(res.market_sentiment, "BULLISH")


if __name__ == "__main__":
    unittest.main()
