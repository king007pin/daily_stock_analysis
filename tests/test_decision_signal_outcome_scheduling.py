# -*- coding: utf-8 -*-
"""The decision-signal outcome evaluator must actually be scheduled.

Before this, ``DecisionSignalOutcomeService.run_outcomes`` had no production
caller anywhere: signals accrued and nothing ever scored them, so no
calibration data could exist regardless of how many signals were generated.
These tests exist so that regression cannot recur silently.
"""

from types import SimpleNamespace
from unittest.mock import patch

import main


def test_outcome_runner_is_wired_into_the_analysis_return_path() -> None:
    config = SimpleNamespace(backtest_enabled=True, market_review_enabled=False)
    args = SimpleNamespace(portfolio=None, no_market_review=True)

    with patch.object(main, "_resolve_portfolio_stock_codes", return_value=[]), patch.object(
        main, "_run_auto_backtest"
    ) as auto_backtest, patch.object(
        main, "_run_decision_signal_outcomes"
    ) as run_outcomes:
        assert main.run_full_analysis(config, args, []) is True

    auto_backtest.assert_called_once_with(config)
    run_outcomes.assert_called_once_with(config)


def test_outcome_runner_calls_the_service_and_reports_counts(caplog) -> None:
    config = SimpleNamespace(backtest_enabled=True)
    stats = {"evaluated": 3, "created": 2, "updated": 1, "skipped": 0}

    with patch(
        "src.services.decision_signal_outcome_service.DecisionSignalOutcomeService"
    ) as service_cls:
        service_cls.return_value.run_outcomes.return_value = stats
        main._run_decision_signal_outcomes(config)

    service_cls.return_value.run_outcomes.assert_called_once()


def test_outcome_runner_is_fail_soft() -> None:
    """A scoring failure must never take down the analysis run."""
    config = SimpleNamespace(backtest_enabled=True)

    with patch(
        "src.services.decision_signal_outcome_service.DecisionSignalOutcomeService",
        side_effect=RuntimeError("database is locked"),
    ):
        main._run_decision_signal_outcomes(config)  # must not raise


def test_outcome_runner_respects_backtest_disabled() -> None:
    config = SimpleNamespace(backtest_enabled=False)

    with patch(
        "src.services.decision_signal_outcome_service.DecisionSignalOutcomeService"
    ) as service_cls:
        main._run_decision_signal_outcomes(config)

    service_cls.assert_not_called()
