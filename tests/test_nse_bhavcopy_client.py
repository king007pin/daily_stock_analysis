# -*- coding: utf-8 -*-
"""NSE bhavcopy parsing and the comparison tolerances.

Every threshold here is pinned to a measurement taken on 2026-09-01 over 265
comparisons (13 symbols x 21 trading days), not to a guess:

  volume  - 1 of 265 differed by more than 2% (the real IDEA.NS error, 70%).
            The largest of the rest was 0.5042%. Signal and noise are ~140x apart.
  price   - 12 of 13 symbols had a ratio of exactly 1.00000 between the stored
            adjusted close and the published raw close. Only HAL.NS showed a
            dividend step, at 0.202%.

These tests exist to fail if someone widens or narrows a tolerance without
re-measuring.
"""

import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.services.nse_bhavcopy_client import (
    PRICE_TOLERANCE_PCT,
    VOLUME_TOLERANCE_PCT,
    BhavcopyUnavailable,
    fetch_bhavcopy,
    parse_bhavcopy,
    price_diff_pct,
    prices_match,
    volume_diff_pct,
    volumes_match,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sec_bhavdata_full_sample.csv"


class ParseBhavcopyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = parse_bhavcopy(FIXTURE.read_text(encoding="utf-8"))

    def test_only_eq_series_is_kept(self) -> None:
        """BE / other series are not comparable equity bars."""
        self.assertIn("IDEA", self.rows)
        self.assertNotIn("NOTEQ", self.rows)

    def test_values_are_parsed(self) -> None:
        row = self.rows["IDEA"]
        self.assertEqual(row.close, 15.19)
        self.assertEqual(row.volume, 1534470198)
        self.assertEqual(row.delivery_pct, 28.34)

    def test_absent_delivery_is_none_not_zero(self) -> None:
        """NSE writes '-' when delivery is unavailable. Absent is not zero."""
        row = self.rows["NODELIV"]
        self.assertIsNone(row.delivery_qty)
        self.assertIsNone(row.delivery_pct)

    def test_empty_input_yields_nothing(self) -> None:
        self.assertEqual(parse_bhavcopy(""), {})


class VolumeToleranceTestCase(unittest.TestCase):
    """Volume needs no dividend adjustment, so it is the primary signal."""

    def test_the_real_error_is_caught(self) -> None:
        """IDEA.NS 2026-08-25: stored 460,728,374 vs published 1,534,470,198."""
        self.assertFalse(volumes_match(460_728_374, 1_534_470_198))
        self.assertAlmostEqual(volume_diff_pct(460_728_374, 1_534_470_198), 69.97, places=1)

    def test_measured_noise_ceiling_is_tolerated(self) -> None:
        """The largest non-error difference observed was 0.5042%."""
        published = 1_000_000.0
        self.assertTrue(volumes_match(published * 1.005042, published))

    def test_threshold_sits_between_signal_and_noise(self) -> None:
        self.assertGreater(VOLUME_TOLERANCE_PCT, 0.5042)   # above measured noise
        self.assertLess(VOLUME_TOLERANCE_PCT, 69.97)       # below the real error

    def test_missing_volume_is_not_a_match(self) -> None:
        self.assertFalse(volumes_match(None, 1000.0))
        self.assertFalse(volumes_match(1000.0, None))
        self.assertFalse(volumes_match(1000.0, 0))


class PriceToleranceTestCase(unittest.TestCase):
    """Stored closes are dividend-adjusted; published closes are raw."""

    def test_measured_dividend_step_is_tolerated(self) -> None:
        """HAL.NS sat at a 0.99798 ratio - a 0.202% step - across its ex-date."""
        published = 4641.70
        self.assertTrue(prices_match(published * 0.99798, published))

    def test_a_genuine_break_is_caught(self) -> None:
        published = 2255.0
        self.assertFalse(prices_match(published * 0.95, published))

    def test_threshold_clears_the_measured_step_with_margin(self) -> None:
        self.assertGreater(PRICE_TOLERANCE_PCT, 0.202)
        self.assertGreaterEqual(PRICE_TOLERANCE_PCT / 0.202, 4.0)

    def test_large_dividend_exceeds_the_tolerance_by_design(self) -> None:
        """A dividend above 1% of price flags once on the ex-date.

        That is a company-action signal to reconcile, not data corruption. The
        limit is documented rather than hidden behind a wider tolerance.
        """
        published = 100.0
        self.assertFalse(prices_match(published * 0.97, published))

    def test_diff_helper_reports_magnitude(self) -> None:
        self.assertAlmostEqual(price_diff_pct(99.0, 100.0), 1.0, places=6)


class FetchBhavcopyTestCase(unittest.TestCase):
    """The network path. The offline suite must never reach NSE."""

    def test_non_bhavcopy_response_is_unavailable(self) -> None:
        """NSE returns an HTML error page on non-trading days, not a 404."""
        class _Response:
            def read(self):
                return b"<html>Not found</html>"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with patch("urllib.request.urlopen", return_value=_Response()):
            with self.assertRaises(BhavcopyUnavailable):
                fetch_bhavcopy(date(2026, 8, 30))

    def test_network_failure_is_unavailable_not_a_crash(self) -> None:
        with patch("urllib.request.urlopen", side_effect=OSError("dns")):
            with self.assertRaises(BhavcopyUnavailable):
                fetch_bhavcopy(date(2026, 8, 25))

    def test_successful_fetch_parses(self) -> None:
        payload = FIXTURE.read_bytes()

        class _Response:
            def read(self):
                return payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with patch("urllib.request.urlopen", return_value=_Response()):
            rows = fetch_bhavcopy(date(2026, 8, 25))
        self.assertEqual(rows["IDEA"].volume, 1534470198)


if __name__ == "__main__":
    unittest.main()
