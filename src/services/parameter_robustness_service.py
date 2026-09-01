# -*- coding: utf-8 -*-
"""Parameter robustness sweep — the test that kills curve-fit strategies.

A backtest reports what a parameter set did on one path of history. It cannot tell you
whether that result came from a real effect or from the parameters having been nudged,
consciously or not, onto the one lucky point in the search space. Those two cases look
identical in a single equity curve, and only the second one collapses in live trading.

The discriminator is cheap: perturb each parameter by +/-10% and +/-20% and re-evaluate.
A real effect degrades **gracefully and roughly symmetrically** — it is a plateau. A
curve-fit sits on a **spike**: the baseline outscores every neighbour, and the metric
falls off a cliff a few percent away in either direction.

This module runs that sweep and names the failure. It deliberately does not decide what
to do about it.

What it reports
---------------
``one_at_a_time``
    Each parameter perturbed alone, all others held at baseline. Isolates which single
    parameter the result actually hangs on.
``joint_worst_case``
    All parameters perturbed together toward their individually-worst directions. Real
    strategies degrade further here than in any single-parameter sweep; this is the
    number to quote as the honest downside, not the baseline.

Three verdicts
--------------
``ROBUST``
    No perturbation collapses the metric, and the baseline is not the peak everywhere.
``FRAGILE``
    At least one perturbation collapses the metric below ``collapse_threshold`` of
    baseline, or flips its sign.
``CURVE_FIT``
    The baseline is the best value for **every** parameter swept. With N parameters and
    4 perturbations each, landing on the peak of all N by chance is unlikely; the
    ordinary explanation is that the parameters were selected on this same data.

Design constraints
------------------
* **No default strategy, no network, no DB.** The caller supplies ``evaluate``; this
  module is a pure function of it. That keeps the harness testable offline and usable
  against any backtester.
* **No silent success on a broken evaluate.** If ``evaluate`` raises or returns a
  non-finite metric, that perturbation is recorded as ``error`` and the run is reported
  as incomplete. A sweep that quietly skipped half its points would be worse than no
  sweep, because it would read as a pass.
* **Deterministic.** Same inputs, same report. No sampling.

Interpreting a pass
-------------------
Surviving this sweep is necessary, not sufficient. It rules out one specific failure
(parameter overfit). It says nothing about survivorship bias, look-ahead, regime
dependence, or costs. See ``transaction_cost_service`` for the last of those.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_PERTURBATIONS",
    "ParameterSpec",
    "PerturbationOutcome",
    "ParameterReport",
    "RobustnessReport",
    "RobustnessError",
    "ParameterRobustnessService",
]

#: Davey's rule of thumb: a strategy whose result collapses inside +/-20% was fit, not found.
DEFAULT_PERTURBATIONS: Tuple[float, ...] = (-0.20, -0.10, 0.10, 0.20)


class RobustnessError(ValueError):
    """Raised when a sweep cannot be set up meaningfully (bad specs, no parameters)."""


@dataclass(frozen=True)
class ParameterSpec:
    """One tunable parameter and the limits of what it is allowed to become.

    ``minimum`` / ``maximum`` clamp perturbed values into the range the strategy can
    actually accept (a lookback of 0 bars is not a strategy variant, it is a crash).
    ``integer`` rounds perturbed values, for parameters that index bars or counts.

    A clamped or rounded perturbation that lands back on the baseline is recorded as
    ``degenerate`` rather than counted as a passing test point — otherwise a tightly
    clamped parameter would look robust purely because it never actually moved.
    """

    name: str
    baseline: float
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    integer: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.baseline):
            raise RobustnessError(f"{self.name}: baseline must be finite, got {self.baseline!r}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise RobustnessError(f"{self.name}: minimum {self.minimum} exceeds maximum {self.maximum}")
        if self.minimum is not None and self.baseline < self.minimum:
            raise RobustnessError(f"{self.name}: baseline {self.baseline} below minimum {self.minimum}")
        if self.maximum is not None and self.baseline > self.maximum:
            raise RobustnessError(f"{self.name}: baseline {self.baseline} above maximum {self.maximum}")

    def perturbed(self, pct: float) -> float:
        """Baseline scaled by ``1 + pct``, then rounded and clamped to the spec."""
        value = self.baseline * (1.0 + pct)
        if self.integer:
            value = float(round(value))
        if self.minimum is not None:
            value = max(value, self.minimum)
        if self.maximum is not None:
            value = min(value, self.maximum)
        return value


@dataclass(frozen=True)
class PerturbationOutcome:
    """Result of evaluating one perturbed parameter set."""

    parameter: str
    pct: float
    value: float
    metric: Optional[float]
    #: Set when ``evaluate`` raised or produced a non-finite metric.
    error: Optional[str] = None
    #: True when clamping/rounding put the value back on the baseline, so nothing was tested.
    degenerate: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.metric is not None and math.isfinite(self.metric)


@dataclass
class ParameterReport:
    """Per-parameter view: how far the metric moved, and in which direction."""

    name: str
    baseline_metric: float
    outcomes: List[PerturbationOutcome] = field(default_factory=list)

    @property
    def tested(self) -> List[PerturbationOutcome]:
        return [o for o in self.outcomes if o.ok and not o.degenerate]

    @property
    def worst_metric(self) -> Optional[float]:
        vals = [o.metric for o in self.tested]
        return min(vals) if vals else None  # type: ignore[arg-type]

    @property
    def best_metric(self) -> Optional[float]:
        vals = [o.metric for o in self.tested]
        return max(vals) if vals else None  # type: ignore[arg-type]

    @property
    def baseline_is_peak(self) -> bool:
        """True when no perturbation of this parameter beat the baseline.

        One parameter peaking at baseline is unremarkable. *Every* parameter peaking at
        baseline is the curve-fit fingerprint.
        """
        best = self.best_metric
        return best is not None and best <= self.baseline_metric

    @property
    def max_elasticity(self) -> Optional[float]:
        """Largest |relative metric change| / |relative parameter change| observed.

        Reads as: "a 1% parameter error costs this fraction of the result." Values above
        ~3 mean the result is balanced on a knife edge even if nothing formally collapsed.
        Undefined (``None``) when the baseline metric is zero, since there is no
        meaningful relative change to divide by.
        """
        if self.baseline_metric == 0:
            return None
        worst: Optional[float] = None
        for o in self.tested:
            assert o.metric is not None
            rel_metric = abs(o.metric - self.baseline_metric) / abs(self.baseline_metric)
            e = rel_metric / abs(o.pct)
            worst = e if worst is None else max(worst, e)
        return worst


@dataclass
class RobustnessReport:
    """Full sweep result and its verdict."""

    baseline_metric: float
    metric_name: str
    parameters: List[ParameterReport]
    joint_worst_case: Optional[PerturbationOutcome]
    collapse_threshold: float
    errors: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """False when any point failed to evaluate — the verdict is then provisional."""
        return not self.errors

    @property
    def collapsed(self) -> List[PerturbationOutcome]:
        """Perturbations that fell below the threshold or flipped the metric's sign."""
        out: List[PerturbationOutcome] = []
        base = self.baseline_metric
        candidates = [o for p in self.parameters for o in p.tested]
        if self.joint_worst_case is not None and self.joint_worst_case.ok:
            candidates.append(self.joint_worst_case)
        for o in candidates:
            assert o.metric is not None
            if base > 0 and o.metric < base * self.collapse_threshold:
                out.append(o)
            elif base > 0 and o.metric <= 0:
                out.append(o)
            elif base <= 0:
                # A non-positive baseline has no edge to preserve; the sweep cannot
                # meaningfully grade degradation, and `verdict` reports that separately.
                continue
        return out

    @property
    def verdict(self) -> str:
        if self.baseline_metric <= 0:
            return "NO_BASELINE_EDGE"
        if self.collapsed:
            return "FRAGILE"
        swept = [p for p in self.parameters if p.tested]
        if len(swept) >= 2 and all(p.baseline_is_peak for p in swept):
            return "CURVE_FIT"
        return "ROBUST"

    def summary(self) -> str:
        lines = [
            f"verdict: {self.verdict}"
            + ("" if self.complete else f"  (PROVISIONAL — {len(self.errors)} failed point(s))"),
            f"baseline {self.metric_name}: {self.baseline_metric:.4f}",
        ]
        for p in self.parameters:
            worst = p.worst_metric
            el = p.max_elasticity
            lines.append(
                f"  {p.name}: worst={'n/a' if worst is None else f'{worst:.4f}'}"
                f"  peak_at_baseline={p.baseline_is_peak}"
                f"  max_elasticity={'n/a' if el is None else f'{el:.2f}'}"
            )
        if self.joint_worst_case is not None and self.joint_worst_case.ok:
            assert self.joint_worst_case.metric is not None
            lines.append(f"  joint worst case: {self.joint_worst_case.metric:.4f}")
        for e in self.errors:
            lines.append(f"  ! {e}")
        return "\n".join(lines)


