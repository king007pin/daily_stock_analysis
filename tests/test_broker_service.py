# -*- coding: utf-8 -*-
"""Unit tests for BrokerExecutionService and ModelCalibrationService."""

import unittest
from src.services.broker_service import BrokerExecutionService, OrderBracket
from src.services.calibration_service import ModelCalibrationService, AuditMetrics


class TestBrokerAndCalibration(unittest.TestCase):
    """Verify smart order execution and nightly model calibration."""

    def setUp(self):
        self.broker = BrokerExecutionService(mode="paper", account_capital=100000.0)
        self.calibrator = ModelCalibrationService(target_win_rate=60.0)

    def test_paper_order_execution(self):
        order = self.broker.create_bracket_order(
            stock_code="RELIANCE.NS",
            action="BUY",
            entry_price=2800.0,
            target_price=3100.0,
            stop_loss_price=2700.0,
            recommended_position_pct=5.0,
        )
        self.assertIsInstance(order, OrderBracket)
        self.assertEqual(order.status, "FILLED")
        self.assertGreater(order.quantity, 0)
        self.assertEqual(order.stock_code, "RELIANCE.NS")

    def test_circuit_risk_order_rejection(self):
        # Microcap with active circuit risk should be rejected
        order = self.broker.create_bracket_order(
            stock_code="IDEA.NS",
            action="BUY",
            entry_price=7.40,
            target_price=8.50,
            stop_loss_price=6.80,
            recommended_position_pct=2.0,
            circuit_risk_flag=True,
        )
        self.assertEqual(order.status, "REJECTED")
        self.assertTrue(order.circuit_risk)
        self.assertEqual(order.quantity, 0)

    def test_calibration_metrics(self):
        # Trend correctly predicted in upward direction
        predicted = [100.0, 102.0, 104.0, 106.0, 108.0]
        actual = [100.0, 101.5, 103.8, 105.2, 107.9]
        metrics = self.calibrator.evaluate_predictions(predicted, actual)

        self.assertIsInstance(metrics, AuditMetrics)
        self.assertEqual(metrics.directional_accuracy_pct, 100.0)
        self.assertGreaterEqual(metrics.recommended_risk_multiplier, 1.0)
        self.assertEqual(metrics.status, "HEALTHY")

    def test_calibration_conservative_fallback_on_low_accuracy(self):
        # Inverted prediction direction
        predicted = [100.0, 105.0, 110.0, 115.0]
        actual = [100.0, 95.0, 90.0, 85.0]
        metrics = self.calibrator.evaluate_predictions(predicted, actual)

        self.assertEqual(metrics.directional_accuracy_pct, 0.0)
        self.assertLessEqual(metrics.recommended_risk_multiplier, 0.5)
        self.assertEqual(metrics.status, "CALIBRATING_CONSERVATIVE")


if __name__ == "__main__":
    unittest.main()
