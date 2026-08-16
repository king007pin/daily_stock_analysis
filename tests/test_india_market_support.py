# -*- coding: utf-8 -*-
"""Comprehensive unit tests for Indian Stock Market (NSE & BSE) integration."""

from datetime import date, datetime
import unittest
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

from src.services.market_symbol_utils import (
    get_suffix_market,
    is_in_suffix_symbol,
    is_suffix_market_symbol,
    normalize_suffix_market_symbol,
)
from src.services.stock_code_utils import (
    is_code_like,
    normalize_code,
    _normalize_code_and_exchange,
)
from src.services.stock_list_parser import (
    parse_analysis_target,
    ParseStatus,
)
from src.market_context import (
    detect_market,
    get_market_role,
    get_market_guidelines,
)
from src.core.trading_calendar import (
    get_market_for_stock,
    MARKET_TIMEZONE,
    MARKET_EXCHANGE,
    _CLOSING_AUCTION_WINDOW_MINUTES,
    compute_effective_region,
    is_market_open,
)
from src.core.market_profile import (
    get_profile,
    IN_PROFILE,
)
from data_provider.yfinance_fetcher import YfinanceFetcher
from src.search_service import SearchService


class TestIndiaMarketSymbolUtils(unittest.TestCase):
    """Test market symbol and suffix utilities for Indian tickers."""

    def test_nse_alphanumeric_tickers(self):
        symbols = [
            "RELIANCE.NS",
            "TCS.NS",
            "HDFCBANK.NS",
            "INFY.NS",
            "3MINDIA.NS",
            "M&M.NS",
            "BAJAJ-AUTO.NS",
            "LT.NS",
        ]
        for sym in symbols:
            with self.subTest(symbol=sym):
                self.assertEqual(get_suffix_market(sym), "in")
                self.assertTrue(is_in_suffix_symbol(sym))
                self.assertTrue(is_suffix_market_symbol(sym, "in"))

    def test_bse_numeric_and_text_tickers(self):
        symbols = [
            "500325.BO",
            "532540.BO",
            "RELIANCE.BO",
            "TCS.BO",
        ]
        for sym in symbols:
            with self.subTest(symbol=sym):
                self.assertEqual(get_suffix_market(sym), "in")
                self.assertTrue(is_in_suffix_symbol(sym))
                self.assertTrue(is_suffix_market_symbol(sym, "in"))

    def test_case_insensitivity_and_normalization(self):
        self.assertEqual(get_suffix_market("reliance.ns"), "in")
        self.assertEqual(normalize_suffix_market_symbol("reliance.ns"), "RELIANCE.NS")
        self.assertEqual(normalize_suffix_market_symbol("500325.bo"), "500325.BO")

    def test_invalid_indian_symbols(self):
        self.assertIsNone(get_suffix_market("RELIANCE"))
        self.assertIsNone(get_suffix_market("500325"))
        self.assertIsNone(get_suffix_market(".NS"))
        self.assertIsNone(get_suffix_market("RELIANCE.XYZ"))


class TestIndiaStockCodeUtils(unittest.TestCase):
    """Test stock code normalizer and classification for Indian tickers."""

    def test_is_code_like(self):
        self.assertTrue(is_code_like("RELIANCE.NS"))
        self.assertTrue(is_code_like("TCS.NS"))
        self.assertTrue(is_code_like("500325.BO"))
        self.assertTrue(is_code_like("M&M.NS"))

    def test_normalize_code_and_exchange(self):
        code, ex = _normalize_code_and_exchange("RELIANCE.NS")
        self.assertEqual(code, "RELIANCE.NS")
        self.assertEqual(normalize_code("RELIANCE.NS"), "RELIANCE.NS")


class TestIndiaStockListParser(unittest.TestCase):
    """Test structured analysis-target parser for Indian tickers."""

    def test_parse_nse_target(self):
        target = parse_analysis_target("RELIANCE.NS")
        self.assertEqual(target.asset_type, ParseStatus.STOCK)
        self.assertEqual(target.canonical_id, "RELIANCE.NS")
        self.assertEqual(target.display_code, "RELIANCE.NS")
        self.assertEqual(target.exchange, "NS")

    def test_parse_bse_target(self):
        target = parse_analysis_target("500325.BO")
        self.assertEqual(target.asset_type, ParseStatus.STOCK)
        self.assertEqual(target.canonical_id, "500325.BO")
        self.assertEqual(target.display_code, "500325.BO")
        self.assertEqual(target.exchange, "BO")


