# -*- coding: utf-8 -*-
"""Tests for src/services/performance_stats_service.py.

Fully offline and deterministic: every bootstrap is seeded explicitly, no DB,
no network, no filesystem. The point of the module under test is that a
statistic which the sample cannot support is *absent*, not merely caveated, so
the tests assert the ``None`` point estimates directly.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.performance_stats_service import (  # noqa: E402
    MIN_SHARPE_SAMPLE_SIZE,
    MIN_WIN_RATE_SAMPLE_SIZE,
    REASON_INSUFFICIENT_SAMPLE,
    REASON_NO_DECIDED_OUTCOMES,
    REASON_ZERO_VARIANCE,
    REQUIRED_SUMMARY_KEYS,
    PerformanceStatsError,
    _assert_summary_contract,
    annualized_sharpe_ratio,
    deflated_sharpe_ratio,
    normal_ppf,
    per_period_sharpe,
    sharpe_ratio_with_ci,
    summary,
    wilson_score_interval,
    win_rate_with_interval,
)

pytestmark = pytest.mark.unit

SEED = 20260824

# [0.02, 0.00] repeated: mean 0.01, sample sd = 0.01 * sqrt(n / (n - 1)),
# so the per-period Sharpe collapses to sqrt((n - 1) / n) exactly.
CLEAN_PATTERN_4 = [0.02, 0.00] * 2  # n = 4  -> below every gate
CLEAN_PATTERN_32 = [0.02, 0.00] * 16  # n = 32 -> above the Sharpe gate
# Mean exactly zero: the Sharpe point estimate is 0 and the CI must straddle it.
ZERO_MEAN_32 = [0.02, -0.02] * 16
# Deliberately low-variance positive series: CI must sit clearly above zero.
LOW_VARIANCE_POSITIVE_33 = [0.020, 0.021, 0.019] * 11


# --- Sharpe point estimate ----------------------------------------------------


def test_sharpe_matches_hand_computed_value():
    # n = 4, values [0.02, 0, 0.02, 0]: mean 0.01, sum of squared deviations
    # 4 * 0.01^2 = 4e-4, sample variance 4e-4 / 3, sd = 0.01 * sqrt(4/3).
    # Per-period Sharpe = 0.01 / (0.01 * sqrt(4/3)) = sqrt(3)/2 = 0.8660254.
    expected = math.sqrt(3) / 2 * math.sqrt(252)
    assert annualized_sharpe_ratio(CLEAN_PATTERN_4) == pytest.approx(expected, rel=1e-12)

    # n = 32, same pattern: per-period Sharpe = sqrt(31/32).
    expected_32 = math.sqrt(31 / 32) * math.sqrt(252)
    assert annualized_sharpe_ratio(CLEAN_PATTERN_32) == pytest.approx(expected_32, rel=1e-12)
    assert annualized_sharpe_ratio(CLEAN_PATTERN_32) == pytest.approx(15.6244999919, rel=1e-9)


def test_risk_free_rate_is_annual_and_converted_per_period():
    # 2.52% annual over 252 periods = 0.01% per period, so the excess mean drops
    # from 0.01 to 0.0099 while the sd is unchanged.
    sd = 0.01 * math.sqrt(32 / 31)
    expected = 0.0099 / sd * math.sqrt(252)
    got = annualized_sharpe_ratio(CLEAN_PATTERN_32, risk_free_rate=0.0252, periods_per_year=252)
    assert got == pytest.approx(expected, rel=1e-12)


def test_zero_variance_series_has_no_sharpe():
    flat = [0.001] * 40
    assert annualized_sharpe_ratio(flat) is None
    result = sharpe_ratio_with_ci(flat, seed=SEED, n_bootstrap=200)
    assert result.point_estimate is None
    assert result.unavailable_reason == REASON_ZERO_VARIANCE
    assert result.excludes_zero is False


# --- Bootstrap CI -------------------------------------------------------------


def test_bootstrap_ci_brackets_point_estimate_and_is_deterministic():
    first = sharpe_ratio_with_ci(CLEAN_PATTERN_32, seed=SEED, n_bootstrap=500)
    second = sharpe_ratio_with_ci(CLEAN_PATTERN_32, seed=SEED, n_bootstrap=500)

    assert first.point_estimate is not None
    assert first.lower_bound <= first.point_estimate <= first.upper_bound
    assert first.lower_bound < first.upper_bound
    # Same seed, same inputs -> byte-identical interval. An unreproducible
    # confidence bound is not a measurement.
    assert (first.lower_bound, first.upper_bound) == (second.lower_bound, second.upper_bound)
    assert first.to_dict() == second.to_dict()

    # A different seed moves the interval but must still bracket the estimate.
    other = sharpe_ratio_with_ci(CLEAN_PATTERN_32, seed=SEED + 1, n_bootstrap=500)
    assert other.lower_bound <= other.point_estimate <= other.upper_bound
    assert other.point_estimate == pytest.approx(first.point_estimate)


def test_ci_widens_as_confidence_rises():
    narrow = sharpe_ratio_with_ci(CLEAN_PATTERN_32, confidence=0.80, seed=SEED, n_bootstrap=500)
    wide = sharpe_ratio_with_ci(CLEAN_PATTERN_32, confidence=0.99, seed=SEED, n_bootstrap=500)
    assert wide.lower_bound < narrow.lower_bound
    assert wide.upper_bound > narrow.upper_bound


# --- The hard gate (most important assertions in this file) -------------------


def test_sharpe_below_gate_has_none_point_estimate():
    """A sample below the gate yields NO number at all — not a caveated one."""
    too_small = [0.02, 0.00] * ((MIN_SHARPE_SAMPLE_SIZE - 1) // 2)
    assert len(too_small) < MIN_SHARPE_SAMPLE_SIZE

    result = sharpe_ratio_with_ci(too_small, seed=SEED, n_bootstrap=500)

    assert result.insufficient_sample is True
    assert result.point_estimate is None  # <- the whole point of Stage 11
    assert result.lower_bound is None
    assert result.upper_bound is None
    assert result.excludes_zero is False
    assert result.unavailable_reason == REASON_INSUFFICIENT_SAMPLE
    assert result.n_observations == len(too_small)
    assert result.min_sample_size == MIN_SHARPE_SAMPLE_SIZE
    # Nothing anywhere in the payload leaks a usable Sharpe number.
    assert result.to_dict()["point_estimate"] is None


def test_sharpe_gate_is_exact_at_the_boundary():
    below = sharpe_ratio_with_ci([0.02, 0.00] * 14 + [0.01], seed=SEED, n_bootstrap=200)
    assert below.n_observations == MIN_SHARPE_SAMPLE_SIZE - 1
    assert below.point_estimate is None
    assert below.insufficient_sample is True

    at_gate = sharpe_ratio_with_ci([0.02, 0.00] * 15, seed=SEED, n_bootstrap=200)
    assert at_gate.n_observations == MIN_SHARPE_SAMPLE_SIZE
    assert at_gate.point_estimate is not None
    assert at_gate.insufficient_sample is False


def test_empty_return_series_is_gated_not_crashed():
    result = sharpe_ratio_with_ci([], seed=SEED)
    assert result.n_observations == 0
    assert result.point_estimate is None
    assert result.insufficient_sample is True


# --- excludes_zero ------------------------------------------------------------


def test_excludes_zero_is_false_when_ci_straddles_zero():
    result = sharpe_ratio_with_ci(ZERO_MEAN_32, seed=SEED, n_bootstrap=1000)
    assert result.point_estimate == pytest.approx(0.0, abs=1e-12)
    assert result.lower_bound < 0.0 < result.upper_bound
    assert result.excludes_zero is False


def test_excludes_zero_is_true_when_ci_is_clearly_above_zero():
    result = sharpe_ratio_with_ci(LOW_VARIANCE_POSITIVE_33, seed=SEED, n_bootstrap=1000)
    assert result.point_estimate > 0.0
    assert result.lower_bound > 0.0
    assert result.excludes_zero is True


# --- Wilson interval ----------------------------------------------------------


def test_wilson_interval_matches_hand_checked_small_sample():
    # n = 20, x = 12, z = 1.959964.  z^2 = 3.8415.
    # centre = (12 + z^2/2) / (20 + z^2) = 13.9208 / 23.8416 = 0.583972
    # margin = z/(20 + z^2) * sqrt(12*8/20 + z^2/4) = 0.0822076 * 2.400083
    lower, upper = wilson_score_interval(12, 20)
    assert lower == pytest.approx(0.3865815008, abs=1e-9)
    assert upper == pytest.approx(0.7811934676, abs=1e-9)
    # The interval is NOT centred on the observed 0.6 — it is pulled toward 0.5.
    assert (lower + upper) / 2 == pytest.approx(0.5838874842, abs=1e-9)
    assert (lower + upper) / 2 < 0.6


def test_wilson_two_of_two_is_not_a_clean_hundred_percent():
    """2 hits, 0 misses must not read as a symmetric, confident 100%."""
    lower, upper = wilson_score_interval(2, 2)
    assert lower == pytest.approx(0.3423802275, abs=1e-9)
    assert upper == pytest.approx(1.0)
    # Asymmetric about the observed rate of 1.0: all the uncertainty is below.
    observed = 1.0
    assert observed - lower > 0.65
    assert upper - observed == pytest.approx(0.0)
    assert (upper - lower) > 0.65
    # The Wald/normal approximation would have said [1.0, 1.0]; Wilson does not.
    assert lower < 0.5


def test_wilson_zero_of_two_is_asymmetric_at_the_lower_bound():
    lower, upper = wilson_score_interval(0, 2)
    assert lower == pytest.approx(0.0)
    assert upper == pytest.approx(1.0 - 0.3423802275, abs=1e-9)


def test_wilson_rejects_impossible_counts():
    with pytest.raises(PerformanceStatsError):
        wilson_score_interval(3, 2)
    with pytest.raises(PerformanceStatsError):
        wilson_score_interval(1, 10, confidence=1.5)


def test_wilson_with_zero_trials_is_totally_uninformative():
    assert wilson_score_interval(0, 0) == (0.0, 1.0)


# --- Win rate gate ------------------------------------------------------------


def test_win_rate_below_gate_has_none_point_estimate():
    """No percentage is returned for a sample that cannot support one."""
    result = win_rate_with_interval(hits=2, misses=0)

    assert result.insufficient_sample is True
    assert result.point_estimate is None  # never 1.0, never "100%"
    assert result.lower_bound is None
    assert result.upper_bound is None
    assert result.excludes_coin_flip is False
    assert result.unavailable_reason == REASON_INSUFFICIENT_SAMPLE
    assert result.n_decided == 2
    assert result.min_sample_size == MIN_WIN_RATE_SAMPLE_SIZE
    assert result.to_dict()["point_estimate"] is None


def test_win_rate_with_no_decided_outcomes_reports_that_reason():
    result = win_rate_with_interval(hits=0, misses=0)
    assert result.point_estimate is None
    assert result.insufficient_sample is True
    assert result.unavailable_reason == REASON_NO_DECIDED_OUTCOMES


def test_win_rate_at_gate_reports_rate_with_wilson_interval():
    result = win_rate_with_interval(hits=12, misses=8)
    assert result.n_decided == MIN_WIN_RATE_SAMPLE_SIZE
    assert result.insufficient_sample is False
    assert result.point_estimate == pytest.approx(0.6)
    assert result.lower_bound == pytest.approx(0.3865815008, abs=1e-9)
    assert result.upper_bound == pytest.approx(0.7811934676, abs=1e-9)
    # 12/20 cannot distinguish skill from a coin flip; the interval spans 0.5.
    assert result.excludes_coin_flip is False


def test_win_rate_excludes_coin_flip_only_with_a_real_edge():
    result = win_rate_with_interval(hits=80, misses=20)
    assert result.point_estimate == pytest.approx(0.8)
    assert result.lower_bound > 0.5
    assert result.excludes_coin_flip is True


# --- Deflated Sharpe ----------------------------------------------------------


def test_deflated_sharpe_penalises_more_executed_trials():
    single = deflated_sharpe_ratio(0.15, n_trials=1, n_observations=250)
    many = deflated_sharpe_ratio(0.15, n_trials=100, n_observations=250)

    assert single.expected_max_sharpe_under_null == pytest.approx(0.0)
    assert many.expected_max_sharpe_under_null > 0.0
    assert many.deflated_sharpe < single.deflated_sharpe
    assert 0.0 <= many.deflated_sharpe <= 1.0


def test_deflated_sharpe_below_gate_has_none_point_estimate():
    result = deflated_sharpe_ratio(0.5, n_trials=10, n_observations=4)
    assert result.insufficient_sample is True
    assert result.deflated_sharpe is None
    assert result.unavailable_reason == REASON_INSUFFICIENT_SAMPLE


def test_deflated_sharpe_requires_at_least_one_executed_trial():
    with pytest.raises(PerformanceStatsError):
        deflated_sharpe_ratio(0.5, n_trials=0, n_observations=250)


def test_deflated_sharpe_documents_that_unexecuted_variants_do_not_count():
    doc = (deflated_sharpe_ratio.__doc__ or "").lower()
    assert "never tested" in doc
    assert "backtested" in doc and "run" in doc
    assert "document" in doc
    # The caveat travels with the result, not only with the docstring.
    caveat = deflated_sharpe_ratio(0.15, n_trials=5, n_observations=250).caveat.lower()
    assert "never executed" in caveat and "never tested" in caveat


def test_per_period_sharpe_round_trips_the_annualization():
    annual = annualized_sharpe_ratio(CLEAN_PATTERN_32)
    assert per_period_sharpe(annual) == pytest.approx(math.sqrt(31 / 32), rel=1e-12)


# --- summary() ----------------------------------------------------------------


def test_summary_always_reports_n_and_unable_breakdown_when_everything_is_gated():
    payload = summary(
        outcomes=["hit", "miss", "neutral"],
        unable_reasons=["non_directional_action", "non_directional_action", "missing_anchor_price"],
        returns=[0.01, -0.01, 0.02],
        seed=SEED,
        n_bootstrap=200,
    )

    for key in REQUIRED_SUMMARY_KEYS:
        assert key in payload

    # Every statistic is gated off...
    assert payload["win_rate"]["point_estimate"] is None
    assert payload["sharpe"]["point_estimate"] is None
    assert payload["reportable"] is False
    # ...and the denominators and coverage are still there.
    assert payload["total"] == 6
    assert payload["completed"] == 3
    assert payload["decided"] == 2
    assert payload["unable"] == 3
    assert payload["unable_reasons"] == {
        "missing_anchor_price": 1,
        "non_directional_action": 2,
    }
    assert payload["neutral_share"] == pytest.approx(1 / 3)
    assert payload["coverage"]["unable_share"] == pytest.approx(0.5)
    assert payload["coverage"]["n_return_observations"] == 3
    assert payload["notes"]


def test_summary_never_emits_a_bare_ungated_rate_key():
    payload = summary(outcomes=["hit"] * 40 + ["miss"] * 10, seed=SEED, n_bootstrap=200)
    # The ungated hit_rate_pct convention from _aggregate is deliberately absent.
    assert "hit_rate_pct" not in payload
    assert "win_rate_pct" not in payload
    # A rate exists only inside a block that also carries its own denominator.
    assert payload["win_rate"]["point_estimate"] == pytest.approx(0.8)
    assert payload["win_rate"]["n_decided"] == 50
    assert payload["reportable"] is True


def test_summary_win_rate_denominator_excludes_neutrals():
    payload = summary(outcomes=["hit"] * 15 + ["miss"] * 5 + ["neutral"] * 30, seed=SEED, n_bootstrap=200)
    assert payload["completed"] == 50
    assert payload["decided"] == 20
    assert payload["win_rate"]["n_decided"] == 20
    assert payload["win_rate"]["point_estimate"] == pytest.approx(0.75)
    # Neutrals dropped from the denominator must still be visible next to it.
    assert payload["neutral_share"] == pytest.approx(0.6)
    assert payload["coverage"]["decided_share"] == pytest.approx(0.4)


def test_summary_accepts_unable_reasons_as_a_mapping():
    payload = summary(outcomes=["hit", "miss"], unable_reasons={"insufficient_forward_bars": 4})
    assert payload["unable"] == 4
    assert payload["unable_reasons"] == {"insufficient_forward_bars": 4}
    assert payload["total"] == 6


def test_summary_rejects_unknown_outcome_vocabulary():
    with pytest.raises(PerformanceStatsError):
        summary(outcomes=["hit", "win"])


def test_summary_contract_guard_rejects_a_payload_missing_its_denominator():
    payload = summary(outcomes=["hit", "miss"])
    stripped = {key: value for key, value in payload.items() if key != "total"}
    with pytest.raises(PerformanceStatsError):
        _assert_summary_contract(stripped)
    _assert_summary_contract(payload)  # unmodified payload is fine


# --- The system's real current state ------------------------------------------


def test_current_state_2_hits_2_neutral_0_miss_refuses_to_emit_a_win_rate():
    """Stage 11's motivating case: N=4 scored outcomes is not a track record."""
    payload = summary(
        outcomes=["hit", "hit", "neutral", "neutral"],
        unable_reasons=["non_directional_action"],
        label="decision_signal_outcomes_current",
    )

    assert payload["total"] == 5
    assert payload["completed"] == 4
    assert payload["hit"] == 2
    assert payload["miss"] == 0
    assert payload["neutral"] == 2
    assert payload["decided"] == 2
    assert payload["neutral_share"] == pytest.approx(0.5)

    win_rate = payload["win_rate"]
    assert win_rate["point_estimate"] is None  # NOT 1.0, NOT "100%"
    assert win_rate["lower_bound"] is None
    assert win_rate["upper_bound"] is None
    assert win_rate["insufficient_sample"] is True
    assert win_rate["n_decided"] == 2
    assert win_rate["excludes_coin_flip"] is False

    assert payload["sharpe"]["point_estimate"] is None
    assert payload["reportable"] is False
    assert payload["unable_reasons"] == {"non_directional_action": 1}
    assert any("minimum of 20" in note for note in payload["notes"])

    # And the naive "2 for 2 = 100%" number is nowhere in the rate fields.
    rate_fields = [win_rate[key] for key in ("point_estimate", "lower_bound", "upper_bound")]
    assert all(value is None for value in rate_fields)
    assert "100%" not in str(payload)


