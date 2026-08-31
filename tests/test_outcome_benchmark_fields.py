# -*- coding: utf-8 -*-
"""The outcome evaluator must record the benchmark leg alongside absolute return.

Absolute return cannot answer the question that matters for skill: did the call
beat the market it was taken in? +15% inside a +20% index is underperformance.

``benchmark_return_service`` was built on 2026-08-24 with zero callers. This wires
it into ``_evaluate_signal_horizon``. The new columns are supplementary — they do
not change what ``outcome`` or ``stock_return_pct`` mean — so no new
``engine_version`` is required.
"""

import datetime as dt
import unittest
from unittest.mock import MagicMock

from src.services.benchmark_return_service import ExcessReturnResult
from src.services.decision_signal_outcome_service import DecisionSignalOutcomeService


def _service(benchmark):
    svc = DecisionSignalOutcomeService.__new__(DecisionSignalOutcomeService)
    svc._benchmark_service = benchmark
    svc._benchmark_enabled = True
    return svc


class BenchmarkFieldsTestCase(unittest.TestCase):
    def test_excess_return_is_recorded(self) -> None:
        bench = MagicMock()
        bench.evaluate_excess_return.return_value = ExcessReturnResult(
            market="in", benchmark_symbol="^NSEI", benchmark_name="Nifty 50",
            convention="close_to_close", start_date="2026-08-24", eval_window_days=3,
            signal_return_pct=6.40, benchmark_return_pct=-0.53,
            excess_return_pct=6.93, benchmark_source="yfinance", reason=None,
        )
        fields = _service(bench)._benchmark_fields(
            market="in", signal_return_pct=6.40,
            anchor_date=dt.date(2026, 8, 24), eval_window_days=3, intraday=False,
        )
        self.assertEqual(fields["benchmark_symbol"], "^NSEI")
        self.assertAlmostEqual(fields["excess_return_pct"], 6.93)
        self.assertIsNone(fields["benchmark_reason"])

    def test_intraday_flag_is_forwarded(self) -> None:
        """An intraday signal must be compared against the index's own session."""
        bench = MagicMock()
        bench.evaluate_excess_return.return_value = ExcessReturnResult(
            market="in", benchmark_symbol="^NSEI", benchmark_name="Nifty 50",
            convention="open_to_close", start_date="2026-08-18", eval_window_days=1,
            signal_return_pct=-2.29, benchmark_return_pct=-0.28,
            excess_return_pct=-2.01, benchmark_source="yfinance", reason=None,
        )
        _service(bench)._benchmark_fields(
            market="in", signal_return_pct=-2.29,
            anchor_date=dt.date(2026, 8, 18), eval_window_days=1, intraday=True,
        )
        self.assertTrue(bench.evaluate_excess_return.call_args.kwargs["intraday"])

    def test_missing_benchmark_is_recorded_not_zeroed(self) -> None:
        """An absent benchmark must stay None. Missing is not the same as flat."""
        bench = MagicMock()
        bench.evaluate_excess_return.return_value = ExcessReturnResult(
            market="in", benchmark_symbol="^NSEI", benchmark_name="Nifty 50",
            convention="close_to_close", start_date="2026-08-24", eval_window_days=3,
            signal_return_pct=6.40, benchmark_return_pct=None,
            excess_return_pct=None, benchmark_source=None,
            reason="benchmark_data_unavailable",
        )
        fields = _service(bench)._benchmark_fields(
            market="in", signal_return_pct=6.40,
            anchor_date=dt.date(2026, 8, 24), eval_window_days=3, intraday=False,
        )
        self.assertIsNone(fields["benchmark_return_pct"])
        self.assertIsNone(fields["excess_return_pct"])
        self.assertEqual(fields["benchmark_reason"], "benchmark_data_unavailable")

    def test_benchmark_failure_never_blocks_scoring(self) -> None:
        """A raising benchmark service must not lose the absolute outcome."""
        bench = MagicMock()
        bench.evaluate_excess_return.side_effect = RuntimeError("network down")
        fields = _service(bench)._benchmark_fields(
            market="in", signal_return_pct=6.40,
            anchor_date=dt.date(2026, 8, 24), eval_window_days=3, intraday=False,
        )
        self.assertEqual(fields, {"benchmark_reason": "benchmark_evaluation_failed"})


class BenchmarkOptInTestCase(unittest.TestCase):
    """The benchmark leg fetches index data, so it must be opt-in.

    Regression: wiring it in unconditionally made the *offline* suite perform real
    network requests — measured at 2.9 seconds per scored signal. The same file
    already had the precedent three lines above (`_refill_enabled`), whose comment
    says network work is opt-in precisely so the offline suite stays offline.
    """

    def test_disabled_by_default(self) -> None:
        svc = DecisionSignalOutcomeService.__new__(DecisionSignalOutcomeService)
        svc._benchmark_enabled = False
        svc._benchmark_service = MagicMock()

        fields = svc._benchmark_fields(
            market="in", signal_return_pct=1.0,
            anchor_date=dt.date(2026, 8, 24), eval_window_days=3, intraday=False,
        )
        self.assertEqual(fields, {"benchmark_reason": "benchmark_disabled"})
        svc._benchmark_service.evaluate_excess_return.assert_not_called()

    def test_injected_service_counts_as_enabled(self) -> None:
        """Passing a service explicitly is an intentional opt-in."""
        bench = MagicMock()
        bench.evaluate_excess_return.return_value = ExcessReturnResult(
            market="in", benchmark_symbol="^NSEI", benchmark_name="Nifty 50",
            convention="close_to_close", start_date="2026-08-24", eval_window_days=3,
            signal_return_pct=1.0, benchmark_return_pct=0.5,
            excess_return_pct=0.5, benchmark_source="test", reason=None,
        )
        svc = _service(bench)
        svc._benchmark_enabled = True
        fields = svc._benchmark_fields(
            market="in", signal_return_pct=1.0,
            anchor_date=dt.date(2026, 8, 24), eval_window_days=3, intraday=False,
        )
        self.assertAlmostEqual(fields["excess_return_pct"], 0.5)


if __name__ == "__main__":
    unittest.main()
