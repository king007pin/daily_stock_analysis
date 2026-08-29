# -*- coding: utf-8 -*-
"""The decision-signal evaluator must self-heal gaps in stock_daily.

BacktestService has refilled missing bars since it was written. This path did
not, so whenever the scheduled analysis jobs stopped running the bar table went
stale and already-recorded signals could never mature — verified 2026-08-24,
where outcomes sat at `insufficient_forward_bars` until bars were backfilled by
hand. These tests exist so that asymmetry cannot silently return.
"""

from datetime import date
from unittest.mock import MagicMock, patch

from src.services.decision_signal_outcome_service import DecisionSignalOutcomeService


def _service_with_bars(start_bar, forward_bars):
    svc = DecisionSignalOutcomeService.__new__(DecisionSignalOutcomeService)
    svc._refill_enabled = True
    svc.stock_repo = MagicMock()
    svc.stock_repo.get_daily_on_date.return_value = start_bar
    svc.stock_repo.get_forward_bars.return_value = forward_bars
    return svc


def test_refill_is_attempted_when_forward_bars_are_short() -> None:
    svc = _service_with_bars(object(), [object()])  # 1 bar, needs 3

    assert svc._bars_are_inadequate("600519", date(2024, 1, 2), "3d", 3, object()) is True


def test_refill_is_attempted_when_anchor_bar_is_missing() -> None:
    svc = _service_with_bars(None, [])

    assert svc._bars_are_inadequate("600519", date(2024, 1, 2), "3d", 3, None) is True


def test_no_refill_when_window_is_already_complete() -> None:
    svc = _service_with_bars(object(), [object(), object(), object()])

    assert svc._bars_are_inadequate("600519", date(2024, 1, 2), "3d", 3, object()) is False


def test_intraday_needs_only_the_anchor_bar() -> None:
    """The anchor day's own bar is the whole window — never refill on its account."""
    svc = _service_with_bars(object(), [])

    assert svc._bars_are_inadequate("IDEA.NS", date(2024, 1, 2), "intraday", 1, object()) is False


def test_probe_failure_does_not_trigger_a_refill_storm() -> None:
    svc = DecisionSignalOutcomeService.__new__(DecisionSignalOutcomeService)
    svc._refill_enabled = True
    svc.stock_repo = MagicMock()
    svc.stock_repo.get_forward_bars.side_effect = RuntimeError("db locked")

    assert svc._bars_are_inadequate("600519", date(2024, 1, 2), "3d", 3, object()) is False


def test_refill_persists_fetched_bars() -> None:
    svc = DecisionSignalOutcomeService.__new__(DecisionSignalOutcomeService)
    svc._refill_enabled = True
    svc.stock_repo = MagicMock()
    frame = MagicMock(); frame.empty = False

    with patch("data_provider.base.DataFetcherManager") as manager_cls:
        manager_cls.return_value.get_daily_data.return_value = (frame, "YfinanceFetcher")
        svc._try_fill_daily_data(code="IDEA.NS", anchor_date=date(2024, 1, 2), eval_window_days=3)

    svc.stock_repo.save_dataframe.assert_called_once_with(frame, "IDEA.NS", "YfinanceFetcher")


def test_refill_is_fail_soft_on_data_source_outage() -> None:
    """A fetcher outage must degrade the outcome to `unable`, never raise."""
    svc = DecisionSignalOutcomeService.__new__(DecisionSignalOutcomeService)
    svc._refill_enabled = True
    svc.stock_repo = MagicMock()

    with patch("data_provider.base.DataFetcherManager", side_effect=RuntimeError("network down")):
        svc._try_fill_daily_data(code="IDEA.NS", anchor_date=date(2024, 1, 2), eval_window_days=3)

    svc.stock_repo.save_dataframe.assert_not_called()


def test_refill_skips_empty_frames() -> None:
    svc = DecisionSignalOutcomeService.__new__(DecisionSignalOutcomeService)
    svc._refill_enabled = True
    svc.stock_repo = MagicMock()
    empty = MagicMock(); empty.empty = True

    with patch("data_provider.base.DataFetcherManager") as manager_cls:
        manager_cls.return_value.get_daily_data.return_value = (empty, "src")
        svc._try_fill_daily_data(code="IDEA.NS", anchor_date=date(2024, 1, 2), eval_window_days=3)

    svc.stock_repo.save_dataframe.assert_not_called()


def test_refill_is_off_by_default_so_the_suite_stays_offline() -> None:
    """Opt-in by design: unset config must not turn evaluation into a network call.

    The first version of this feature had no gate. It made five existing tests
    fetch live data from eastmoney and took the outcome suite from seconds to
    167s. Default-off is the fix, not a compromise.
    """
    svc = DecisionSignalOutcomeService()

    assert svc._refill_enabled is False


def test_refill_can_be_enabled_explicitly() -> None:
    assert DecisionSignalOutcomeService(refill_enabled=True)._refill_enabled is True
