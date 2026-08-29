# -*- coding: utf-8 -*-
"""
Tests for Multi-Agent Trading Swarm Consensus Service
"""

import unittest
from src.services.multi_agent_swarm_service import MultiAgentSwarmService, SwarmConsensusResult


class TestMultiAgentSwarmService(unittest.TestCase):
    """Test suite for MultiAgentSwarmService."""

    def setUp(self):
        self.swarm = MultiAgentSwarmService(required_approval_votes=4)

    def test_approved_unanimous_buy_signal(self):
        """Verify strong fundamental + technical candidate achieves APPROVED_BUY."""
        res = self.swarm.evaluate_swarm_consensus(
            symbol="HAL.NS",
            pe_ratio=28.5,
            volume_surge_ratio=2.2,
            price_vs_vwap_pct=1.2,
            rsi=62.0,
            atr_pct=2.4,
            circuit_buffer_pct=8.5,
            has_news_catalyst=True,
        )

        self.assertIsInstance(res, SwarmConsensusResult)
        self.assertEqual(res.decision, "APPROVED_BUY")
        self.assertGreaterEqual(res.buy_votes, 4)
        self.assertFalse(res.veto_triggered)

    def test_risk_manager_absolute_veto_on_circuit_buffer(self):
        """Verify Risk Manager veto rejects trade even if all other 4 agents vote BUY."""
        res = self.swarm.evaluate_swarm_consensus(
            symbol="IDEA.NS",
            pe_ratio=15.0,
            volume_surge_ratio=3.0,
            price_vs_vwap_pct=2.5,
            rsi=70.0,
            atr_pct=3.0,
            circuit_buffer_pct=1.2,  # Breaches 2.0% buffer!
            has_news_catalyst=True,
        )

        self.assertEqual(res.decision, "REJECTED_VETO")
        self.assertTrue(res.veto_triggered)
        self.assertIn("Circuit limit breach risk", res.veto_reason)

    def test_insufficient_votes_rejection(self):
        """Verify mediocre candidate with low momentum and volume is rejected."""
        res = self.swarm.evaluate_swarm_consensus(
            symbol="SLUGGISH.NS",
            pe_ratio=65.0,
            volume_surge_ratio=0.8,
            price_vs_vwap_pct=-0.5,
            rsi=42.0,
            atr_pct=2.0,
            circuit_buffer_pct=10.0,
            has_news_catalyst=False,
        )

        self.assertEqual(res.decision, "REJECTED_INSUFFICIENT_VOTES")
        self.assertLess(res.buy_votes, 4)


if __name__ == "__main__":
    unittest.main()
