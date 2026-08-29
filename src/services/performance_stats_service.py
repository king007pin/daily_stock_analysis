# -*- coding: utf-8 -*-
"""Honest performance statistics (Stage 11 of the quant maturity plan).

Why this module exists
----------------------
As of this file's introduction the system has **4 scored decision-signal
outcomes** (2 hit / 2 neutral / 0 miss). Any "win rate" computed from that is
noise: with 2 decided observations the 95% Wilson interval spans roughly
34%-100%, i.e. it cannot distinguish a coin flip from a world-class edge. The
same applies to a Sharpe ratio read off a handful of returns.

So this service is written to make a misleading number *structurally* hard to
emit, not merely discouraged by a comment:

1. Every reported statistic passes through a hard minimum-sample gate. Below
   the gate the point estimate is ``None`` and ``insufficient_sample`` is True.
   There is no "here is the number, but be careful" path — the number simply
   does not exist.
2. Confidence intervals are computed without assuming normality (bootstrap for
   Sharpe, Wilson score for proportions). Trading returns are fat-tailed and
   small-sample proportions are skewed; the normal approximation lies in
   exactly the regime this system is in.
3. :func:`summary` always emits N, the ``unable`` breakdown by reason, and the
   neutral share next to any statistic, so a consumer physically cannot render
   a rate without also holding its denominator and its coverage. The payload is
   self-checked before it is returned.

Conventions reused from the existing codebase (deliberately, not reinvented)
---------------------------------------------------------------------------
* Outcome vocabulary ``hit`` / ``miss`` / ``neutral`` and eval status
  ``completed`` / ``unable`` mirror
  ``src/services/decision_signal_outcome_service.py``.
* Win rate is ``hit / (hit + miss)`` with **neutrals excluded from the
  denominator** — the convention in that service's ``_aggregate`` and in
  ``scripts/build_calibration_curve.py``. Neutrals are reported separately as
  ``neutral_share`` because excluding them from a rate silently shrinks its
  denominator, and a reader must see how much of the sample was dropped.
* Reporting keys ``total`` / ``completed`` / ``unable`` / ``hit`` / ``miss`` /
  ``neutral`` / ``unable_reasons`` keep the same names as that service's
  ``_aggregate`` output so both can feed one reporting surface.
* The minimum-sample-gate idea (``insufficient_sample`` instead of a rate)
  comes from ``scripts/build_calibration_curve.py``.
* Deliberate divergence: this module never emits a bare ``hit_rate_pct``. That
  key in ``_aggregate`` is ungated, which is the exact failure mode Stage 11
  exists to remove. The gated ``win_rate`` block replaces it here.

The vocabulary constants are re-declared locally rather than imported from
``decision_signal_outcome_service`` on purpose: that module pulls in the
storage layer, repositories and config. This one is pure — sequences of floats
and ints in, dataclasses and dicts out. No DB, no I/O, no network.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

# scipy is optional in this repo (it is not in requirements.txt). Probe for it
# and fall back to an implementation that needs nothing beyond numpy and the
# standard library, so this module never becomes an install-time dependency.
try:  # pragma: no cover - import-shape branch, exercised by whichever env runs
    from scipy.stats import norm as _scipy_norm  # type: ignore

    SCIPY_AVAILABLE = True
except ImportError:  # pragma: no cover - see above
    _scipy_norm = None
    SCIPY_AVAILABLE = False


# --- Vocabulary (mirrors decision_signal_outcome_service; see module docstring)
OUTCOME_VALUES = frozenset({"hit", "miss", "neutral"})
EVAL_STATUSES = frozenset({"completed", "unable"})
DECIDED_OUTCOMES = frozenset({"hit", "miss"})
UNKNOWN_UNABLE_REASON = "unknown"


# --- Minimum-sample gates -----------------------------------------------------
#
# MIN_SHARPE_SAMPLE_SIZE = 30
#   Matches MIN_PROFILE_CALIBRATION_SAMPLE_SIZE in
#   decision_signal_outcome_service, so the codebase has one "enough data to
#   say something" number rather than two. It is also the point below which a
#   bootstrap CI is an artifact of the sample rather than a measurement: with
#   n < 30 the resamples redraw from so few distinct values that the percentile
#   interval mostly reports how lumpy the original sample was. Note that 30 is
#   a floor for *arithmetic honesty*, not a claim of statistical comfort — the
#   standard error of a Sharpe estimate at n=30 is still ~0.19 per period.
#
# MIN_WIN_RATE_SAMPLE_SIZE = 20 (decided outcomes = hit + miss)
#   At 20 decided outcomes the 95% Wilson half-width near p=0.5 is ~+/-21pp,
#   which is the first sample size where the interval can rule *anything* out.
#   Deliberately stricter than build_calibration_curve.py's N>=5: that gate
#   guards exploratory per-bucket diagnostics, this one guards a headline rate
#   that a human will read as a track record.
#
# For reference, the system's current state (4 scored, 2 decided) fails both by
# roughly an order of magnitude.
MIN_SHARPE_SAMPLE_SIZE = 30
MIN_WIN_RATE_SAMPLE_SIZE = 20

DEFAULT_PERIODS_PER_YEAR = 252
DEFAULT_N_BOOTSTRAP = 2000
DEFAULT_CONFIDENCE = 0.95
# Bootstraps are seeded by default so the same inputs always produce the same
# reported interval. An unreproducible confidence bound is not a measurement.
DEFAULT_BOOTSTRAP_SEED = 20260824

# Reasons a statistic can be absent. Absence always carries a reason so a
# consumer can say *why* there is no number instead of rendering a blank.
REASON_INSUFFICIENT_SAMPLE = "insufficient_sample"
REASON_ZERO_VARIANCE = "zero_variance"
REASON_NO_DECIDED_OUTCOMES = "no_decided_outcomes"
REASON_BOOTSTRAP_DEGENERATE = "bootstrap_degenerate"

EULER_MASCHERONI = 0.5772156649015329

# Keys summary() guarantees. Enforced at runtime by _assert_summary_contract.
REQUIRED_SUMMARY_KEYS: Tuple[str, ...] = (
    "total",
    "completed",
    "unable",
    "hit",
    "miss",
    "neutral",
    "unable_reasons",
    "neutral_share",
    "decided",
    "coverage",
    "win_rate",
    "sharpe",
    "min_sample_sizes",
    "reportable",
    "notes",
)


class PerformanceStatsError(ValueError):
    """Raised for malformed inputs (bad vocabulary, impossible counts)."""


# --- Result containers --------------------------------------------------------


@dataclass(frozen=True)
class SharpeResult:
    """Bootstrap Sharpe ratio result.

    ``point_estimate`` is ``None`` whenever the statistic is not supportable —
    below the sample gate, or with zero return variance. It is never a number
    plus a caveat; ``unavailable_reason`` explains the None.
    """

    point_estimate: Optional[float]
    lower_bound: Optional[float]
    upper_bound: Optional[float]
    n_observations: int
    excludes_zero: bool
    insufficient_sample: bool
    min_sample_size: int
    confidence: float
    periods_per_year: float
    n_bootstrap: int
    seed: Optional[int]
    unavailable_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WinRateResult:
    """Wilson score interval result for a hit/miss proportion.

    ``point_estimate`` is a fraction in [0, 1] (not a percentage) or ``None``.
    ``excludes_coin_flip`` is the proportion analogue of
    :attr:`SharpeResult.excludes_zero`: True only when the whole interval sits
    off 0.5, i.e. when the sample can actually tell skill from a coin flip.
    """

    point_estimate: Optional[float]
    lower_bound: Optional[float]
    upper_bound: Optional[float]
    hits: int
    misses: int
    n_decided: int
    excludes_coin_flip: bool
    insufficient_sample: bool
    min_sample_size: int
    confidence: float
    unavailable_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DeflatedSharpeResult:
    """Deflated Sharpe ratio (Bailey & Lopez de Prado) for executed trials."""

    deflated_sharpe: Optional[float]
    expected_max_sharpe_under_null: Optional[float]
    observed_sharpe: float
    n_trials: int
    n_observations: int
    insufficient_sample: bool
    min_sample_size: int
    unavailable_reason: Optional[str] = None
    caveat: str = (
        "n_trials must count backtests that were actually RUN. Strategy "
        "documents, ideas, or parameter grids that were never executed must "
        "not be counted: a Sharpe cannot be deflated for hypotheses that were "
        "never tested."
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --- Distribution helpers -----------------------------------------------------


def normal_ppf(p: float) -> float:
    """Inverse standard-normal CDF.

    Uses scipy when installed, otherwise ``statistics.NormalDist`` from the
    standard library (accurate to ~1e-14, and needs no third-party package).
    """
    p = float(p)
    if not 0.0 < p < 1.0:
        raise PerformanceStatsError(f"normal_ppf requires 0 < p < 1, got {p!r}")
    if SCIPY_AVAILABLE:  # pragma: no cover - depends on the env
        return float(_scipy_norm.ppf(p))
    from statistics import NormalDist

    return float(NormalDist().inv_cdf(p))


def normal_cdf(x: float) -> float:
    """Standard-normal CDF (``math.erf`` based; no scipy required)."""
    return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))


def _z_for_confidence(confidence: float) -> float:
    confidence = float(confidence)
    if not 0.0 < confidence < 1.0:
        raise PerformanceStatsError(
            f"confidence must be in (0, 1), got {confidence!r}"
        )
    return normal_ppf(1.0 - (1.0 - confidence) / 2.0)


# --- Input coercion -----------------------------------------------------------


def _coerce_returns(returns: Optional[Sequence[float]]) -> np.ndarray:
    """Validate a return series into a 1-D float array.

    Non-finite values are rejected rather than dropped: silently discarding a
    NaN return changes the denominator of every statistic downstream.
    """
    if returns is None:
        return np.empty(0, dtype=float)
    if isinstance(returns, (str, bytes)):
        raise PerformanceStatsError("returns must be a sequence of floats, not a string")
    try:
        array = np.asarray(list(returns), dtype=float)
    except (TypeError, ValueError) as exc:
        raise PerformanceStatsError(f"returns must be numeric: {exc}") from exc
    if array.size == 0:
        return np.empty(0, dtype=float)
    if array.ndim != 1:
        raise PerformanceStatsError("returns must be one-dimensional")
    if not np.all(np.isfinite(array)):
        raise PerformanceStatsError("returns must be finite (no NaN / inf)")
    return array


def _coerce_count(value: Any, name: str) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise PerformanceStatsError(f"{name} must be an integer: {value!r}") from exc
    if count < 0:
        raise PerformanceStatsError(f"{name} must be >= 0, got {count}")
    return count


# --- Sharpe -------------------------------------------------------------------


def annualized_sharpe_ratio(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
) -> Optional[float]:
    """Ungated annualized Sharpe ratio, or ``None`` if it is undefined.

    ``risk_free_rate`` is an **annual** rate and is converted to a per-period
    rate by dividing by ``periods_per_year``. Sample standard deviation uses
    ddof=1 (this is an estimate from a sample, not a population).

    This helper is intentionally ungated so the gate lives in exactly one
    place, :func:`sharpe_ratio_with_ci`, which is what reporting must call.
    """
    array = _coerce_returns(returns)
    if array.size < 2:
        return None
    periods_per_year = float(periods_per_year)
    if periods_per_year <= 0:
        raise PerformanceStatsError("periods_per_year must be > 0")
    excess = array - (float(risk_free_rate) / periods_per_year)
    std = float(np.std(excess, ddof=1))
    if std <= 0.0 or not math.isfinite(std):
        return None
    return float(np.mean(excess) / std * math.sqrt(periods_per_year))


def sharpe_ratio_with_ci(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
    n_bootstrap: int = DEFAULT_N_BOOTSTRAP,
    confidence: float = DEFAULT_CONFIDENCE,
    *,
    seed: Optional[int] = DEFAULT_BOOTSTRAP_SEED,
    min_sample_size: int = MIN_SHARPE_SAMPLE_SIZE,
) -> SharpeResult:
    """Annualized Sharpe ratio with a bootstrap percentile confidence interval.

    The interval is built by resampling the return series with replacement
    (``n_bootstrap`` draws of size N) and taking percentiles of the resulting
    Sharpe distribution. No normality is assumed anywhere: trading returns are
    fat-tailed and skewed, and the textbook ``SR / sqrt((1 + SR^2/2) / N)``
    normal interval understates the tails precisely when it matters.

    Below ``min_sample_size`` observations the result carries
    ``insufficient_sample=True`` and ``point_estimate=None``. That is a hard
    gate, not a warning: there is no code path that returns a Sharpe number for
    a sample too small to support one.

    ``seed`` defaults to a fixed value so the reported interval is reproducible
    for identical inputs.
    """
    array = _coerce_returns(returns)
    n = int(array.size)
    confidence = float(confidence)
    z_check = _z_for_confidence(confidence)  # validates confidence early
    del z_check
    periods_per_year = float(periods_per_year)
    if periods_per_year <= 0:
        raise PerformanceStatsError("periods_per_year must be > 0")
    n_bootstrap = _coerce_count(n_bootstrap, "n_bootstrap")
    min_sample_size = _coerce_count(min_sample_size, "min_sample_size")

    def _gated(reason: str) -> SharpeResult:
        return SharpeResult(
            point_estimate=None,
            lower_bound=None,
            upper_bound=None,
            n_observations=n,
            excludes_zero=False,
            insufficient_sample=reason == REASON_INSUFFICIENT_SAMPLE,
            min_sample_size=min_sample_size,
            confidence=confidence,
            periods_per_year=periods_per_year,
            n_bootstrap=n_bootstrap,
            seed=seed,
            unavailable_reason=reason,
        )

    if n < min_sample_size:
        return _gated(REASON_INSUFFICIENT_SAMPLE)

    point = annualized_sharpe_ratio(array, risk_free_rate, periods_per_year)
    if point is None:
        return _gated(REASON_ZERO_VARIANCE)

    if n_bootstrap < 1:
        raise PerformanceStatsError("n_bootstrap must be >= 1 for an interval")

    per_period_rf = float(risk_free_rate) / periods_per_year
    excess = array - per_period_rf
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, n, size=(n_bootstrap, n))
    samples = excess[indices]
    means = samples.mean(axis=1)
    stds = samples.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpes = np.where(stds > 0, means / stds * math.sqrt(periods_per_year), np.nan)
    finite = sharpes[np.isfinite(sharpes)]
    if finite.size == 0:
        return _gated(REASON_BOOTSTRAP_DEGENERATE)

    alpha = (1.0 - confidence) / 2.0
    lower = float(np.quantile(finite, alpha))
    upper = float(np.quantile(finite, 1.0 - alpha))
    return SharpeResult(
        point_estimate=float(point),
        lower_bound=lower,
        upper_bound=upper,
        n_observations=n,
        excludes_zero=bool(lower > 0.0 or upper < 0.0),
        insufficient_sample=False,
        min_sample_size=min_sample_size,
        confidence=confidence,
        periods_per_year=periods_per_year,
        n_bootstrap=n_bootstrap,
        seed=seed,
        unavailable_reason=None,
    )


# --- Win rate -----------------------------------------------------------------


def wilson_score_interval(
    successes: int,
    n: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion, clamped to [0, 1].

    Correct for small N and near the 0/1 boundaries, where the normal
    (Wald) approximation degenerates — most visibly at 2 successes out of 2,
    which Wald reports as the nonsense interval [1.0, 1.0].

    Ungated on purpose: this is the raw statistic. Reporting must go through
    :func:`win_rate_with_interval`, which applies the sample gate.
    """
    successes = _coerce_count(successes, "successes")
    n = _coerce_count(n, "n")
    if successes > n:
        raise PerformanceStatsError(f"successes ({successes}) cannot exceed n ({n})")
    if n == 0:
        return (0.0, 1.0)
    z = _z_for_confidence(confidence)
    z2 = z * z
    denominator = n + z2
    center = (successes + z2 / 2.0) / denominator
    margin = (z / denominator) * math.sqrt(
        successes * (n - successes) / n + z2 / 4.0
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def win_rate_with_interval(
    hits: int,
    misses: int,
    confidence: float = DEFAULT_CONFIDENCE,
    *,
    min_sample_size: int = MIN_WIN_RATE_SAMPLE_SIZE,
) -> WinRateResult:
    """Win rate ``hit / (hit + miss)`` with a Wilson score interval.

    Neutrals are **not** arguments here: the repo convention (see
    ``decision_signal_outcome_service._aggregate`` and
    ``scripts/build_calibration_curve.py``) excludes them from the win-rate
    denominator. They must still be reported next to the rate — see
    :func:`summary`, which carries ``neutral_share`` alongside.

    Below ``min_sample_size`` decided outcomes the point estimate is ``None``
    and ``insufficient_sample`` is True. A hard gate, same as
    :func:`sharpe_ratio_with_ci`: no percentage is returned for a sample that
    cannot support one, no matter how tempting "2 for 2" looks.
    """
    hits = _coerce_count(hits, "hits")
    misses = _coerce_count(misses, "misses")
    confidence = float(confidence)
    _z_for_confidence(confidence)
    min_sample_size = _coerce_count(min_sample_size, "min_sample_size")
    n_decided = hits + misses

    if n_decided == 0 or n_decided < min_sample_size:
        return WinRateResult(
            point_estimate=None,
            lower_bound=None,
            upper_bound=None,
            hits=hits,
            misses=misses,
            n_decided=n_decided,
            excludes_coin_flip=False,
            insufficient_sample=True,
            min_sample_size=min_sample_size,
            confidence=confidence,
            unavailable_reason=(
                REASON_NO_DECIDED_OUTCOMES if n_decided == 0 else REASON_INSUFFICIENT_SAMPLE
            ),
        )

    lower, upper = wilson_score_interval(hits, n_decided, confidence)
    return WinRateResult(
        point_estimate=hits / n_decided,
        lower_bound=lower,
        upper_bound=upper,
        hits=hits,
        misses=misses,
        n_decided=n_decided,
        excludes_coin_flip=bool(lower > 0.5 or upper < 0.5),
        insufficient_sample=False,
        min_sample_size=min_sample_size,
        confidence=confidence,
        unavailable_reason=None,
    )


# --- Deflated Sharpe ----------------------------------------------------------


def deflated_sharpe_ratio(
    sharpe: float,
    n_trials: int,
    n_observations: int,
    *,
    skew: float = 0.0,
    excess_kurtosis: float = 0.0,
    min_sample_size: int = MIN_SHARPE_SAMPLE_SIZE,
) -> DeflatedSharpeResult:
    """Deflated Sharpe ratio (Bailey & Lopez de Prado, 2014).

    WHEN THIS APPLIES
    -----------------
    Only at the point where strategy or parameter variants are **actually
    backtested**. ``n_trials`` is the number of backtests that were RUN
    against data — every parameter combination, every variant, every re-run
    after a tweak, including the ones that were discarded.

    WHEN THIS MUST NOT BE APPLIED
    -----------------------------
    Never to strategy *documents*, write-ups, ideas, or a parameter grid that
    was merely enumerated. **A Sharpe cannot be deflated for hypotheses that
    were never tested.** Deflation corrects for selection bias: the inflation
    that occurs because the best of N *measured* results was picked. If nothing
    was measured there is no selection to correct, and passing a count of
    unexecuted documents as ``n_trials`` produces a number that looks like
    rigour while meaning nothing. Counting documents also cuts the wrong way —
    it deflates an honest single backtest for searching that never happened.

    UNITS
    -----
    ``sharpe`` must be the **non-annualized, per-observation** Sharpe over
    ``n_observations`` periods. Convert an annualized figure first with
    :func:`per_period_sharpe`; feeding an annualized value here silently
    overstates significance by ``sqrt(periods_per_year)``.

    METHOD
    ------
    ``E[max SR]`` under the null of zero true skill is approximated with the
    expected maximum of ``n_trials`` draws whose standard deviation is
    ``1 / sqrt(n_observations)``::

        E[max SR] ~ sigma * ((1 - g) * Phi^-1(1 - 1/N) + g * Phi^-1(1 - 1/(N*e)))

    with ``g`` the Euler-Mascheroni constant. The deflated Sharpe is then the
    probabilistic Sharpe ratio measured against that benchmark instead of
    against zero, and is a **probability** in [0, 1] that true skill exceeds
    what the search alone would produce. Same hard gate as everywhere else:
    below ``min_sample_size`` observations the result is ``None`` with
    ``insufficient_sample=True``.
    """
    sharpe = float(sharpe)
    if not math.isfinite(sharpe):
        raise PerformanceStatsError(f"sharpe must be finite, got {sharpe!r}")
    n_trials = _coerce_count(n_trials, "n_trials")
    n_observations = _coerce_count(n_observations, "n_observations")
    min_sample_size = _coerce_count(min_sample_size, "min_sample_size")
    if n_trials < 1:
        raise PerformanceStatsError(
            "n_trials must be >= 1 (the count of backtests actually run)"
        )

    if n_observations < max(2, min_sample_size):
        return DeflatedSharpeResult(
            deflated_sharpe=None,
            expected_max_sharpe_under_null=None,
            observed_sharpe=sharpe,
            n_trials=n_trials,
            n_observations=n_observations,
            insufficient_sample=True,
            min_sample_size=min_sample_size,
            unavailable_reason=REASON_INSUFFICIENT_SAMPLE,
        )

    sigma = 1.0 / math.sqrt(n_observations)
    if n_trials == 1:
        expected_max = 0.0
    else:
        expected_max = sigma * (
            (1.0 - EULER_MASCHERONI) * normal_ppf(1.0 - 1.0 / n_trials)
            + EULER_MASCHERONI * normal_ppf(1.0 - 1.0 / (n_trials * math.e))
        )

    variance_term = (
        1.0 - float(skew) * sharpe + (float(excess_kurtosis) - 1.0) / 4.0 * sharpe ** 2
    )
    if variance_term <= 0.0:
        return DeflatedSharpeResult(
            deflated_sharpe=None,
            expected_max_sharpe_under_null=expected_max,
            observed_sharpe=sharpe,
            n_trials=n_trials,
            n_observations=n_observations,
            insufficient_sample=False,
            min_sample_size=min_sample_size,
            unavailable_reason=REASON_ZERO_VARIANCE,
        )

    statistic = (sharpe - expected_max) * math.sqrt(n_observations - 1) / math.sqrt(
        variance_term
    )
    return DeflatedSharpeResult(
        deflated_sharpe=normal_cdf(statistic),
        expected_max_sharpe_under_null=expected_max,
        observed_sharpe=sharpe,
        n_trials=n_trials,
        n_observations=n_observations,
        insufficient_sample=False,
        min_sample_size=min_sample_size,
        unavailable_reason=None,
    )


def per_period_sharpe(
    annualized_sharpe: float,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
) -> float:
    """De-annualize a Sharpe ratio for :func:`deflated_sharpe_ratio`."""
    periods_per_year = float(periods_per_year)
    if periods_per_year <= 0:
        raise PerformanceStatsError("periods_per_year must be > 0")
    return float(annualized_sharpe) / math.sqrt(periods_per_year)


# --- Reporting ----------------------------------------------------------------


def _count_outcomes(outcomes: Optional[Iterable[str]]) -> Counter:
    counts: Counter = Counter()
    for raw in outcomes or ():
        value = str(raw or "").strip().lower()
        if value not in OUTCOME_VALUES:
            raise PerformanceStatsError(
                f"unknown outcome {raw!r}; expected one of {sorted(OUTCOME_VALUES)}"
            )
        counts[value] += 1
    return counts


def _count_unable_reasons(
    unable_reasons: Optional[Union[Iterable[str], Mapping[str, int]]]
) -> Dict[str, int]:
    counts: Counter = Counter()
    if unable_reasons is None:
        return {}
    if isinstance(unable_reasons, Mapping):
        for reason, count in unable_reasons.items():
            key = str(reason or "").strip().lower() or UNKNOWN_UNABLE_REASON
            counts[key] += _coerce_count(count, f"unable_reasons[{reason!r}]")
    else:
        for reason in unable_reasons:
            key = str(reason or "").strip().lower() or UNKNOWN_UNABLE_REASON
            counts[key] += 1
    return dict(sorted(counts.items()))


def _assert_summary_contract(payload: Mapping[str, Any]) -> None:
    """Fail loudly rather than emit a rate without its denominator.

    This is the structural half of the module's promise: a refactor that drops
    N, the ``unable`` breakdown or the neutral share from the payload breaks
    here instead of quietly shipping a bare percentage to a report.
    """
    missing = [key for key in REQUIRED_SUMMARY_KEYS if key not in payload]
    if missing:
        raise PerformanceStatsError(
            f"summary payload is missing required reporting keys: {missing}"
        )


def summary(
    outcomes: Optional[Sequence[str]] = None,
    unable_reasons: Optional[Union[Sequence[str], Mapping[str, int]]] = None,
    returns: Optional[Sequence[float]] = None,
    *,
    risk_free_rate: float = 0.0,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
    n_bootstrap: int = DEFAULT_N_BOOTSTRAP,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: Optional[int] = DEFAULT_BOOTSTRAP_SEED,
    min_win_rate_sample_size: int = MIN_WIN_RATE_SAMPLE_SIZE,
    min_sharpe_sample_size: int = MIN_SHARPE_SAMPLE_SIZE,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a reporting payload where every statistic carries its denominator.

    ``outcomes`` is a sequence of ``completed`` outcome values
    (``hit`` / ``miss`` / ``neutral``); ``unable_reasons`` is either a sequence
    of reason strings or a ``{reason: count}`` mapping for the outcomes whose
    ``eval_status`` was ``unable``. ``returns`` is an optional per-period
    return series for the Sharpe block.

    The payload ALWAYS contains ``total`` (N including unable),
    ``completed``, ``decided``, ``unable``, ``unable_reasons`` (count by
    reason) and ``neutral_share`` — even when every statistic is gated off. A
    consumer therefore cannot render a rate without also holding its
    denominator and the coverage it was computed over.
    """
    counts = _count_outcomes(outcomes)
    hit = counts.get("hit", 0)
    miss = counts.get("miss", 0)
    neutral = counts.get("neutral", 0)
    completed = hit + miss + neutral
    decided = hit + miss
    unable_breakdown = _count_unable_reasons(unable_reasons)
    unable = sum(unable_breakdown.values())
    total = completed + unable

    win_rate = win_rate_with_interval(
        hit,
        miss,
        confidence,
        min_sample_size=min_win_rate_sample_size,
    )
    sharpe = sharpe_ratio_with_ci(
        returns,
        risk_free_rate,
        periods_per_year,
        n_bootstrap,
        confidence,
        seed=seed,
        min_sample_size=min_sharpe_sample_size,
    )

    notes: List[str] = []
    if win_rate.point_estimate is None:
        notes.append(
            f"No win rate reported: {decided} decided outcome(s) "
            f"(hit+miss) vs a minimum of {win_rate.min_sample_size}."
        )
    if sharpe.point_estimate is None:
        notes.append(
            f"No Sharpe reported: {sharpe.n_observations} return observation(s) "
            f"vs a minimum of {sharpe.min_sample_size}"
            + (
                "."
                if sharpe.unavailable_reason == REASON_INSUFFICIENT_SAMPLE
                else f" (reason: {sharpe.unavailable_reason})."
            )
        )
    if completed and neutral == completed:
        notes.append("Every completed outcome was neutral; nothing was decided.")
    if unable:
        notes.append(
            f"{unable} of {total} outcome(s) could not be evaluated; "
            "treat coverage, not just the rate, as the finding."
        )

    payload: Dict[str, Any] = {
        "label": label,
        # Denominators and coverage first: they are not optional context.
        "total": total,
        "completed": completed,
        "decided": decided,
        "unable": unable,
        "hit": hit,
        "miss": miss,
        "neutral": neutral,
        "unable_reasons": unable_breakdown,
        "neutral_share": (neutral / completed) if completed else None,
        "coverage": {
            "completed_share": (completed / total) if total else None,
            "unable_share": (unable / total) if total else None,
            "decided_share": (decided / completed) if completed else None,
            "n_return_observations": sharpe.n_observations,
        },
        # Statistics, each already gated and each carrying its own N.
        "win_rate": win_rate.to_dict(),
        "sharpe": sharpe.to_dict(),
        "min_sample_sizes": {
            "win_rate_decided": win_rate.min_sample_size,
            "sharpe_observations": sharpe.min_sample_size,
        },
        "confidence": float(confidence),
        "reportable": bool(
            win_rate.point_estimate is not None or sharpe.point_estimate is not None
        ),
        "notes": notes,
    }
    _assert_summary_contract(payload)
    return payload
