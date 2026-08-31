# -*- coding: utf-8 -*-
"""Minimum viable horizon, derived from measured volatility and cost.

Values are the real measurements taken on 2026-08-31 from ``stock_daily``.
"""

import unittest

from src.services.horizon_policy import (
    CAPTURE_FRACTION,
    minimum_viable_horizon,
    noise_safe_stop_pct,
    reachable_target_pct,
    required_target_pct,
    round_trip_cost_pct,
    tick_size,
)


class TickAndCostTestCase(unittest.TestCase):
    def test_tick_bands_follow_nse(self) -> None:
        self.assertEqual(tick_size(1.25), 0.01)     # below Rs 250
        self.assertEqual(tick_size(249.99), 0.01)
        self.assertEqual(tick_size(500.0), 0.05)    # Rs 250-1000
        self.assertEqual(tick_size(2241.0), 0.10)   # above Rs 1000

    def test_tick_dominates_cost_on_cheap_names(self) -> None:
        """On GTLINFRA one paisa is 0.8% of price - far above statutory cost."""
        cheap = round_trip_cost_pct(1.29)
        rich = round_trip_cost_pct(2241.10)
        self.assertGreater(cheap, 1.5)
        self.assertLess(rich, 0.15)
        self.assertGreater(cheap, rich * 10)


class MinimumViableHorizonTestCase(unittest.TestCase):
    def test_no_instrument_supports_intraday(self) -> None:
        """The measured headline: 0 of 13 names are viable in one day."""
        universe = [
            ("GTLINFRA.NS", 3.37, 1.29), ("DISHTV.NS", 2.99, 2.95),
            ("EASEMYTRIP.NS", 3.07, 6.77), ("RTNPOWER.NS", 2.72, 8.83),
            ("BCG.NS", 3.56, 9.59), ("PCJEWELLER.NS", 4.64, 9.67),
            ("GREENPOWER.NS", 2.40, 10.06), ("ALOKINDS.NS", 2.67, 12.16),
            ("IDEA.NS", 3.06, 13.98), ("JPPOWER.NS", 2.53, 17.48),
            ("RELIANCE.NS", 1.54, 1304.91), ("TCS.NS", 2.54, 2241.10),
            ("HAL.NS", 2.04, 4641.70),
        ]
        for code, adr, price in universe:
            with self.subTest(code=code):
                self.assertNotIn(minimum_viable_horizon(adr, price), ("1d", None))

    def test_liquid_names_are_viable_in_five_days(self) -> None:
        self.assertEqual(minimum_viable_horizon(3.06, 13.98), "5d")     # IDEA
        self.assertEqual(minimum_viable_horizon(1.54, 1304.91), "5d")   # RELIANCE

    def test_thin_cheap_names_need_ten_days(self) -> None:
        self.assertEqual(minimum_viable_horizon(3.37, 1.29), "10d")     # GTLINFRA
        self.assertEqual(minimum_viable_horizon(2.99, 2.95), "10d")     # DISHTV

    def test_unusable_instrument_returns_none(self) -> None:
        """A name whose cost swamps its movement has no viable horizon."""
        self.assertIsNone(minimum_viable_horizon(0.30, 0.50))

    def test_missing_inputs_return_none(self) -> None:
        self.assertIsNone(minimum_viable_horizon(None, 100.0))
        self.assertIsNone(minimum_viable_horizon(3.0, None))
        self.assertIsNone(minimum_viable_horizon(0.0, 100.0))


class TargetAndStopTestCase(unittest.TestCase):
    def test_reachable_target_scales_with_horizon(self) -> None:
        five = reachable_target_pct(3.06, "5d")
        ten = reachable_target_pct(3.06, "10d")
        self.assertAlmostEqual(ten, five * 2, places=6)

    def test_reachable_target_applies_measured_capture(self) -> None:
        """Not the full range - only the share actually captured."""
        self.assertAlmostEqual(
            reachable_target_pct(2.0, "5d"), 0.6 * 2.0 * CAPTURE_FRACTION * 5, places=6
        )

    def test_stop_floor_beats_noise(self) -> None:
        """A stop must survive normal movement, not merely exceed costs."""
        adr, price = 3.06, 13.98
        self.assertGreaterEqual(noise_safe_stop_pct(adr, price), 0.5 * adr)
        self.assertGreater(noise_safe_stop_pct(adr, price), round_trip_cost_pct(price))

    def test_required_target_clears_costs_on_both_legs(self) -> None:
        adr, price = 3.06, 13.98
        needed = required_target_pct(adr, price)
        stop = noise_safe_stop_pct(adr, price)
        cost = round_trip_cost_pct(price)
        net_reward = needed - cost
        net_risk = stop + cost
        self.assertAlmostEqual(net_reward / net_risk, 2.0, places=6)

    def test_unknown_horizon_has_no_reachable_target(self) -> None:
        self.assertIsNone(reachable_target_pct(3.0, "99d"))
        self.assertIsNone(reachable_target_pct(3.0, None))


if __name__ == "__main__":
    unittest.main()
