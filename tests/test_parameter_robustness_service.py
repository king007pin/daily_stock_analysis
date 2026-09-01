# -*- coding: utf-8 -*-
"""Offline tests for the parameter robustness sweep.

Every "strategy" here is a closed-form function chosen so the expected verdict can be
reasoned about by hand: a plateau, a spike, a cliff. No market data, no network, no DB.
"""

from __future__ import annotations

import math

import pytest

from src.services.parameter_robustness_service import (
    ParameterRobustnessService,
    ParameterSpec,
    RobustnessError,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- specs


def test_spec_rejects_baseline_outside_bounds():
    with pytest.raises(RobustnessError):
        ParameterSpec("lookback", baseline=5, minimum=10)
    with pytest.raises(RobustnessError):
        ParameterSpec("lookback", baseline=50, maximum=20)


def test_spec_rejects_inverted_bounds():
    with pytest.raises(RobustnessError):
        ParameterSpec("x", baseline=5, minimum=10, maximum=1)


def test_perturbed_rounds_and_clamps():
    spec = ParameterSpec("lookback", baseline=20, minimum=18, integer=True)
    assert spec.perturbed(0.10) == 22.0          # 22.0 exactly
    assert spec.perturbed(-0.20) == 18.0         # 16 clamped up to the floor
    assert spec.perturbed(0.02) == 20.0          # rounds back onto the baseline


def test_clamped_to_baseline_is_degenerate_not_a_pass():
    """A parameter pinned by its bounds must not read as robust — nothing was tested."""
    spec = ParameterSpec("x", baseline=10, minimum=10, maximum=10)
    svc = ParameterRobustnessService()
    report = svc.sweep([spec, ParameterSpec("y", baseline=10)], lambda p: 1.0)
    x_report = next(r for r in report.parameters if r.name == "x")
    assert x_report.tested == []
    assert all(o.degenerate for o in x_report.outcomes)


# --------------------------------------------------------------------------- verdicts


def test_plateau_is_robust():
    """Metric barely moves with the parameters — the shape a real effect has."""
    svc = ParameterRobustnessService()
    report = svc.sweep(
        [ParameterSpec("a", 100.0), ParameterSpec("b", 50.0)],
        lambda p: 2.0 + 0.0001 * p["a"],
        metric_name="expectancy",
    )
    assert report.verdict == "ROBUST"
    assert report.complete
    assert not report.collapsed


def test_spike_at_baseline_is_curve_fit():
    """Baseline is the peak for every parameter — the overfit fingerprint."""
    svc = ParameterRobustnessService()

    def evaluate(p):
        # Gentle penalty away from baseline: peaks at baseline but never collapses,
        # so CURVE_FIT must be reached on the peak rule, not the collapse rule.
        return 2.0 - 0.4 * (abs(p["a"] - 100.0) / 100.0) - 0.4 * (abs(p["b"] - 50.0) / 50.0)

    report = svc.sweep([ParameterSpec("a", 100.0), ParameterSpec("b", 50.0)], evaluate)
    assert not report.collapsed
    assert all(pr.baseline_is_peak for pr in report.parameters)
    assert report.verdict == "CURVE_FIT"


def test_cliff_is_fragile():
    """A 10% move wipes out most of the edge."""
    svc = ParameterRobustnessService()

    def evaluate(p):
        return 3.0 if abs(p["a"] - 100.0) < 1e-9 else 0.2

    report = svc.sweep([ParameterSpec("a", 100.0), ParameterSpec("b", 50.0)], evaluate)
    assert report.verdict == "FRAGILE"
    assert report.collapsed


def test_sign_flip_counts_as_collapse():
    svc = ParameterRobustnessService()

    def evaluate(p):
        return 1.0 if p["a"] <= 100.0 else -1.0

    report = svc.sweep([ParameterSpec("a", 100.0)], evaluate)
    assert report.verdict == "FRAGILE"


def test_single_parameter_peaking_is_not_curve_fit():
    """One peak is unremarkable; the verdict needs at least two swept parameters."""
    svc = ParameterRobustnessService()
    report = svc.sweep([ParameterSpec("a", 100.0)], lambda p: 2.0 - abs(p["a"] - 100.0) / 1000.0)
    assert report.verdict == "ROBUST"


def test_non_positive_baseline_reports_no_edge():
    svc = ParameterRobustnessService()
    report = svc.sweep([ParameterSpec("a", 100.0), ParameterSpec("b", 5.0)], lambda p: -0.5)
    assert report.verdict == "NO_BASELINE_EDGE"


# --------------------------------------------------------------------------- errors


def test_failing_point_is_recorded_not_swallowed():
    svc = ParameterRobustnessService()

    def evaluate(p):
        if p["a"] > 110.0:
            raise ValueError("insufficient bars")
        return 2.0

    report = svc.sweep([ParameterSpec("a", 100.0), ParameterSpec("b", 5.0)], evaluate)
    assert not report.complete
    assert any("insufficient bars" in e for e in report.errors)
    assert "PROVISIONAL" in report.summary()


def test_non_finite_metric_is_an_error():
    svc = ParameterRobustnessService()
    report = svc.sweep(
        [ParameterSpec("a", 100.0), ParameterSpec("b", 5.0)],
        lambda p: float("nan") if p["a"] > 110.0 else 1.0,
    )
    assert not report.complete
    assert any("non-finite" in e for e in report.errors)


def test_baseline_failure_raises():
    svc = ParameterRobustnessService()
    with pytest.raises(RobustnessError, match="baseline evaluation failed"):
        svc.sweep([ParameterSpec("a", 1.0)], lambda p: (_ for _ in ()).throw(RuntimeError("boom")))


def test_rejects_empty_and_duplicate_specs():
    svc = ParameterRobustnessService()
    with pytest.raises(RobustnessError):
        svc.sweep([], lambda p: 1.0)
    with pytest.raises(RobustnessError, match="duplicate"):
        svc.sweep([ParameterSpec("a", 1.0), ParameterSpec("a", 2.0)], lambda p: 1.0)


def test_rejects_zero_perturbation_and_bad_threshold():
    with pytest.raises(RobustnessError):
        ParameterRobustnessService(perturbations=(0.0, 0.1))
    with pytest.raises(RobustnessError):
        ParameterRobustnessService(collapse_threshold=0.0)
    with pytest.raises(RobustnessError):
        ParameterRobustnessService(collapse_threshold=1.0)


# --------------------------------------------------------------------------- metrics


def test_joint_worst_case_is_no_better_than_the_worst_single_move():
    """Independent penalties must compound when applied together."""
    svc = ParameterRobustnessService()

    def evaluate(p):
        return 10.0 - abs(p["a"] - 100.0) / 10.0 - abs(p["b"] - 100.0) / 10.0

    report = svc.sweep([ParameterSpec("a", 100.0), ParameterSpec("b", 100.0)], evaluate)
    assert report.joint_worst_case is not None and report.joint_worst_case.ok
    worst_single = min(p.worst_metric for p in report.parameters)
    assert report.joint_worst_case.metric <= worst_single + 1e-9


def test_elasticity_measures_sensitivity():
    """A 20% parameter move costing 20% of the metric is elasticity 1.0."""
    svc = ParameterRobustnessService(perturbations=(0.20,))
    report = svc.sweep([ParameterSpec("a", 100.0)], lambda p: 100.0 / p["a"] * 2.0)
    # a=120 -> metric 1.6667 from baseline 2.0: rel change 0.1667 over pct 0.20 => 0.833
    assert report.parameters[0].max_elasticity == pytest.approx(0.8333, abs=1e-3)


def test_elasticity_undefined_at_zero_baseline():
    svc = ParameterRobustnessService()
    report = svc.sweep([ParameterSpec("a", 100.0)], lambda p: 0.0)
    assert report.parameters[0].max_elasticity is None


def test_sweep_is_deterministic():
    svc = ParameterRobustnessService()

    def evaluate(p):
        return math.sin(p["a"]) + p["b"] / 100.0

    specs = [ParameterSpec("a", 12.0), ParameterSpec("b", 40.0)]
    assert svc.sweep(specs, evaluate).summary() == svc.sweep(specs, evaluate).summary()
