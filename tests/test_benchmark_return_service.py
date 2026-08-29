# -*- coding: utf-8 -*-
"""Unit tests for benchmark-relative (excess) return measurement.

Fully offline: a fake ``DataFetcherManager``-shaped object is injected, so no
network call and no DB access happens. The fake mirrors the real contract
``get_daily_data(stock_code, start_date, end_date, days) -> (DataFrame, source)``
from ``data_provider/base.py``.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

import pandas as pd

from src.services.benchmark_return_service import (
    CONVENTION_CLOSE_TO_CLOSE,
    CONVENTION_INTRADAY_OPEN_TO_CLOSE,
    REASON_ANCHOR_BAR_MISSING,
    REASON_EMPTY_DATA,
    REASON_FETCH_FAILED,
    REASON_INSUFFICIENT_BARS,
    REASON_MISSING_SIGNAL_RETURN,
    REASON_NO_BENCHMARK,
    BenchmarkReturnService,
    excess_return_pct,
    get_benchmark_spec,
)


def _bars_frame(start: date, rows):
    """rows: list of (open, close). One calendar day per bar, starting at `start`."""
    data = []
    for offset, (open_px, close_px) in enumerate(rows):
        data.append(
            {
                "date": (start + timedelta(days=offset)).strftime("%Y-%m-%d"),
                "open": open_px,
                "high": max(open_px, close_px),
                "low": min(open_px, close_px),
                "close": close_px,
                "volume": 1000,
            }
        )
    return pd.DataFrame(data)


class _FakeFetcherManager:
    """Stands in for DataFetcherManager; records calls, returns canned frames."""

    def __init__(self, frame=None, source="FakeFetcher", error=None):
        self.frame = frame
        self.source = source
        self.error = error
        self.calls = []

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append(
            {"stock_code": stock_code, "start_date": start_date, "end_date": end_date, "days": days}
        )
        if self.error is not None:
            raise self.error
        return self.frame, self.source


class ExcessReturnArithmeticTestCase(unittest.TestCase):
    def test_positive_signal_lagging_positive_market_is_negative_excess(self):
        # +15% in a +20% market is underperformance - the whole point of this module.
        self.assertAlmostEqual(excess_return_pct(15.0, 20.0), -5.0)

    def test_both_negative_but_signal_outperforms(self):
        # signal -1%, benchmark -5% -> excess +4%
        self.assertAlmostEqual(excess_return_pct(-1.0, -5.0), 4.0)

    def test_matching_the_index_is_a_real_zero(self):
        self.assertEqual(excess_return_pct(3.5, 3.5), 0.0)

    def test_missing_benchmark_never_becomes_zero(self):
        result = excess_return_pct(4.2, None)
        self.assertIsNone(result)
        self.assertNotEqual(result, 0.0)

    def test_missing_signal_return_yields_none(self):
        self.assertIsNone(excess_return_pct(None, 1.0))

    def test_non_finite_inputs_yield_none(self):
        self.assertIsNone(excess_return_pct(float("nan"), 1.0))
        self.assertIsNone(excess_return_pct(1.0, float("inf")))


class BenchmarkSpecTestCase(unittest.TestCase):
    def test_default_benchmarks_match_repo_index_symbols(self):
        self.assertEqual(get_benchmark_spec("in").symbol, "^NSEI")  # Nifty 50
        self.assertEqual(get_benchmark_spec("us").symbol, "^GSPC")  # S&P 500
        self.assertEqual(get_benchmark_spec("cn").symbol, "sh000001")  # 上证指数

    def test_market_without_configured_benchmark(self):
        self.assertIsNone(get_benchmark_spec("hk"))
        self.assertIsNone(get_benchmark_spec("zz"))


class BenchmarkWindowReturnTestCase(unittest.TestCase):
    def test_close_to_close_over_eval_window(self):
        # anchor close 100 -> 3rd forward bar close 105 => +5%
        frame = _bars_frame(
            date(2024, 1, 2),
            [(99.0, 100.0), (100.0, 102.0), (102.0, 103.0), (103.0, 105.0), (105.0, 110.0)],
        )
        manager = _FakeFetcherManager(frame=frame)
        service = BenchmarkReturnService(fetcher_manager=manager)

        window = service.benchmark_return_pct("in", "2024-01-02", 3)

        self.assertEqual(window.benchmark_symbol, "^NSEI")
        self.assertEqual(window.convention, CONVENTION_CLOSE_TO_CLOSE)
        self.assertEqual(window.eval_window_days, 3)
        self.assertAlmostEqual(window.benchmark_return_pct, 5.0)
        self.assertEqual(window.start_price, 100.0)
        self.assertEqual(window.end_close, 105.0)
        self.assertEqual(window.end_date, "2024-01-05")
        self.assertIsNone(window.reason)
        self.assertTrue(window.is_available)
        # Reused fetcher contract: symbol + date range passed positionally/kw as
        # DataFetcherManager.get_daily_data declares them.
        self.assertEqual(manager.calls[0]["stock_code"], "^NSEI")
        self.assertEqual(manager.calls[0]["start_date"], "2024-01-02")

    def test_accepts_date_objects_and_datetime_dates_in_frame(self):
        frame = _bars_frame(date(2024, 1, 2), [(99.0, 100.0), (100.0, 96.0)])
        frame["date"] = pd.to_datetime(frame["date"])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        window = service.benchmark_return_pct("us", date(2024, 1, 2), 1)

        self.assertEqual(window.benchmark_symbol, "^GSPC")
        self.assertAlmostEqual(window.benchmark_return_pct, -4.0)

    def test_rejects_non_positive_window(self):
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=pd.DataFrame()))
        with self.assertRaises(ValueError):
            service.benchmark_return_pct("in", "2024-01-02", 0)

    def test_insufficient_forward_bars_is_explicit(self):
        frame = _bars_frame(date(2024, 1, 2), [(99.0, 100.0), (100.0, 101.0)])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        window = service.benchmark_return_pct("in", "2024-01-02", 3)

        self.assertIsNone(window.benchmark_return_pct)
        self.assertEqual(window.reason, REASON_INSUFFICIENT_BARS)
        self.assertFalse(window.is_available)

    def test_anchor_day_not_traded_is_explicit(self):
        frame = _bars_frame(date(2024, 1, 3), [(99.0, 100.0), (100.0, 101.0)])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        window = service.benchmark_return_pct("in", "2024-01-02", 1)

        self.assertIsNone(window.benchmark_return_pct)
        self.assertEqual(window.reason, REASON_ANCHOR_BAR_MISSING)

    def test_empty_frame_is_explicit(self):
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=pd.DataFrame()))

        window = service.benchmark_return_pct("cn", "2024-01-02", 1)

        self.assertIsNone(window.benchmark_return_pct)
        self.assertEqual(window.reason, REASON_EMPTY_DATA)

    def test_fetch_exception_is_caught_and_reported(self):
        manager = _FakeFetcherManager(error=RuntimeError("all data sources failed"))
        service = BenchmarkReturnService(fetcher_manager=manager)

        window = service.benchmark_return_pct("in", "2024-01-02", 1)

        self.assertIsNone(window.benchmark_return_pct)
        self.assertEqual(window.reason, REASON_FETCH_FAILED)

    def test_no_benchmark_configured_window(self):
        manager = _FakeFetcherManager(frame=_bars_frame(date(2024, 1, 2), [(1.0, 1.0)]))
        service = BenchmarkReturnService(fetcher_manager=manager)

        window = service.benchmark_return_pct("hk", "2024-01-02", 3)

        self.assertEqual(window.reason, REASON_NO_BENCHMARK)
        self.assertIsNone(window.benchmark_symbol)
        self.assertIsNone(window.benchmark_return_pct)
        self.assertEqual(manager.calls, [])  # never fetched a substitute index


class IntradayBenchmarkTestCase(unittest.TestCase):
    def test_intraday_uses_same_day_open_to_close(self):
        # Anchor bar: open 200 -> close 202 = +1%. Next day's close (220) must be
        # ignored; the intraday signal convention is a same-day square-off.
        frame = _bars_frame(date(2024, 1, 2), [(200.0, 202.0), (202.0, 220.0)])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        window = service.intraday_benchmark_return_pct("in", "2024-01-02")

        self.assertEqual(window.convention, CONVENTION_INTRADAY_OPEN_TO_CLOSE)
        self.assertEqual(window.eval_window_days, 1)
        self.assertAlmostEqual(window.benchmark_return_pct, 1.0)
        self.assertEqual(window.start_price, 200.0)
        self.assertEqual(window.end_close, 202.0)
        self.assertEqual(window.end_date, "2024-01-02")

    def test_intraday_differs_from_close_to_close_on_same_bars(self):
        frame = _bars_frame(date(2024, 1, 2), [(200.0, 202.0), (202.0, 220.0)])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        intraday = service.intraday_benchmark_return_pct("in", "2024-01-02")
        close_to_close = service.benchmark_return_pct("in", "2024-01-02", 1)

        self.assertAlmostEqual(intraday.benchmark_return_pct, 1.0)
        self.assertAlmostEqual(close_to_close.benchmark_return_pct, 8.910891)
        self.assertNotEqual(intraday.benchmark_return_pct, close_to_close.benchmark_return_pct)

    def test_intraday_missing_anchor_bar_is_explicit(self):
        frame = _bars_frame(date(2024, 1, 3), [(200.0, 202.0)])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        window = service.intraday_benchmark_return_pct("in", "2024-01-02")

        self.assertIsNone(window.benchmark_return_pct)
        self.assertEqual(window.reason, REASON_ANCHOR_BAR_MISSING)

    def test_intraday_no_benchmark_configured(self):
        manager = _FakeFetcherManager(frame=_bars_frame(date(2024, 1, 2), [(1.0, 1.0)]))
        service = BenchmarkReturnService(fetcher_manager=manager)

        window = service.intraday_benchmark_return_pct("jp", "2024-01-02")

        self.assertEqual(window.reason, REASON_NO_BENCHMARK)
        self.assertIsNone(window.benchmark_symbol)
        self.assertEqual(manager.calls, [])


class EvaluateExcessReturnTestCase(unittest.TestCase):
    def test_underperformance_in_a_rising_market(self):
        # Index +5% over the window; signal made +2% -> excess -3%.
        frame = _bars_frame(
            date(2024, 1, 2), [(99.0, 100.0), (100.0, 102.0), (102.0, 103.0), (103.0, 105.0)]
        )
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        result = service.evaluate_excess_return(
            "in", signal_return_pct=2.0, start_date="2024-01-02", eval_window_days=3
        )

        self.assertAlmostEqual(result.signal_return_pct, 2.0)
        self.assertAlmostEqual(result.benchmark_return_pct, 5.0)
        self.assertAlmostEqual(result.excess_return_pct, -3.0)
        self.assertEqual(result.benchmark_symbol, "^NSEI")
        self.assertEqual(result.eval_window_days, 3)
        self.assertIsNone(result.reason)
        self.assertTrue(result.is_benchmark_relative)

    def test_negative_signal_still_outperforms_falling_market(self):
        # Index -5% (100 -> 95); signal -1% -> excess +4%.
        frame = _bars_frame(date(2024, 1, 2), [(101.0, 100.0), (100.0, 95.0)])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        result = service.evaluate_excess_return(
            "in", signal_return_pct=-1.0, start_date="2024-01-02", eval_window_days=1
        )

        self.assertAlmostEqual(result.benchmark_return_pct, -5.0)
        self.assertAlmostEqual(result.excess_return_pct, 4.0)

    def test_intraday_evaluation_matches_signal_side_convention(self):
        # Signal side scores intraday as anchor open -> anchor close. Benchmark
        # must do the same: index open 200 -> close 202 = +1%; signal +1.5%.
        frame = _bars_frame(date(2024, 1, 2), [(200.0, 202.0), (202.0, 300.0)])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        result = service.evaluate_excess_return(
            "in", signal_return_pct=1.5, start_date="2024-01-02", intraday=True
        )

        self.assertEqual(result.convention, CONVENTION_INTRADAY_OPEN_TO_CLOSE)
        self.assertAlmostEqual(result.benchmark_return_pct, 1.0)
        self.assertAlmostEqual(result.excess_return_pct, 0.5)
        self.assertEqual(result.eval_window_days, 1)

    def test_missing_benchmark_data_yields_none_and_reason_not_zero(self):
        manager = _FakeFetcherManager(error=RuntimeError("all data sources failed"))
        service = BenchmarkReturnService(fetcher_manager=manager)

        result = service.evaluate_excess_return(
            "in", signal_return_pct=7.5, start_date="2024-01-02", eval_window_days=3
        )

        self.assertIsNone(result.benchmark_return_pct)
        self.assertIsNone(result.excess_return_pct)
        # Explicitly: unknown must NOT be reported as a flat market.
        self.assertNotEqual(result.benchmark_return_pct, 0.0)
        self.assertNotEqual(result.excess_return_pct, 0.0)
        self.assertEqual(result.reason, REASON_FETCH_FAILED)
        self.assertFalse(result.is_benchmark_relative)
        # The absolute leg is preserved and still reportable.
        self.assertAlmostEqual(result.signal_return_pct, 7.5)

    def test_no_benchmark_configured_result_is_explicit_and_not_zero(self):
        manager = _FakeFetcherManager(frame=_bars_frame(date(2024, 1, 2), [(1.0, 1.0)]))
        service = BenchmarkReturnService(fetcher_manager=manager)

        result = service.evaluate_excess_return(
            "hk", signal_return_pct=3.0, start_date="2024-01-02", eval_window_days=1
        )

        self.assertEqual(result.reason, REASON_NO_BENCHMARK)
        self.assertIsNone(result.benchmark_symbol)
        self.assertIsNone(result.benchmark_return_pct)
        self.assertIsNone(result.excess_return_pct)
        self.assertNotEqual(result.excess_return_pct, 0.0)
        self.assertEqual(manager.calls, [])

    def test_missing_signal_return_is_reported_separately(self):
        frame = _bars_frame(date(2024, 1, 2), [(99.0, 100.0), (100.0, 101.0)])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        result = service.evaluate_excess_return(
            "in", signal_return_pct=None, start_date="2024-01-02", eval_window_days=1
        )

        self.assertAlmostEqual(result.benchmark_return_pct, 1.0)
        self.assertIsNone(result.signal_return_pct)
        self.assertIsNone(result.excess_return_pct)
        self.assertEqual(result.reason, REASON_MISSING_SIGNAL_RETURN)

    def test_to_dict_carries_the_full_contract(self):
        frame = _bars_frame(date(2024, 1, 2), [(99.0, 100.0), (100.0, 101.0)])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        payload = service.evaluate_excess_return(
            "in", signal_return_pct=2.0, start_date="2024-01-02", eval_window_days=1
        ).to_dict()

        self.assertEqual(payload["market"], "in")
        self.assertEqual(payload["benchmark_symbol"], "^NSEI")
        self.assertEqual(payload["benchmark_name"], "Nifty 50")
        self.assertEqual(payload["convention"], CONVENTION_CLOSE_TO_CLOSE)
        self.assertEqual(payload["start_date"], "2024-01-02")
        self.assertEqual(payload["eval_window_days"], 1)
        self.assertAlmostEqual(payload["signal_return_pct"], 2.0)
        self.assertAlmostEqual(payload["benchmark_return_pct"], 1.0)
        self.assertAlmostEqual(payload["excess_return_pct"], 1.0)
        self.assertEqual(payload["benchmark_source"], "FakeFetcher")
        self.assertIsNone(payload["reason"])

    def test_window_result_to_dict(self):
        frame = _bars_frame(date(2024, 1, 2), [(99.0, 100.0), (100.0, 101.0)])
        service = BenchmarkReturnService(fetcher_manager=_FakeFetcherManager(frame=frame))

        payload = service.benchmark_return_pct("in", "2024-01-02", 1).to_dict()

        self.assertEqual(payload["benchmark_symbol"], "^NSEI")
        self.assertEqual(payload["end_date"], "2024-01-03")
        self.assertAlmostEqual(payload["benchmark_return_pct"], 1.0)
        self.assertIsNone(payload["reason"])


class NoNetworkGuardTestCase(unittest.TestCase):
    def test_real_fetcher_manager_is_never_constructed_without_a_fetch(self):
        # Constructing the service must not touch data_provider or the network.
        service = BenchmarkReturnService()
        self.assertIsNone(service._fetcher_manager)
        self.assertEqual(
            service.benchmark_return_pct("hk", "2024-01-02", 1).reason, REASON_NO_BENCHMARK
        )
        self.assertIsNone(service._fetcher_manager)


if __name__ == "__main__":
    unittest.main()
