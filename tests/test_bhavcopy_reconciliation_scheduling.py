# -*- coding: utf-8 -*-
"""The NSE bhavcopy reconciliation must actually be scheduled.

Before this, ``BhavcopyReconciliationService.reconcile`` had no production
caller anywhere: the service could quarantine bars that disagree with NSE's own
published file and backfill the missing delivery fields, but nothing ever asked
it to, so no reconciliation evidence could accrue no matter how many bars were
stored. These tests exist so that regression cannot recur silently.

Fully offline: the service and the calendar helper are both patched, so no test
here touches the network, the database, or the real clock.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import main

SERVICE_PATH = "src.services.bhavcopy_reconciliation_service.BhavcopyReconciliationService"
PREVIOUS_DAY_PATH = "src.services.nse_trading_day_guard.previous_nse_trading_day"

# Friday 2026-08-21, the last session before the Monday the rest of the suite uses.
LAST_SESSION = date(2026, 8, 21)


def _summary(**overrides):
    summary = {
        "status": "ok",
        "trade_date": LAST_SESSION.isoformat(),
        "reason": None,
        "compared": 12,
        "agreed": 11,
        "quarantined": 1,
        "quarantine_records_written": 1,
        "delivery_backfilled": 11,
        "price_check": "enabled",
    }
    summary.update(overrides)
    return summary


def test_reconciliation_is_wired_into_the_empty_portfolio_return_path() -> None:
    """The path taken when the broker reports no eligible positions."""
    config = SimpleNamespace(backtest_enabled=True, market_review_enabled=False)
    args = SimpleNamespace(portfolio=None, no_market_review=True)

    with patch.object(main, "_resolve_portfolio_stock_codes", return_value=[]), patch.object(
        main, "_run_auto_backtest"
    ), patch.object(main, "_run_decision_signal_outcomes"), patch.object(
        main, "_run_bhavcopy_reconciliation"
    ) as reconcile:
        assert main.run_full_analysis(config, args, []) is True

    reconcile.assert_called_once_with(config)


def test_reconciliation_is_wired_into_the_shared_return_path() -> None:
    """The path every non-early return funnels through (``_return_with_auto_backtest``)."""
    config = SimpleNamespace(
        backtest_enabled=True,
        market_review_enabled=False,
        stock_list=[],
        refresh_stock_list=lambda: None,
    )
    args = SimpleNamespace(portfolio=None, no_market_review=True, dry_run=False)

    with patch.object(main, "_resolve_portfolio_stock_codes", return_value=None), patch.object(
        main, "_refresh_stock_index_cache_for_analysis"
    ), patch.object(main, "_run_auto_backtest"), patch.object(
        main, "_run_decision_signal_outcomes"
    ), patch.object(main, "_run_bhavcopy_reconciliation") as reconcile:
        assert main.run_full_analysis(config, args, None) is False

    reconcile.assert_called_once_with(config)


def test_runner_reconciles_the_last_completed_session_not_today() -> None:
    """Today's bhavcopy does not exist until after the close; never ask for it."""
    config = SimpleNamespace(bhavcopy_reconciliation_enabled=True)

    with patch(PREVIOUS_DAY_PATH, return_value=LAST_SESSION) as previous_day, patch(
        SERVICE_PATH
    ) as service_cls:
        service_cls.return_value.reconcile.return_value = _summary()
        main._run_bhavcopy_reconciliation(config)

    previous_day.assert_called_once_with()
    service_cls.return_value.reconcile.assert_called_once_with(LAST_SESSION)


def test_runner_skips_when_the_calendar_cannot_name_a_session() -> None:
    """``previous_nse_trading_day`` is fail-closed; a ``None`` must not become a guess."""
    config = SimpleNamespace(bhavcopy_reconciliation_enabled=True)

    with patch(PREVIOUS_DAY_PATH, return_value=None), patch(SERVICE_PATH) as service_cls:
        main._run_bhavcopy_reconciliation(config)

    service_cls.assert_not_called()


def test_runner_is_fail_soft() -> None:
    """A reconciliation failure must never take down the analysis run."""
    config = SimpleNamespace(bhavcopy_reconciliation_enabled=True)

    with patch(PREVIOUS_DAY_PATH, return_value=LAST_SESSION), patch(
        SERVICE_PATH, side_effect=RuntimeError("NSE returned 403")
    ):
        main._run_bhavcopy_reconciliation(config)  # must not raise


def test_runner_survives_a_reconcile_that_raises() -> None:
    """The failure can also come from the call itself, not just construction."""
    config = SimpleNamespace(bhavcopy_reconciliation_enabled=True)

    with patch(PREVIOUS_DAY_PATH, return_value=LAST_SESSION), patch(SERVICE_PATH) as service_cls:
        service_cls.return_value.reconcile.side_effect = RuntimeError("database is locked")
        main._run_bhavcopy_reconciliation(config)  # must not raise


def test_runner_respects_the_disabled_flag() -> None:
    """Reconciliation reaches the network, so the .env switch gates it before anything runs."""
    config = SimpleNamespace(bhavcopy_reconciliation_enabled=False)

    with patch(PREVIOUS_DAY_PATH) as previous_day, patch(SERVICE_PATH) as service_cls:
        main._run_bhavcopy_reconciliation(config)

    service_cls.assert_not_called()
    previous_day.assert_not_called()


def test_runner_treats_a_missing_flag_as_disabled() -> None:
    """An older config object without the attribute must not enable network access."""
    config = SimpleNamespace()

    with patch(PREVIOUS_DAY_PATH) as previous_day, patch(SERVICE_PATH) as service_cls:
        main._run_bhavcopy_reconciliation(config)

    service_cls.assert_not_called()
    previous_day.assert_not_called()


def test_runner_reports_an_unavailable_bhavcopy_without_raising() -> None:
    """Weekend/holiday/outage is a normal result, not an error (AGENTS.md 1.3)."""
    config = SimpleNamespace(bhavcopy_reconciliation_enabled=True)

    with patch(PREVIOUS_DAY_PATH, return_value=LAST_SESSION), patch(SERVICE_PATH) as service_cls:
        service_cls.return_value.reconcile.return_value = _summary(
            status="unavailable", reason="bhavcopy 不可用", compared=0, agreed=0
        )
        main._run_bhavcopy_reconciliation(config)  # must not raise
