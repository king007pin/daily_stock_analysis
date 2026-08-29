# -*- coding: utf-8 -*-
"""
Tests for Intraday Signal & Margin Sizer Engine
"""

import unittest
from src.services.intraday_signal_engine import IntradaySignalEngine, IntradayTradeTicket


class TestIntradaySignalEngine(unittest.TestCase):
    """Test suite for IntradaySignalEngine."""

    def setUp(self):
        self.engine = IntradaySignalEngine(default_capital=5000.0)

    def test_approved_intraday_mis_signal(self):
        """Verify successful MIS signal generation with 5x leverage and 15:05 IST auto-squareoff."""
        prices_1m = [100.0, 100.5, 101.2, 101.8, 102.0]
        volumes_1m = [5000, 8000, 12000, 15000, 20000]
        orb_highs = [101.0, 101.5]
        orb_lows = [99.5, 100.0]

        ticket = self.engine.generate_intraday_signal(
            symbol="HAL.NS",
            current_price=102.0,
            prices_1m=prices_1m,
            volumes_1m=volumes_1m,
            orb_highs=orb_highs,
            orb_lows=orb_lows,
            pe_ratio=28.0,
            volume_surge_ratio=2.2,
            rsi=64.0,
            circuit_buffer_pct=8.0,
            is_fno_eligible=True,
            has_news_catalyst=True,
        )

        self.assertIsInstance(ticket, IntradayTradeTicket)
        self.assertEqual(ticket.action, "BUY_MIS")
        self.assertEqual(ticket.leverage_multiplier, 5.0)
        self.assertEqual(ticket.allocated_capital, 1250.0)  # 25% of 5000
        self.assertGreater(ticket.position_quantity, 0)
        self.assertGreater(ticket.target_price, ticket.entry_price)
        self.assertLess(ticket.stop_loss_price, ticket.entry_price)
        self.assertGreaterEqual(ticket.risk_reward_ratio, 2.0)
        self.assertEqual(ticket.auto_squareoff_time, "15:05 IST")

    def test_rejected_signal_on_circuit_risk(self):
        """Verify ticket is rejected if circuit buffer is violated."""
        prices_1m = [10.0, 10.5]
        volumes_1m = [1000, 2000]

        ticket = self.engine.generate_intraday_signal(
            symbol="IDEA.NS",
            current_price=10.5,
            prices_1m=prices_1m,
            volumes_1m=volumes_1m,
            orb_highs=[10.2],
            orb_lows=[9.8],
            pe_ratio=15.0,
            circuit_buffer_pct=1.0,  # Fails!
        )

        self.assertEqual(ticket.action, "NO_TRADE_REJECTED")
        self.assertEqual(ticket.position_quantity, 0)
        self.assertIsNotNone(ticket.rejection_reason)


if __name__ == "__main__":
    unittest.main()
