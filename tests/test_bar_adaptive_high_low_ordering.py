# -*- coding: utf-8 -*-
"""Same-bar stop/target ordering: conservative default vs adaptive heuristic.

Daily OHLC does not contain the intra-bar path. When both the stop-loss and the
take-profit sit inside one bar's range, something has to decide which filled
first. These tests pin both available answers and the fallbacks between them.
"""

from datetime import date

import pytest

from src.core.backtest_engine import BacktestEngine, EvaluationConfig


class _Bar:
    def __init__(self, d, open_, high, low, close):
        self.date = d
        self.open = open_
        self.high = high
        self.low = low
        self.close = close


def _evaluate(bar, *, adaptive: bool):
    return BacktestEngine.evaluate_single(
        operation_advice="买入",
        analysis_date=date(2024, 1, 1),
        start_price=100.0,
        forward_bars=[bar],
        stop_loss=95.0,
        take_profit=105.0,
        config=EvaluationConfig(
            eval_window_days=1,
            neutral_band_pct=2.0,
            engine_version="test",
            bar_adaptive_high_low_ordering=adaptive,
        ),
    )


# Both 95 and 105 are inside each bar's range, so ordering decides the outcome.
_OPENED_NEAR_HIGH = _Bar(date(2024, 1, 1), 104.0, 106.0, 94.0, 100.0)
_OPENED_NEAR_LOW = _Bar(date(2024, 1, 1), 96.0, 106.0, 94.0, 100.0)


def test_default_is_unchanged_and_assumes_stop_first() -> None:
    """Off by default: previously stored outcomes keep their meaning."""
    for bar in (_OPENED_NEAR_HIGH, _OPENED_NEAR_LOW):
        result = _evaluate(bar, adaptive=False)
        assert result["first_hit"] == "ambiguous"
        assert result["simulated_exit_reason"] == "ambiguous_stop_loss"
        assert result["simulated_exit_price"] == 95.0


def test_open_near_high_resolves_to_take_profit() -> None:
    result = _evaluate(_OPENED_NEAR_HIGH, adaptive=True)

    assert result["first_hit"] == "take_profit"
    assert result["simulated_exit_reason"] == "adaptive_take_profit"
    assert result["simulated_exit_price"] == 105.0


def test_open_near_low_resolves_to_stop_loss() -> None:
    result = _evaluate(_OPENED_NEAR_LOW, adaptive=True)

    assert result["first_hit"] == "stop_loss"
    assert result["simulated_exit_reason"] == "adaptive_stop_loss"
    assert result["simulated_exit_price"] == 95.0


def test_both_targets_still_recorded_as_touched_either_way() -> None:
    """Resolving the order must not lose the fact that both levels traded."""
    for adaptive in (False, True):
        result = _evaluate(_OPENED_NEAR_HIGH, adaptive=adaptive)
        assert result["hit_stop_loss"] is True
        assert result["hit_take_profit"] is True


@pytest.mark.parametrize(
    "bar",
    [
        _Bar(date(2024, 1, 1), None, 106.0, 94.0, 100.0),   # no open
        _Bar(date(2024, 1, 1), 100.0, 106.0, 94.0, 100.0),  # exactly equidistant
    ],
    ids=["missing_open", "equidistant_open"],
)
def test_falls_back_to_conservative_when_heuristic_cannot_decide(bar) -> None:
    result = _evaluate(bar, adaptive=True)

    assert result["first_hit"] == "ambiguous"
    assert result["simulated_exit_reason"] == "ambiguous_stop_loss"


def test_unambiguous_bars_are_unaffected_by_the_flag() -> None:
    """Only genuinely ambiguous bars may behave differently."""
    only_tp = _Bar(date(2024, 1, 1), 100.0, 106.0, 99.0, 105.0)
    only_sl = _Bar(date(2024, 1, 1), 100.0, 101.0, 94.0, 95.0)

    for bar, expected in ((only_tp, "take_profit"), (only_sl, "stop_loss")):
        for adaptive in (False, True):
            assert _evaluate(bar, adaptive=adaptive)["first_hit"] == expected