class TestIndiaMarketContext(unittest.TestCase):
    """Test LLM prompt context and regulatory guidelines for Indian market."""

    def test_market_detection(self):
        self.assertEqual(detect_market("RELIANCE.NS"), "in")
        self.assertEqual(detect_market("500325.BO"), "in")

    def test_market_role(self):
        self.assertEqual(get_market_role("RELIANCE.NS", lang="en"), "Indian stock")
        self.assertEqual(get_market_role("RELIANCE.NS", lang="zh"), "印度股票")

    def test_market_guidelines_content(self):
        guidelines_en = get_market_guidelines("RELIANCE.NS", lang="en")
        self.assertIn("SEBI", guidelines_en)
        self.assertIn("FII", guidelines_en)
        self.assertIn("DII", guidelines_en)
        self.assertIn("RBI", guidelines_en)
        self.assertIn("circuit", guidelines_en.lower())

        guidelines_zh = get_market_guidelines("500325.BO", lang="zh")
        self.assertIn("SEBI", guidelines_zh)
        self.assertIn("印度卢比", guidelines_zh)
        self.assertIn("FII", guidelines_zh)


class TestIndiaTradingCalendar(unittest.TestCase):
    """Test timezone, exchange-calendar codes, and trading phases for India."""

    def test_market_for_stock(self):
        self.assertEqual(get_market_for_stock("RELIANCE.NS"), "in")
        self.assertEqual(get_market_for_stock("500325.BO"), "in")

    def test_calendar_metadata(self):
        self.assertEqual(MARKET_TIMEZONE["in"], "Asia/Kolkata")
        self.assertEqual(MARKET_EXCHANGE["in"], "XBOM")
        self.assertEqual(_CLOSING_AUCTION_WINDOW_MINUTES["in"], 10)

    def test_compute_effective_region_india(self):
        self.assertEqual(
            compute_effective_region("in", {"in", "us"}),
            "in",
        )
        self.assertEqual(
            compute_effective_region("in", {"us"}),
            "",
        )
        self.assertEqual(
            compute_effective_region("cn,in", {"cn", "in"}),
            "cn,in",
        )


class TestIndiaMarketProfile(unittest.TestCase):
    """Test market review profile for Indian indices."""

    def test_profile_lookup(self):
        profile = get_profile("in")
        self.assertEqual(profile.region, "in")
        self.assertEqual(profile.mood_index_code, "^NSEI")
        self.assertFalse(profile.has_market_stats)
        self.assertTrue(len(profile.news_queries) > 0)


class TestIndiaYfinanceFetcher(unittest.TestCase):
    """Test code conversion and suffix dispatch in YfinanceFetcher."""

    def test_code_conversion(self):
        fetcher = YfinanceFetcher()
        self.assertEqual(fetcher._convert_stock_code("RELIANCE.NS"), "RELIANCE.NS")
        self.assertEqual(fetcher._convert_stock_code("500325.BO"), "500325.BO")
        self.assertEqual(fetcher._convert_stock_code("TCS.NS"), "TCS.NS")

    def test_is_in_suffix_stock(self):
        self.assertTrue(YfinanceFetcher._is_in_suffix_stock("RELIANCE.NS"))
        self.assertTrue(YfinanceFetcher._is_in_suffix_stock("500325.BO"))
        self.assertFalse(YfinanceFetcher._is_in_suffix_stock("AAPL"))
        self.assertFalse(YfinanceFetcher._is_in_suffix_stock("600519"))


class TestIndiaSearchService(unittest.TestCase):
    """Test search service classification for Indian stocks."""

    def test_foreign_stock_classification(self):
        self.assertTrue(SearchService._is_foreign_stock("RELIANCE.NS"))
        self.assertTrue(SearchService._is_foreign_stock("500325.BO"))
        self.assertTrue(SearchService._is_foreign_stock("TCS.NS"))


class TestIndiaDataProviderRouting(unittest.TestCase):
    """Test market tagging and fetcher filtering for Indian stocks."""

    def test_market_tag(self):
        from data_provider.base import _market_tag, _is_in_market
        self.assertTrue(_is_in_market("RELIANCE.NS"))
        self.assertTrue(_is_in_market("500325.BO"))
        self.assertEqual(_market_tag("RELIANCE.NS"), "in")
        self.assertEqual(_market_tag("500325.BO"), "in")

    def test_daily_market_fetcher_filtering(self):
        from data_provider.base import DataFetcherManager, BaseFetcher
        
        f1 = MagicMock(spec=BaseFetcher)
        f1.name = "EfinanceFetcher"
        f2 = MagicMock(spec=BaseFetcher)
        f2.name = "AkshareFetcher"
        f3 = MagicMock(spec=BaseFetcher)
        f3.name = "BaostockFetcher"
        f4 = MagicMock(spec=BaseFetcher)
        f4.name = "YfinanceFetcher"

        fetchers = [f1, f2, f3, f4]
        filtered = DataFetcherManager._filter_daily_fetchers_for_market(fetchers, "in")
        names = [f.name for f in filtered]
        self.assertEqual(names, ["YfinanceFetcher"])


if __name__ == "__main__":
    unittest.main()
