# -*- coding: utf-8 -*-
"""Tests for decision-signal level validation.

每个用例都对应 2026-08-31 在真实信号里观察到的一种情况，数字取自
``decision_signals`` 与 ``stock_daily`` 的实测值。
"""

import unittest

from src.services.decision_signal_level_validator import (
    CAPTURE_FRACTION,
    validate_levels,
)


class LevelValidatorTestCase(unittest.TestCase):
    def test_non_directional_action_is_skipped(self) -> None:
        """watch 不表达方向，不应被几何规则拒绝。"""
        result = validate_levels(
            action="watch", entry=13.37, stop_loss=12.90, target_price=14.50
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.issues, ())

    def test_missing_target_is_rejected(self) -> None:
        """真实情况：29 条印度信号中有 9 条 target 为空或 0。"""
        result = validate_levels(
            action="buy", entry=10.0, stop_loss=9.0, target_price=None
        )
        self.assertFalse(result.ok)
        self.assertIn("missing_target", result.issues)

    def test_zero_target_counts_as_missing(self) -> None:
        result = validate_levels(
            action="sell", entry=10.0, stop_loss=11.0, target_price=0
        )
        self.assertFalse(result.ok)
        self.assertIn("missing_target", result.issues)

    def test_bearish_call_with_bullish_levels_is_inconsistent(self) -> None:
        """真实信号 id=55（BCG.NS, reduce）：stop 在下、target 在上。

        这是看多的几何，却挂在看空的 action 上。方向必须来自 action，
        若从 stop/target 大小关系反推，这个缺陷会被静默地当成正常方向。
        """
        result = validate_levels(
            action="reduce", entry=9.04, stop_loss=8.80, target_price=9.38
        )
        self.assertFalse(result.ok)
        self.assertIn("levels_inconsistent", result.issues)
        self.assertEqual(result.detail["expected"], "target < entry < stop")

    def test_properly_bracketed_bearish_call_passes_geometry(self) -> None:
        result = validate_levels(
            action="sell", entry=9.04, stop_loss=9.50, target_price=8.20
        )
        self.assertNotIn("levels_inconsistent", result.issues)

    def test_reward_risk_below_floor_is_rejected(self) -> None:
        """真实信号 id=48：目标比止损更近，R:R = 0.82。"""
        result = validate_levels(
            action="buy", entry=13.91, stop_loss=13.20, target_price=14.50
        )
        self.assertFalse(result.ok)
        self.assertIn("reward_risk_too_low", result.issues)
        self.assertLess(result.reward_risk, 1.0)

    def test_healthy_reward_risk_passes(self) -> None:
        """真实信号 id=49：R:R ≈ 2.55。"""
        result = validate_levels(
            action="buy", entry=13.92, stop_loss=13.30, target_price=15.50
        )
        self.assertNotIn("reward_risk_too_low", result.issues)
        self.assertGreater(result.reward_risk, 2.0)

    def test_target_unreachable_within_intraday_horizon(self) -> None:
        """真实信号 id=28（IDEA.NS, intraday）：目标 11.32% 外，日均振幅 3.06%。

        即便假设每天整段振幅都朝有利方向走，也需要约 3.7 个交易日 —— 当天
        收盘前不可能触及，因此这条信号永远停在"既没止损也没到目标"。
        """
        result = validate_levels(
            action="buy",
            entry=13.47,
            stop_loss=12.80,
            target_price=15.00,
            horizon="intraday",
            average_daily_range_pct=3.06,
        )
        self.assertFalse(result.ok)
        self.assertIn("target_unreachable_in_horizon", result.issues)
        self.assertGreater(result.favourable_days_to_target, 3.0)

    def test_reachable_target_within_three_day_horizon(self) -> None:
        """真实信号 id=12（RELIANCE.NS, 3d）：目标 3.13% 外，日均振幅 1.54%。"""
        result = validate_levels(
            action="buy",
            entry=1309.0,
            stop_loss=1290.0,
            target_price=1350.0,
            horizon="3d",
            average_daily_range_pct=1.54,
        )
        self.assertNotIn("target_unreachable_in_horizon", result.issues)

    def test_realistic_days_applies_measured_capture_fraction(self) -> None:
        """乐观天数按实测可捕获比例折算后应更大。"""
        result = validate_levels(
            action="buy",
            entry=100.0,
            stop_loss=95.0,
            target_price=110.0,
            horizon="10d",
            average_daily_range_pct=2.0,
        )
        self.assertAlmostEqual(result.favourable_days_to_target, 5.0, places=6)
        self.assertAlmostEqual(
            result.realistic_days_to_target, 5.0 / CAPTURE_FRACTION, places=6
        )

    def test_reachability_check_skipped_without_range(self) -> None:
        """没有日均振幅时跳过可达性检查，其余检查照常执行。"""
        result = validate_levels(
            action="buy", entry=100.0, stop_loss=95.0, target_price=110.0
        )
        self.assertTrue(result.ok)
        self.assertIsNone(result.favourable_days_to_target)


class PlanQualityDowngradeTestCase(unittest.TestCase):
    """plan_quality must reflect what the levels support, not what was claimed.

    Before 2026-08-31 `_normalize_plan_quality` only counted filled slots, so a
    signal with entry/stop/target/invalidation was labelled ``complete`` even when
    its geometry was backwards or its target unreachable. 42 signals claimed
    ``complete`` while 9 of 12 directional ones were not executable.
    """

    def setUp(self) -> None:
        from src.services.decision_signal_service import DecisionSignalService

        self.service = DecisionSignalService.__new__(DecisionSignalService)
        self.service._adr_cache = {}          # skip the DB lookup entirely

    def _quality(self, **fields):
        fields.setdefault("invalidation", "x")
        return self.service._normalize_plan_quality(None, fields=fields)

    def test_sound_plan_stays_complete(self) -> None:
        q = self._quality(
            stock_code="IDEA.NS", action="buy", horizon="5d",
            entry_low=14.00, entry_high=14.10, stop_loss=13.85, target_price=14.60,
        )
        self.assertEqual(q, "complete")

    def test_inverted_reward_risk_is_downgraded(self) -> None:
        """Real signal id=48: risking 0.71 to make 0.59."""
        q = self._quality(
            stock_code="IDEA.NS", action="buy", horizon="intraday",
            entry_low=13.79, entry_high=14.04, stop_loss=13.20, target_price=14.50,
        )
        self.assertEqual(q, "partial")

    def test_backwards_geometry_is_downgraded(self) -> None:
        """Real signal id=55: a bearish call with the stop below entry."""
        q = self._quality(
            stock_code="BCG.NS", action="reduce", horizon="intraday",
            entry_low=9.00, entry_high=9.08, stop_loss=8.80, target_price=9.38,
        )
        self.assertEqual(q, "partial")

    def test_caller_claim_does_not_override_measurement(self) -> None:
        """An explicit plan_quality must still be validated."""
        fields = dict(
            stock_code="BCG.NS", action="reduce", horizon="intraday",
            entry_low=9.00, entry_high=9.08, stop_loss=8.80, target_price=9.38,
            invalidation="x",
        )
        self.assertEqual(
            self.service._normalize_plan_quality("complete", fields=fields), "partial"
        )

    def test_minimal_is_left_alone(self) -> None:
        """Nothing to validate when there are no levels to check."""
        self.assertEqual(
            self.service._normalize_plan_quality("minimal", fields={"action": "buy"}),
            "minimal",
        )

if __name__ == "__main__":
    unittest.main()
