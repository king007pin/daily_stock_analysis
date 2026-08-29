# -*- coding: utf-8 -*-
"""
Regression tests for India (NSE) realtime quote routing — Phase 04.

Before this fix, India realtime quotes routed to YfinanceFetcher only, with
no fallback at all if it failed (same single-point-of-failure pattern
already fixed for daily bars in Phase 00). This also verifies the
jp/kr/tw branch split out from `is_in` in the same edit is unaffected.
"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

if "litellm" not in sys.modules:
    sys.modules["litellm"] = MagicMock()
if "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote


class _DummyFetcher:
    def __init__(self, name: str, priority: int, result=None):
        self.name = name
        self.priority = priority
        self.result = result
        self.calls = []

    def get_realtime_quote(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class TestIndiaRealtimeRouting(unittest.TestCase):
    @patch("src.config.get_config")
    def test_jugaad_supplements_depth_when_yfinance_succeeds(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(enable_realtime_quote=True)

        yfinance_quote = UnifiedRealtimeQuote(
            code="RELIANCE.NS", price=1316.0, source=RealtimeSource.FALLBACK,
        )
        jugaad_quote = UnifiedRealtimeQuote(
            code="RELIANCE.NS", price=1316.0, source=RealtimeSource.JUGAAD_NSE,
            ask_price=1316.0, ask_qty=4751,
        )
        yfinance = _DummyFetcher("YfinanceFetcher", 4, result=yfinance_quote)
        jugaad = _DummyFetcher("JugaadDataFetcher", 6, result=jugaad_quote)

        manager = DataFetcherManager(fetchers=[yfinance, jugaad])
        quote = manager.get_realtime_quote("RELIANCE.NS")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, RealtimeSource.FALLBACK)  # primary stays Yfinance
        self.assertEqual(quote.ask_price, 1316.0)  # supplemented from jugaad
        self.assertEqual(quote.ask_qty, 4751)
        self.assertEqual(len(yfinance.calls), 1)
        self.assertEqual(len(jugaad.calls), 1)

    @patch("src.config.get_config")
    def test_jugaad_becomes_sole_source_when_yfinance_fails(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(enable_realtime_quote=True)

        jugaad_quote = UnifiedRealtimeQuote(
            code="RELIANCE.NS", price=1316.0, source=RealtimeSource.JUGAAD_NSE,
        )
        yfinance = _DummyFetcher("YfinanceFetcher", 4, result=None)
        jugaad = _DummyFetcher("JugaadDataFetcher", 6, result=jugaad_quote)

        manager = DataFetcherManager(fetchers=[yfinance, jugaad])
        quote = manager.get_realtime_quote("RELIANCE.NS")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, RealtimeSource.JUGAAD_NSE)

    @patch("src.config.get_config")
    def test_returns_none_when_both_sources_fail(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(enable_realtime_quote=True)

        yfinance = _DummyFetcher("YfinanceFetcher", 4, result=None)
        jugaad = _DummyFetcher("JugaadDataFetcher", 6, result=None)

        manager = DataFetcherManager(fetchers=[yfinance, jugaad])
        quote = manager.get_realtime_quote("RELIANCE.NS")

        self.assertIsNone(quote)

    @patch("src.config.get_config")
    def test_tw_routing_unaffected_by_india_branch_split(self, mock_get_config):
        """Regression guard: splitting `is_in` out of the jp/kr/tw block must not
        change tw/jp/kr behavior — Yfinance-only, no jugaad involvement."""
        mock_get_config.return_value = SimpleNamespace(enable_realtime_quote=True)

        tw_quote = UnifiedRealtimeQuote(code="2330.TW", price=1200.0, source=RealtimeSource.FALLBACK)
        yfinance = _DummyFetcher("YfinanceFetcher", 4, result=tw_quote)
        jugaad = _DummyFetcher("JugaadDataFetcher", 6, result=None)

        manager = DataFetcherManager(fetchers=[yfinance, jugaad])
        quote = manager.get_realtime_quote("2330.TW")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.price, 1200.0)
        self.assertEqual(jugaad.calls, [])  # jugaad must never be tried for tw


if __name__ == "__main__":
    unittest.main()