# --- Input validation / no-scipy fallback -------------------------------------


def test_normal_ppf_matches_known_quantiles_without_scipy():
    assert normal_ppf(0.975) == pytest.approx(1.959963985, abs=1e-9)
    assert normal_ppf(0.995) == pytest.approx(2.575829304, abs=1e-9)
    assert normal_ppf(0.5) == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(PerformanceStatsError):
        normal_ppf(0.0)


def test_non_finite_returns_are_rejected_not_silently_dropped():
    with pytest.raises(PerformanceStatsError):
        sharpe_ratio_with_ci([0.01, float("nan")] * 20, seed=SEED)
    with pytest.raises(PerformanceStatsError):
        annualized_sharpe_ratio([0.01, float("inf")] * 20)
    with pytest.raises(PerformanceStatsError):
        annualized_sharpe_ratio("0.01,0.02")


def test_invalid_parameters_raise():
    with pytest.raises(PerformanceStatsError):
        sharpe_ratio_with_ci(CLEAN_PATTERN_32, confidence=0.0, seed=SEED)
    with pytest.raises(PerformanceStatsError):
        sharpe_ratio_with_ci(CLEAN_PATTERN_32, periods_per_year=0, seed=SEED)
    with pytest.raises(PerformanceStatsError):
        sharpe_ratio_with_ci(CLEAN_PATTERN_32, n_bootstrap=0, seed=SEED)
    with pytest.raises(PerformanceStatsError):
        win_rate_with_interval(-1, 5)
