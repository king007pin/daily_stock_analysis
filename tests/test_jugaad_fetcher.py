# -*- coding: utf-8 -*-
"""
JugaadDataFetcher offline unit tests.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class TestJugaadDataFetcherNormalize(unittest.TestCase):
    """Test _normalize_data with raw jugaad-data stock_df response shape."""

    def setUp(self):
        from data_provider.jugaad_fetcher import JugaadDataFetcher
        self.fetcher = JugaadDataFetcher()

    def test_normalize_stock_df_columns(self):
        import pandas as pd
        raw = pd.DataFrame({
            'DATE': pd.to_datetime(['2026-08-10', '2026-08-11', '2026-08-12']),
            'OPEN': [1326.6, 1323.9, 1327.8],
            'HIGH': [1328.7, 1329.0, 1327.8],
            'LOW': [1314.1, 1309.2, 1307.2],
            'CLOSE': [1323.9, 1329.0, 1317.0],
            'VOLUME': [9953124, 7901012, 9397301],
            'VALUE': [1.316377e10, 1.040034e10, 1.234655e10],
        })
        result = self.fetcher._normalize_data(raw, 'RELIANCE.NS')
        self.assertIn('date', result.columns)
        self.assertIn('close', result.columns)
        self.assertIn('amount', result.columns)
        self.assertAlmostEqual(result.iloc[0]['close'], 1323.9)
        self.assertEqual(result.iloc[0]['code'], 'RELIANCE.NS')

    def test_normalize_calculates_pct_chg(self):
        import pandas as pd
        raw = pd.DataFrame({
            'DATE': pd.to_datetime(['2026-08-10', '2026-08-11']),
            'OPEN': [100.0, 102.0],
            'HIGH': [101.0, 103.0],
            'LOW': [99.0, 101.0],
            'CLOSE': [100.0, 102.0],
            'VOLUME': [1000, 1200],
            'VALUE': [100000.0, 122400.0],
        })
        result = self.fetcher._normalize_data(raw, 'RELIANCE.NS')
        self.assertAlmostEqual(result.iloc[1]['pct_chg'], 2.0)

    def test_normalize_empty_df(self):
        import pandas as pd
        raw = pd.DataFrame()
        result = self.fetcher._normalize_data(raw, 'RELIANCE.NS')
        self.assertTrue(result.empty)


class TestJugaadDataFetcherFetchRaw(unittest.TestCase):
    """Test _fetch_raw_data with mocked jugaad_data.nse.stock_df."""

    def setUp(self):
        from data_provider.jugaad_fetcher import JugaadDataFetcher
        self.fetcher = JugaadDataFetcher()

    @patch('jugaad_data.nse.stock_df')
    def test_fetch_raw_success(self, mock_stock_df):
        import pandas as pd
        mock_stock_df.return_value = pd.DataFrame({
            'DATE': pd.to_datetime(['2026-08-10']),
            'OPEN': [1326.6], 'HIGH': [1328.7], 'LOW': [1314.1],
            'CLOSE': [1323.9], 'VOLUME': [9953124], 'VALUE': [1.3e10],
        })
        df = self.fetcher._fetch_raw_data('RELIANCE.NS', '2026-08-01', '2026-08-10')
        self.assertFalse(df.empty)
        mock_stock_df.assert_called_once()
        _, kwargs = mock_stock_df.call_args
        self.assertEqual(kwargs['symbol'], 'RELIANCE')

    def test_fetch_raw_rejects_non_ns_code(self):
        from data_provider.base import DataFetchError
        with self.assertRaises(DataFetchError):
            self.fetcher._fetch_raw_data('TCS.BO', '2026-08-01', '2026-08-10')

    def test_fetch_raw_rejects_bare_cn_code(self):
        from data_provider.base import DataFetchError
        with self.assertRaises(DataFetchError):
            self.fetcher._fetch_raw_data('600519', '2026-08-01', '2026-08-10')

    @patch('jugaad_data.nse.stock_df')
    def test_fetch_raw_empty_response(self, mock_stock_df):
        import pandas as pd
        from data_provider.base import DataFetchError
        mock_stock_df.return_value = pd.DataFrame()
        with self.assertRaises(DataFetchError):
            self.fetcher._fetch_raw_data('RELIANCE.NS', '2026-08-01', '2026-08-10')

    @patch('jugaad_data.nse.stock_df')
    def test_fetch_raw_upstream_exception(self, mock_stock_df):
        from data_provider.base import DataFetchError
        mock_stock_df.side_effect = Exception("connection timeout")
        with self.assertRaises(DataFetchError):
            self.fetcher._fetch_raw_data('RELIANCE.NS', '2026-08-01', '2026-08-10')


_SAMPLE_QUOTE_RESPONSE = {
    "orderBook": {
        "buyPrice1": 0, "buyQuantity1": 0,
        "sellPrice1": 1316.0, "sellQuantity1": 4751,
        "lastPrice": 1316.0,
    },
    "metaData": {
        "companyName": "Reliance Industries Limited",
        "open": 1314.0, "dayHigh": 1320.8, "dayLow": 1298.1,
        "previousClose": 1310.0, "change": 6.0, "pChange": 0.46,
    },
    "tradeInfo": {"totalTradedVolume": 13383867, "totalTradedValue": 17514396033.54},
}


class TestJugaadDataFetcherRealtimeQuote(unittest.TestCase):
    """Test get_realtime_quote with a mocked NSELive.stock_quote response."""

    def setUp(self):
        from data_provider.jugaad_fetcher import JugaadDataFetcher
        self.fetcher = JugaadDataFetcher()

    def test_maps_fields_from_order_book_not_price_info(self):
        from data_provider.realtime_types import RealtimeSource
        self.fetcher._get_nse_live = lambda: type(
            "Stub", (), {"stock_quote": staticmethod(lambda symbol: _SAMPLE_QUOTE_RESPONSE)}
        )()
        quote = self.fetcher.get_realtime_quote("RELIANCE.NS")
        self.assertIsNotNone(quote)
        self.assertEqual(quote.price, 1316.0)
        self.assertEqual(quote.name, "Reliance Industries Limited")
        self.assertEqual(quote.source, RealtimeSource.JUGAAD_NSE)
        self.assertEqual(quote.open_price, 1314.0)
        self.assertEqual(quote.high, 1320.8)
        self.assertEqual(quote.low, 1298.1)
        self.assertEqual(quote.pre_close, 1310.0)
        self.assertEqual(quote.change_pct, 0.46)
        self.assertEqual(quote.volume, 13383867)

    def test_empty_order_book_side_maps_to_none_not_zero(self):
        """NSELive returns 0 for an empty book side — must not be reported as a real ₹0 bid."""
        self.fetcher._get_nse_live = lambda: type(
            "Stub", (), {"stock_quote": staticmethod(lambda symbol: _SAMPLE_QUOTE_RESPONSE)}
        )()
        quote = self.fetcher.get_realtime_quote("RELIANCE.NS")
        self.assertIsNone(quote.bid_price)
        self.assertIsNone(quote.bid_qty)
        self.assertEqual(quote.ask_price, 1316.0)
        self.assertEqual(quote.ask_qty, 4751)

    def test_negative_change_pct_preserved_not_nulled(self):
        response = {
            **_SAMPLE_QUOTE_RESPONSE,
            "metaData": {**_SAMPLE_QUOTE_RESPONSE["metaData"], "pChange": -1.11, "change": -14.0},
        }
        self.fetcher._get_nse_live = lambda: type(
            "Stub", (), {"stock_quote": staticmethod(lambda symbol: response)}
        )()
        quote = self.fetcher.get_realtime_quote("RELIANCE.NS")
        self.assertEqual(quote.change_pct, -1.11)
        self.assertEqual(quote.change_amount, -14.0)

    def test_rejects_bse_code(self):
        quote = self.fetcher.get_realtime_quote("TCS.BO")
        self.assertIsNone(quote)

    def test_returns_none_on_upstream_exception(self):
        def raise_error(symbol):
            raise RuntimeError("network down")
        self.fetcher._get_nse_live = lambda: type(
            "Stub", (), {"stock_quote": staticmethod(raise_error)}
        )()
        quote = self.fetcher.get_realtime_quote("RELIANCE.NS")
        self.assertIsNone(quote)

    def test_returns_none_when_last_price_missing(self):
        response = {"orderBook": {"lastPrice": 0}, "metaData": {}, "tradeInfo": {}}
        self.fetcher._get_nse_live = lambda: type(
            "Stub", (), {"stock_quote": staticmethod(lambda symbol: response)}
        )()
        quote = self.fetcher.get_realtime_quote("RELIANCE.NS")
        self.assertIsNone(quote)


class TestJugaadDataFetcherRegistration(unittest.TestCase):
    """JugaadDataFetcher needs no credentials, so it should always be registered."""

    def test_always_registered(self):
        from data_provider.base import DataFetcherManager
        mgr = DataFetcherManager()
        names = [f.name for f in mgr._get_fetchers_snapshot()]
        self.assertIn('JugaadDataFetcher', names)

    def test_priority_after_yfinance(self):
        """Fallback ordering: YfinanceFetcher stays primary for NSE, JugaadDataFetcher is the fallback."""
        from data_provider.base import DataFetcherManager
        mgr = DataFetcherManager()
        names = [f.name for f in mgr._get_fetchers_snapshot()]
        self.assertLess(names.index('YfinanceFetcher'), names.index('JugaadDataFetcher'))

    def test_supports_in_market_only(self):
        from data_provider.base import DataFetcherManager
        self.assertEqual(
            DataFetcherManager._DAILY_MARKET_FETCHER_SUPPORT.get('JugaadDataFetcher'),
            {'in'},
        )


if __name__ == '__main__':
    unittest.main()
