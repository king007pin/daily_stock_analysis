# -*- coding: utf-8 -*-
"""Tests for src/services/nse_universe_discovery.py - NSELive mocked, offline."""

import unittest
from unittest.mock import MagicMock

from src.services.nse_universe_discovery import (
    discovered_symbols,
    fetch_nse_universe_snapshot,
)

_SAMPLE_RESPONSE = {
    "topGainers": [
        {"symbol": "SUBEXLTD", "lastPrice": 16.49, "pchange": 16.87, "totalTradedVolume": 44677415, "totalTradedValue": 731369283.55},
        {"symbol": "KAMANWALA", "lastPrice": 16.26, "pchange": 20.0, "totalTradedVolume": 4545, "totalTradedValue": 73856.25},
    ],
    "topLoosers": [
        {"symbol": "DISHTV", "lastPrice": 2.6, "pchange": -8.1, "totalTradedVolume": 900000, "totalTradedValue": 2340000.0},
    ],
    "mostActiveValue": [],
    "mostActiveVolume": [],
    "volumeSpurtsValue": [],
    "timestamp": "18-Aug-2026 16:00",
}


class TestFetchNseUniverseSnapshot(unittest.TestCase):
    def test_none_on_fetch_exception(self):
        nse_live = MagicMock()
        nse_live.top_stocks.side_effect = RuntimeError("connection refused")
        self.assertIsNone(fetch_nse_universe_snapshot(nse_live=nse_live))

    def test_none_on_unexpected_response_type(self):
        nse_live = MagicMock()
        nse_live.top_stocks.return_value = "not a dict"
        self.assertIsNone(fetch_nse_universe_snapshot(nse_live=nse_live))

    def test_real_gainers_parsed_and_typo_normalized(self):
        nse_live = MagicMock()
        nse_live.top_stocks.return_value = _SAMPLE_RESPONSE
        snapshot = fetch_nse_universe_snapshot(nse_live=nse_live)

        self.assertIsNotNone(snapshot)
        gainers = snapshot.get("top_gainers")
        self.assertEqual(len(gainers), 2)
        self.assertEqual(gainers[0].symbol, "SUBEXLTD")
        self.assertEqual(gainers[0].ns_symbol, "SUBEXLTD.NS")
        self.assertAlmostEqual(gainers[0].pchange, 16.87)

        # topLoosers (NSE's typo) must map to our "top_losers" key.
        losers = snapshot.get("top_losers")
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0].symbol, "DISHTV")

    def test_empty_category_stays_empty_not_fabricated(self):
        nse_live = MagicMock()
        nse_live.top_stocks.return_value = _SAMPLE_RESPONSE
        snapshot = fetch_nse_universe_snapshot(nse_live=nse_live)
        self.assertEqual(snapshot.get("most_active_by_value"), [])

    def test_malformed_entry_is_skipped_not_crashed_on(self):
        nse_live = MagicMock()
        nse_live.top_stocks.return_value = {
            "topGainers": [{"symbol": "GOOD", "lastPrice": 10.0, "pchange": 5.0, "totalTradedVolume": 100, "totalTradedValue": 1000.0}, {"series": "EQ"}],
            "topLoosers": [], "mostActiveValue": [], "mostActiveVolume": [], "volumeSpurtsValue": [],
            "timestamp": "x",
        }
        snapshot = fetch_nse_universe_snapshot(nse_live=nse_live)
        self.assertEqual(len(snapshot.get("top_gainers")), 1)
        self.assertEqual(snapshot.get("top_gainers")[0].symbol, "GOOD")


class TestDiscoveredSymbols(unittest.TestCase):
    def test_dedupes_across_categories(self):
        nse_live = MagicMock()
        nse_live.top_stocks.return_value = {
            "topGainers": [{"symbol": "AAA", "lastPrice": 10.0, "pchange": 5.0, "totalTradedVolume": 1, "totalTradedValue": 1}],
            "topLoosers": [], "mostActiveValue": [{"symbol": "AAA", "lastPrice": 10.0, "pchange": 5.0, "totalTradedVolume": 1, "totalTradedValue": 1}],
            "mostActiveVolume": [], "volumeSpurtsValue": [], "timestamp": "x",
        }
        snapshot = fetch_nse_universe_snapshot(nse_live=nse_live)
        symbols = discovered_symbols(snapshot, categories=["top_gainers", "most_active_by_value"])
        self.assertEqual(symbols, ["AAA.NS"])

    def test_respects_top_n_per_category(self):
        nse_live = MagicMock()
        nse_live.top_stocks.return_value = {
            "topGainers": [{"symbol": f"S{i}", "lastPrice": 10.0, "pchange": 1.0, "totalTradedVolume": 1, "totalTradedValue": 1} for i in range(20)],
            "topLoosers": [], "mostActiveValue": [], "mostActiveVolume": [], "volumeSpurtsValue": [], "timestamp": "x",
        }
        snapshot = fetch_nse_universe_snapshot(nse_live=nse_live)
        symbols = discovered_symbols(snapshot, categories=["top_gainers"], top_n_per_category=3)
        self.assertEqual(len(symbols), 3)


if __name__ == "__main__":
    unittest.main()