class ParameterRobustnessService:
    """Runs the perturbation sweep against a caller-supplied evaluation function.

    ``evaluate`` takes a full parameter mapping and returns a single scalar metric where
    **higher is better** (expectancy, profit factor, Sharpe). Do not pass max-drawdown
    directly — negate it, or the verdict inverts.
    """

    def __init__(
        self,
        perturbations: Sequence[float] = DEFAULT_PERTURBATIONS,
        collapse_threshold: float = 0.5,
    ) -> None:
        if not perturbations:
            raise RobustnessError("at least one perturbation percentage is required")
        if any(p == 0 for p in perturbations):
            raise RobustnessError("a 0% perturbation re-tests the baseline and grades nothing")
        if not 0.0 < collapse_threshold < 1.0:
            raise RobustnessError(f"collapse_threshold must be in (0, 1), got {collapse_threshold}")
        self.perturbations = tuple(perturbations)
        self.collapse_threshold = collapse_threshold

    def sweep(
        self,
        specs: Iterable[ParameterSpec],
        evaluate: Callable[[Mapping[str, float]], float],
        metric_name: str = "metric",
    ) -> RobustnessReport:
        spec_list = list(specs)
        if not spec_list:
            raise RobustnessError("no parameters to sweep")
        names = [s.name for s in spec_list]
        if len(set(names)) != len(names):
            raise RobustnessError("duplicate parameter names in specs")

        baseline_params = {s.name: s.baseline for s in spec_list}
        baseline_metric, err = self._call(evaluate, baseline_params)
        if err is not None or baseline_metric is None:
            raise RobustnessError(f"baseline evaluation failed: {err}")

        errors: List[str] = []
        reports: List[ParameterReport] = []
        # Direction (as a signed pct) that hurt each parameter most, for the joint case.
        worst_direction: Dict[str, float] = {}

        for spec in spec_list:
            report = ParameterReport(name=spec.name, baseline_metric=baseline_metric)
            worst_metric: Optional[float] = None
            for pct in self.perturbations:
                value = spec.perturbed(pct)
                if value == spec.baseline:
                    report.outcomes.append(
                        PerturbationOutcome(spec.name, pct, value, None, degenerate=True)
                    )
                    continue
                params = dict(baseline_params)
                params[spec.name] = value
                metric, e = self._call(evaluate, params)
                if e is not None:
                    errors.append(f"{spec.name} @ {pct:+.0%}: {e}")
                    report.outcomes.append(PerturbationOutcome(spec.name, pct, value, None, error=e))
                    continue
                report.outcomes.append(PerturbationOutcome(spec.name, pct, value, metric))
                assert metric is not None
                if worst_metric is None or metric < worst_metric:
                    worst_metric = metric
                    worst_direction[spec.name] = pct
            reports.append(report)

        joint = self._joint_worst_case(spec_list, worst_direction, evaluate, errors)
        return RobustnessReport(
            baseline_metric=baseline_metric,
            metric_name=metric_name,
            parameters=reports,
            joint_worst_case=joint,
            collapse_threshold=self.collapse_threshold,
            errors=errors,
        )

    def _joint_worst_case(
        self,
        specs: Sequence[ParameterSpec],
        worst_direction: Mapping[str, float],
        evaluate: Callable[[Mapping[str, float]], float],
        errors: List[str],
    ) -> Optional[PerturbationOutcome]:
        """Push every parameter simultaneously in its individually-worst direction.

        This is a lower bound on joint degradation, not the true multi-dimensional
        minimum: parameters interact, and the worst corner of the space may lie
        elsewhere. It is cheap (one extra evaluation) and consistently more honest than
        any single-parameter number, which is the point.
        """
        if not worst_direction:
            return None
        params: Dict[str, float] = {}
        moved = False
        for s in specs:
            pct = worst_direction.get(s.name)
            if pct is None:
                params[s.name] = s.baseline
                continue
            v = s.perturbed(pct)
            params[s.name] = v
            moved = moved or v != s.baseline
        if not moved:
            return None
        metric, e = self._call(evaluate, params)
        if e is not None:
            errors.append(f"joint worst case: {e}")
            return PerturbationOutcome("<joint>", float("nan"), float("nan"), None, error=e)
        return PerturbationOutcome("<joint>", float("nan"), float("nan"), metric)

    @staticmethod
    def _call(
        evaluate: Callable[[Mapping[str, float]], float], params: Mapping[str, float]
    ) -> Tuple[Optional[float], Optional[str]]:
        try:
            raw = evaluate(params)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            return None, f"{type(exc).__name__}: {exc}"
        try:
            metric = float(raw)
        except (TypeError, ValueError):
            return None, f"evaluate returned non-numeric {raw!r}"
        if not math.isfinite(metric):
            return None, f"evaluate returned non-finite {metric!r}"
        return metric, None
