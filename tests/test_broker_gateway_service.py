# -*- coding: utf-8 -*-
"""
Tests for Unified Broker Gateway & GTT Order Router Service
"""

import unittest
from src.services.broker_gateway_service import BrokerGatewayService, OrderSide, OrderType, OrderStatus, ExecutionReceipt


class TestBrokerGatewayService(unittest.TestCase):
    """Test suite for BrokerGatewayService."""

    def setUp(self):
        self.gateway = BrokerGatewayService(mode="PAPER", initial_cash=5000.0)

    def test_successful_buy_order_with_gtt_attachment(self):
        """Verify successful order execution and automatic GTT OCO order attachment."""
        receipt = self.gateway.route_order_with_gtt(
            symbol="RTNPOWER.NS",
            side=OrderSide.BUY,
            product=OrderType.CNC,
            quantity=596,
            price=8.38,
            stop_loss_price=8.00,
            target_price=9.25,
        )

        self.assertIsInstance(receipt, ExecutionReceipt)
        self.assertEqual(receipt.order_status, OrderStatus.FILLED.value)
        self.assertIsNotNone(receipt.gtt_order)
        self.assertEqual(receipt.gtt_order.stop_loss_trigger_price, 8.00)
        # 2% buffer on stop-loss limit
        self.assertAlmostEqual(receipt.gtt_order.stop_loss_limit_price, 7.84, places=2)
        self.assertEqual(receipt.gtt_order.target_trigger_price, 9.25)
        # Cash balance properly decremented
        self.assertLess(self.gateway.cash_balance, 10.0)

    def test_circuit_limit_safety_rejection(self):
        """Verify orders too close to upper circuit are rejected."""
        receipt = self.gateway.route_order_with_gtt(
            symbol="IDEA.NS",
            side=OrderSide.BUY,
            product=OrderType.CNC,
            quantity=100,
            price=15.00,
            upper_circuit=15.10,  # Within 1.5% buffer
        )
        self.assertEqual(receipt.order_status, OrderStatus.REJECTED.value)
        self.assertIn("Upper Circuit", receipt.rejection_reason)

    def test_insufficient_funds_rejection(self):
        """Verify order is rejected if capital exceeds balance."""
        receipt = self.gateway.route_order_with_gtt(
            symbol="HAL.NS",
            side=OrderSide.BUY,
            product=OrderType.CNC,
            quantity=10,
            price=5000.0,  # Requires 50,000, balance is 5,000
        )
        self.assertEqual(receipt.order_status, OrderStatus.REJECTED.value)
        self.assertIn("Insufficient balance", receipt.rejection_reason)


if __name__ == "__main__":
    unittest.main()
