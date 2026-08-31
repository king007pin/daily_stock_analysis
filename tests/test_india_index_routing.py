# -*- coding: utf-8 -*-
"""Indian index symbols must route to the Indian data path.

``^NSEI`` / ``^BSESN`` carry no ``.NS`` / ``.BO`` suffix and are not US indices,
so before 2026-09-01 they matched no market rule at all. Two consequences, both
silent:

1. ``DataFetcherManager`` fell through to the ``cn`` default and asked baostock
   (an A-share source) for the Nifty.
2. ``YfinanceFetcher._convert_stock_code`` could not identify the market and
   appended ``.SZ`` — ``^NSEI`` became ``^NSEI.SZ``, which Yahoo has never heard of.

The benchmark leg therefore had no data for India, which is why
``benchmark_return_service`` shipped inert and unverified.
"""

import unittest

from data_provider.base import IN_INDEX_CODES, _is_in_market, _is_us_market
from data_provider.yfinance_fetcher import YfinanceFetcher


class IndiaIndexMarketDetectionTestCase(unittest.TestCase):
    def test_indian_index_codes_are_indian(self) -> None:
        for code in ("^NSEI", "^BSESN"):
            with self.subTest(code=code):
                self.assertTrue(_is_in_market(code))

    def test_indian_index_codes_are_not_us(self) -> None:
        """Must not be mistaken for a US index, which has its own mapping."""
        for code in ("^NSEI", "^BSESN"):
            with self.subTest(code=code):
                self.assertFalse(_is_us_market(code))

    def test_us_index_is_untouched(self) -> None:
        self.assertFalse(_is_in_market("^GSPC"))
        self.assertTrue(_is_us_market("^GSPC"))

    def test_suffix_symbols_still_detected(self) -> None:
        self.assertTrue(_is_in_market("IDEA.NS"))
        self.assertTrue(_is_in_market("500325.BO"))

    def test_a_share_code_is_not_indian(self) -> None:
        self.assertFalse(_is_in_market("600519"))

    def test_detection_is_case_insensitive(self) -> None:
        self.assertTrue(_is_in_market("^nsei"))


class IndiaIndexSymbolConversionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fetcher = YfinanceFetcher()

    def test_indian_index_passes_through_unchanged(self) -> None:
        """The regression: ^NSEI must not acquire a .SZ suffix."""
        for code in sorted(IN_INDEX_CODES):
            with self.subTest(code=code):
                converted = self.fetcher._convert_stock_code(code)
                self.assertEqual(converted, code)
                self.assertNotIn(".SZ", converted)
                self.assertNotIn(".SS", converted)

    def test_us_index_still_converts(self) -> None:
        self.assertEqual(self.fetcher._convert_stock_code("^GSPC"), "^GSPC")

    def test_indian_equity_suffix_unchanged(self) -> None:
        self.assertEqual(self.fetcher._convert_stock_code("IDEA.NS"), "IDEA.NS")

    def test_a_share_still_gets_exchange_suffix(self) -> None:
        self.assertEqual(self.fetcher._convert_stock_code("600519"), "600519.SS")


if __name__ == "__main__":
    unittest.main()
