# -*- coding: utf-8 -*-
"""
====================================================================
Portfolio Risk Limit Service (fail-CLOSED position admission gate)
====================================================================

Answers one question: "may this proposed position be opened, given everything
the portfolio is already carrying?"

Scope. This is the PORTFOLIO layer. The instrument layer already exists and is
NOT duplicated here: ``KronosForecaster`` computes ``circuit_buffer_pct`` /
``circuit_risk_flag`` (``src/services/kronos_service.py``) and
``BrokerExecutionService.create_bracket_order`` refuses sub-Rs10 names whose
circuit buffer is too narrow (``src/services/broker_service.py``). That gate
looks at one symbol. Nothing looked at the book as a whole: drawdown, daily
loss, gross exposure, position count, sector concentration, correlation
clustering, and per-trade risk as a fraction of equity. That is this file.

Design rules:
1. FAIL-CLOSED, in the same posture as ``src/services/nse_trading_day_guard.py``.
   Every exit that is not a positive, fully-evaluated "no limit was breached"
   returns ``allowed=False``. Missing input, unparseable number, or any internal
   exception maps to ``allowed=False`` with an explicit reason. There is no code
   path on which an error yields ``allowed=True``.
2. ALL breaches are reported, never just the first. A verdict carries every
   limit that tripped, so the operator sees the whole picture in one pass
   instead of fixing one breach and immediately hitting the next.
3. Every limit is individually disableable by setting it to ``None``, and a
   fully-``None`` :class:`RiskLimits` allows everything. That is deliberate: the
   service can be wired into the execution path first and configured second,
   without changing behaviour on the day it lands. Rule 1 still wins over rule
   3 for structurally broken input (unparseable equity, price or quantity) -
   those are refused even when no limit is configured, because "no limits
   configured" is not a licence to act on numbers we could not read.
4. Per-trade risk is (entry - stop) * quantity measured against CURRENT EQUITY,
   not a fixed cash amount and not position notional. A position with no
   stop-loss has unbounded risk, cannot be sized, and is therefore a breach of
   ``max_risk_per_trade_pct`` whenever that limit is configured.
5. Comparison polarity: the count/exposure/risk limits are ceilings, so a value
   exactly AT the limit passes and only ``>`` breaches. The two loss limits
   (``max_drawdown_pct``, ``max_daily_loss_pct``) are kill switches, so ``>=``
   trips them - hitting the drawdown you promised to stop at IS the stop.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Canonical limit names. These strings are the stable contract: they appear in
# RiskBreach.limit, they match the RiskLimits field names, and (prefixed with
# "risk_limit_") they match the config attribute names read by from_config.
LIMIT_MAX_DRAWDOWN_PCT = "max_drawdown_pct"
LIMIT_MAX_RISK_PER_TRADE_PCT = "max_risk_per_trade_pct"
LIMIT_MAX_DAILY_LOSS_PCT = "max_daily_loss_pct"
LIMIT_MAX_GROSS_EXPOSURE_PCT = "max_gross_exposure_pct"
LIMIT_MAX_POSITIONS = "max_positions"
LIMIT_MAX_POSITIONS_PER_SECTOR = "max_positions_per_sector"
LIMIT_MAX_CORRELATED_POSITIONS = "max_correlated_positions"

# Not a configurable limit: the fail-closed bucket. Any exception, missing
# input or unreadable number is reported under this name so callers can tell
# "your risk budget is full" apart from "I could not evaluate your risk budget".
LIMIT_EVALUATION_ERROR = "evaluation_error"

# Config attribute prefix. from_config reads getattr(config, PREFIX + name, None).
CONFIG_PREFIX = "risk_limit_"

ALL_LIMIT_NAMES: Tuple[str, ...] = (
    LIMIT_MAX_DRAWDOWN_PCT,
    LIMIT_MAX_RISK_PER_TRADE_PCT,
    LIMIT_MAX_DAILY_LOSS_PCT,
    LIMIT_MAX_GROSS_EXPOSURE_PCT,
    LIMIT_MAX_POSITIONS,
    LIMIT_MAX_POSITIONS_PER_SECTOR,
    LIMIT_MAX_CORRELATED_POSITIONS,
)


class RiskInputError(ValueError):
    """A required input was absent or could not be read as a number.

    Raised internally and caught by the evaluation entrypoint, which converts it
    into an ``allowed=False`` verdict. It is never allowed to escape as a raw
    exception into an execution path, because a caller that forgot a try/except
    would then trade unguarded.
    """


# --------------------------------------------------------------------------
# Input coercion helpers. Every one of these raises rather than defaulting -
# silently substituting 0.0 for an unreadable price is exactly how a guard
# turns into a rubber stamp.
# --------------------------------------------------------------------------

def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off a dataclass, plain object or Mapping.

    Duck-typed on purpose so callers are not forced to import this module's
    dataclasses just to ask a question. Attribute access is NOT shielded here:
    if a caller's property raises, that exception propagates to the fail-closed
    handler in :meth:`RiskLimitService.evaluate_position`, which is where it
    belongs.
    """
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _require_float(value: Any, label: str) -> float:
    """Read a required number. Missing/unparseable/non-finite -> RiskInputError."""
    if value is None:
        raise RiskInputError(f"{label} is missing")
    if isinstance(value, bool):
        # bool is an int subclass; a True price is a bug, not a number.
        raise RiskInputError(f"{label} is a bool, not a number: {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise RiskInputError(f"{label} is not a number: {value!r}") from exc
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise RiskInputError(f"{label} is not finite: {value!r}")
    return parsed


def _optional_float(value: Any, label: str) -> Optional[float]:
    """Read an optional number. ``None`` stays ``None``; garbage still raises."""
    if value is None:
        return None
    return _require_float(value, label)


def _require_positive_int(value: Any, label: str) -> int:
    """Read a required whole count > 0."""
    if value is None:
        raise RiskInputError(f"{label} is missing")
    if isinstance(value, bool):
        raise RiskInputError(f"{label} is a bool, not a count: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RiskInputError(f"{label} is not an integer: {value!r}") from exc
    if parsed <= 0:
        raise RiskInputError(f"{label} must be > 0, got {parsed}")
    return parsed


def _limit_float(value: Any, label: str) -> Optional[float]:
    """Read a configured limit threshold. ``None`` means 'this limit is off'."""
    if value is None:
        return None
    return _require_float(value, f"limit {label}")


def _limit_int(value: Any, label: str) -> Optional[int]:
    """Read a configured integer limit. ``None`` means 'this limit is off'."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise RiskInputError(f"limit {label} is a bool, not a count: {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RiskInputError(f"limit {label} is not an integer: {value!r}") from exc


def _coerce_config_value(value: Any, name: str) -> Any:
    """Best-effort coercion for :meth:`RiskLimits.from_config`.

    Deliberately does NOT raise. A malformed config value must not crash process
    startup; it is carried through verbatim so that the first evaluation trips
    the fail-closed path and reports which limit is unreadable, instead of the
    limit quietly disappearing (which would be fail-OPEN).
    """
    if value is None:
        return None
    try:
        if name in (LIMIT_MAX_POSITIONS, LIMIT_MAX_POSITIONS_PER_SECTOR, LIMIT_MAX_CORRELATED_POSITIONS):
            if isinstance(value, bool):
                raise ValueError(f"{value!r} is a bool")
            return int(value)
        if isinstance(value, bool):
            raise ValueError(f"{value!r} is a bool")
        return float(value)
    except (TypeError, ValueError):
        logger.warning(
            "risk_limit_service: config %s%s=%r is not a valid number; kept as-is so evaluation fails closed",
            CONFIG_PREFIX, name, value,
        )
        return value


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskLimits:
    """Portfolio-level risk budget. Every field is optional; ``None`` disables it.

    All percentages are expressed as percent-of-equity (``2.0`` means 2%), never
    as cash. That is what makes the budget survive a change in account size.
    """

    # Peak-to-trough kill switch. Once the portfolio has given back this much
    # from its equity high-water mark, no new position may be opened.
    max_drawdown_pct: Optional[float] = None

    # Ceiling on (entry - stop) * quantity, as a percent of current equity.
    # This is the limit that makes a missing stop-loss a breach.
    max_risk_per_trade_pct: Optional[float] = None

    # Intraday kill switch: loss since the session's starting equity.
    max_daily_loss_pct: Optional[float] = None

    # Total notional (existing + proposed) as a percent of equity. Values above
    # 100 are normal and intended: 2x MIS intraday leverage is 200.
    max_gross_exposure_pct: Optional[float] = None

    # Ceilings on breadth and concentration.
    max_positions: Optional[int] = None
    max_positions_per_sector: Optional[int] = None

    # A cluster of names correlated at or above ``correlation_threshold`` is one
    # concentrated bet, not N independent ones. ``max_correlated_positions`` caps
    # the size of that cluster; without a threshold the cluster cannot be
    # measured, so the pair must be configured together.
    max_correlated_positions: Optional[int] = None
    correlation_threshold: Optional[float] = None

    @classmethod
    def from_config(cls, config: Any) -> "RiskLimits":
        """Build limits from ``risk_limit_*`` attributes on a config object.

        Reads every field via ``getattr(config, "risk_limit_<field>", None)``, so
        an unconfigured system yields all-``None`` limits and therefore unchanged
        behaviour. Never raises: an unreadable value is preserved verbatim and
        surfaces at evaluation time as a fail-closed refusal.
        """
        names = ALL_LIMIT_NAMES + ("correlation_threshold",)
        values = {
            name: _coerce_config_value(getattr(config, f"{CONFIG_PREFIX}{name}", None), name)
            for name in names
        }
        return cls(**values)

    def is_fully_disabled(self) -> bool:
        """True when no limit at all is configured (introduce-without-effect mode)."""
        return all(getattr(self, name) is None for name in ALL_LIMIT_NAMES)


# --------------------------------------------------------------------------
# Portfolio / proposal state
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Position:
    """One open position. Duck-typed equivalents (objects, dicts) also work."""
    stock_code: str
    quantity: float
    entry_price: float
    current_price: Optional[float] = None   # falls back to entry_price for exposure
    sector: Optional[str] = None
    stop_loss_price: Optional[float] = None


@dataclass(frozen=True)
class ProposedPosition:
    """The position being asked about. Not yet open."""
    stock_code: str
    quantity: float
    entry_price: float
    stop_loss_price: Optional[float] = None  # absent => unbounded risk => breach
    sector: Optional[str] = None


@dataclass(frozen=True)
class PortfolioState:
    """Current book. ``equity`` is the only always-required field.

    ``peak_equity`` and ``day_start_equity`` are required only when the drawdown
    and daily-loss limits respectively are configured; missing them with those
    limits on is a fail-closed refusal, not a silent skip.

    ``correlations`` maps an unordered symbol pair to its coefficient, e.g.
    ``{("IDEA", "YESBANK"): 0.91}``. Lookup is order-insensitive.
    """
    equity: float
    peak_equity: Optional[float] = None
    day_start_equity: Optional[float] = None
    positions: Sequence[Any] = field(default_factory=tuple)
    correlations: Optional[Mapping[Tuple[str, str], float]] = None


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskBreach:
    """One breached limit, with the numbers that produced the refusal."""
    limit: str
    reason: str
    observed: Optional[float] = None
    limit_value: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "limit": self.limit,
            "reason": self.reason,
            "observed": self.observed,
            "limit_value": self.limit_value,
        }


@dataclass(frozen=True)
class RiskVerdict:
    """Result of an admission check. ``allowed`` is authoritative."""
    allowed: bool
    breaches: Tuple[RiskBreach, ...] = ()

    @property
    def breached_limits(self) -> List[str]:
        """Names of every limit that tripped, in evaluation order."""
        return [b.limit for b in self.breaches]

    @property
    def reasons(self) -> List[str]:
        return [b.reason for b in self.breaches]

    def reason_for(self, limit: str) -> Optional[str]:
        for breach in self.breaches:
            if breach.limit == limit:
                return breach.reason
        return None

    def summary(self) -> str:
        if self.allowed:
            return "allowed"
        return "refused: " + "; ".join(self.reasons)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "breached_limits": self.breached_limits,
            "breaches": [b.to_dict() for b in self.breaches],
            "summary": self.summary(),
        }


def _refused(limit: str, reason: str) -> RiskVerdict:
    """Build a single-breach refusal. Used by the fail-closed handler."""
    return RiskVerdict(allowed=False, breaches=(RiskBreach(limit=limit, reason=reason),))


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------

class RiskLimitService:
    """Portfolio-level admission control for proposed positions.

    Usage::

        service = RiskLimitService(RiskLimits.from_config(config))
        verdict = service.evaluate_position(proposed, portfolio)
        if not verdict.allowed:
            logger.warning("position refused: %s", verdict.summary())
    """

    def __init__(self, limits: Optional[RiskLimits] = None) -> None:
        # No limits supplied == nothing configured == allow (rule 3). The object
        # is still constructible so it can be wired in before it is tuned.
        self.limits = limits if limits is not None else RiskLimits()

    # -- primary entry point -------------------------------------------------

    def evaluate_position(self, proposed: Any, portfolio: Any) -> RiskVerdict:
        """Evaluate a PROPOSED position against the current portfolio.

        Returns a :class:`RiskVerdict` whose ``allowed`` is True only when every
        configured limit was evaluated and none was breached. All breaches are
        reported, not just the first.

        FAIL-CLOSED: any missing input, unparseable number or internal exception
        yields ``allowed=False`` with an ``evaluation_error`` breach. This method
        does not raise.
        """
        try:
            breaches = self._collect_breaches(proposed, portfolio)
            return RiskVerdict(allowed=not breaches, breaches=tuple(breaches))
        except RiskInputError as exc:
            reason = f"fail-closed: {exc}"
            logger.warning("risk_limit_service.evaluate_position refused on input: %s", exc)
            return _refused(LIMIT_EVALUATION_ERROR, reason)
        except Exception as exc:  # noqa: BLE001 - fail-closed by design
            detail = " ".join(str(exc).split()) or type(exc).__name__
            reason = f"fail-closed: risk evaluation raised {type(exc).__name__}: {detail}"
            logger.warning("risk_limit_service.evaluate_position fail-closed: %s", exc)
            return _refused(LIMIT_EVALUATION_ERROR, reason)

    # Alias kept short for call sites that read better without the noun.
    evaluate = evaluate_position

    # -- checks --------------------------------------------------------------

    def _collect_breaches(self, proposed: Any, portfolio: Any) -> List[RiskBreach]:
        """Run every configured check. Raises RiskInputError on unusable input."""
        if proposed is None:
            raise RiskInputError("proposed position is missing")
        if portfolio is None:
            raise RiskInputError("portfolio state is missing")

        limits = self.limits

        # Structural inputs are validated even when no limit is configured: an
        # unreadable price is not something an all-None budget gets to wave through.
        equity = _require_float(_field(portfolio, "equity"), "portfolio.equity")
        if equity <= 0:
            raise RiskInputError(f"portfolio.equity must be > 0, got {equity}")

        symbol = _field(proposed, "stock_code") or _field(proposed, "symbol")
        entry_price = _require_float(_field(proposed, "entry_price"), "proposed.entry_price")
        if entry_price <= 0:
            raise RiskInputError(f"proposed.entry_price must be > 0, got {entry_price}")
        quantity = _require_float(_field(proposed, "quantity"), "proposed.quantity")
        if quantity <= 0:
            raise RiskInputError(f"proposed.quantity must be > 0, got {quantity}")

        positions = list(_field(portfolio, "positions") or ())

        breaches: List[RiskBreach] = []
        breaches.extend(self._check_drawdown(portfolio, equity, limits))
        breaches.extend(self._check_daily_loss(portfolio, equity, limits))
        breaches.extend(self._check_risk_per_trade(proposed, entry_price, quantity, equity, limits))
        breaches.extend(self._check_gross_exposure(entry_price, quantity, equity, positions, limits))
        breaches.extend(self._check_position_count(symbol, positions, limits))
        breaches.extend(self._check_sector_count(proposed, symbol, positions, limits))
        breaches.extend(self._check_correlation(portfolio, symbol, positions, limits))
        return breaches

    def _check_drawdown(self, portfolio: Any, equity: float, limits: RiskLimits) -> List[RiskBreach]:
        """Peak-to-trough kill switch. ``>=`` trips: the stop you set IS the stop."""
        cap = _limit_float(limits.max_drawdown_pct, LIMIT_MAX_DRAWDOWN_PCT)
        if cap is None:
            return []

        peak = _optional_float(_field(portfolio, "peak_equity"), "portfolio.peak_equity")
        if peak is None:
            raise RiskInputError(
                "portfolio.peak_equity is required while max_drawdown_pct is configured"
            )
        if peak <= 0:
            raise RiskInputError(f"portfolio.peak_equity must be > 0, got {peak}")

        drawdown_pct = max(0.0, (peak - equity) / peak * 100.0)
        if drawdown_pct >= cap:
            return [RiskBreach(
                limit=LIMIT_MAX_DRAWDOWN_PCT,
                reason=(
                    f"drawdown kill switch: portfolio is {drawdown_pct:.2f}% below its peak equity "
                    f"of {peak:.2f} (current {equity:.2f}), at or beyond the {cap:.2f}% limit"
                ),
                observed=round(drawdown_pct, 4),
                limit_value=cap,
            )]
        return []

    def _check_daily_loss(self, portfolio: Any, equity: float, limits: RiskLimits) -> List[RiskBreach]:
        """Session loss kill switch, measured from the day's starting equity."""
        cap = _limit_float(limits.max_daily_loss_pct, LIMIT_MAX_DAILY_LOSS_PCT)
        if cap is None:
            return []

        start = _optional_float(_field(portfolio, "day_start_equity"), "portfolio.day_start_equity")
        if start is None:
            raise RiskInputError(
                "portfolio.day_start_equity is required while max_daily_loss_pct is configured"
            )
        if start <= 0:
            raise RiskInputError(f"portfolio.day_start_equity must be > 0, got {start}")

        loss_pct = max(0.0, (start - equity) / start * 100.0)
        if loss_pct >= cap:
            return [RiskBreach(
                limit=LIMIT_MAX_DAILY_LOSS_PCT,
                reason=(
                    f"daily loss kill switch: down {loss_pct:.2f}% today "
                    f"({start:.2f} -> {equity:.2f}), at or beyond the {cap:.2f}% limit"
                ),
                observed=round(loss_pct, 4),
                limit_value=cap,
            )]
        return []

    def _check_risk_per_trade(
        self,
        proposed: Any,
        entry_price: float,
        quantity: float,
        equity: float,
        limits: RiskLimits,
    ) -> List[RiskBreach]:
        """Risk = |entry - stop| * qty, as a percent of CURRENT equity.

        A missing stop-loss is a breach rather than an input error: it is a
        property of the proposal, not of the plumbing, and the operator needs to
        see it alongside any other breach rather than as a lone plumbing fault.
        """
        cap = _limit_float(limits.max_risk_per_trade_pct, LIMIT_MAX_RISK_PER_TRADE_PCT)
        if cap is None:
            return []

        raw_stop = _field(proposed, "stop_loss_price")
        if raw_stop is None:
            return [RiskBreach(
                limit=LIMIT_MAX_RISK_PER_TRADE_PCT,
                reason=(
                    "no stop-loss on the proposed position: risk per trade is unbounded and cannot "
                    f"be sized against the {cap:.2f}% of equity limit"
                ),
                observed=None,
                limit_value=cap,
            )]

        stop = _require_float(raw_stop, "proposed.stop_loss_price")
        if stop <= 0:
            raise RiskInputError(f"proposed.stop_loss_price must be > 0, got {stop}")

        risk_cash = abs(entry_price - stop) * quantity
        risk_pct = risk_cash / equity * 100.0
        if risk_pct > cap:
            return [RiskBreach(
                limit=LIMIT_MAX_RISK_PER_TRADE_PCT,
                reason=(
                    f"risk per trade {risk_pct:.2f}% of equity exceeds the {cap:.2f}% limit "
                    f"(entry {entry_price:.2f}, stop {stop:.2f}, qty {quantity:g} "
                    f"=> {risk_cash:.2f} at risk on {equity:.2f} equity)"
                ),
                observed=round(risk_pct, 4),
                limit_value=cap,
            )]
        return []

    def _check_gross_exposure(
        self,
        entry_price: float,
        quantity: float,
        equity: float,
        positions: Iterable[Any],
        limits: RiskLimits,
    ) -> List[RiskBreach]:
        """Total notional including the proposal, as a percent of equity.

        Leverage lives here: with 2x MIS the configured ceiling is 200, not 100.
        """
        cap = _limit_float(limits.max_gross_exposure_pct, LIMIT_MAX_GROSS_EXPOSURE_PCT)
        if cap is None:
            return []

        gross = entry_price * abs(quantity)
        for index, pos in enumerate(positions):
            label = _field(pos, "stock_code") or f"positions[{index}]"
            qty = _require_float(_field(pos, "quantity"), f"position {label} quantity")
            # Mark to market where the caller supplied a live price; entry price
            # is the documented fallback, never an assumed 0.
            price_source = _field(pos, "current_price")
            if price_source is None:
                price_source = _field(pos, "entry_price")
            price = _require_float(price_source, f"position {label} price")
            gross += abs(qty) * abs(price)

        exposure_pct = gross / equity * 100.0
        if exposure_pct > cap:
            return [RiskBreach(
                limit=LIMIT_MAX_GROSS_EXPOSURE_PCT,
                reason=(
                    f"gross exposure {exposure_pct:.2f}% of equity exceeds the {cap:.2f}% limit "
                    f"({gross:.2f} notional on {equity:.2f} equity, including the proposed position)"
                ),
                observed=round(exposure_pct, 4),
                limit_value=cap,
            )]
        return []

    def _check_position_count(
        self,
        symbol: Optional[str],
        positions: Sequence[Any],
        limits: RiskLimits,
    ) -> List[RiskBreach]:
        """Breadth ceiling. Adding to a name already held does not add a slot."""
        cap = _limit_int(limits.max_positions, LIMIT_MAX_POSITIONS)
        if cap is None:
            return []

        held = {_field(pos, "stock_code") for pos in positions}
        held.discard(None)
        resulting = len(held) if symbol in held else len(held) + 1
        if resulting > cap:
            return [RiskBreach(
                limit=LIMIT_MAX_POSITIONS,
                reason=(
                    f"position count would become {resulting}, exceeding the maximum of {cap} "
                    f"open positions ({len(held)} already open)"
                ),
                observed=float(resulting),
                limit_value=float(cap),
            )]
        return []

    def _check_sector_count(
        self,
        proposed: Any,
        symbol: Optional[str],
        positions: Sequence[Any],
        limits: RiskLimits,
    ) -> List[RiskBreach]:
        """Sector concentration ceiling."""
        cap = _limit_int(limits.max_positions_per_sector, LIMIT_MAX_POSITIONS_PER_SECTOR)
        if cap is None:
            return []

        sector = _field(proposed, "sector")
        if sector is None or (isinstance(sector, str) and not sector.strip()):
            # Fail closed: an unlabelled name cannot be checked against a
            # per-sector cap, and "unknown" is not a free sector.
            raise RiskInputError(
                "proposed.sector is required while max_positions_per_sector is configured"
            )

        same_sector = {
            _field(pos, "stock_code")
            for pos in positions
            if _field(pos, "sector") == sector
        }
        same_sector.discard(None)
        resulting = len(same_sector) if symbol in same_sector else len(same_sector) + 1
        if resulting > cap:
            return [RiskBreach(
                limit=LIMIT_MAX_POSITIONS_PER_SECTOR,
                reason=(
                    f"sector '{sector}' would hold {resulting} positions, exceeding the maximum of "
                    f"{cap} per sector ({len(same_sector)} already open in that sector)"
                ),
                observed=float(resulting),
                limit_value=float(cap),
            )]
        return []

    def _check_correlation(
        self,
        portfolio: Any,
        symbol: Optional[str],
        positions: Sequence[Any],
        limits: RiskLimits,
    ) -> List[RiskBreach]:
        """Correlated names are one concentrated bet, not N independent ones.

        The cluster is the proposed position plus every open position whose
        absolute correlation with it is at or above ``correlation_threshold``.
        Its size is what the limit caps.
        """
        cap = _limit_int(limits.max_correlated_positions, LIMIT_MAX_CORRELATED_POSITIONS)
        if cap is None:
            return []

        threshold = _limit_float(limits.correlation_threshold, "correlation_threshold")
        if threshold is None:
            raise RiskInputError(
                "correlation_threshold is required while max_correlated_positions is configured"
            )

        correlations = _field(portfolio, "correlations")
        if not correlations and positions:
            # Fail closed: we were asked to cap correlated exposure but handed no
            # correlation data for a book that is not empty.
            raise RiskInputError(
                "portfolio.correlations is required while max_correlated_positions is configured "
                "and the portfolio holds open positions"
            )

        cluster = [symbol]
        for pos in positions:
            other = _field(pos, "stock_code")
            if other is None or other == symbol:
                continue
            # A pair absent from the map is treated as uncorrelated (0.0). This is
            # the one deliberate default in this module: requiring a fully dense
            # N x N matrix would make the limit unusable in practice. The map
            # being entirely absent is still refused above.
            rho = _pair_correlation(correlations, symbol, other)
            if rho is not None and abs(rho) >= threshold:
                cluster.append(other)

        if len(cluster) > cap:
            names = ", ".join(str(name) for name in cluster)
            return [RiskBreach(
                limit=LIMIT_MAX_CORRELATED_POSITIONS,
                reason=(
                    f"correlated cluster of {len(cluster)} positions at correlation >= {threshold:.2f} "
                    f"({names}) exceeds the maximum of {cap}; these move as one concentrated bet"
                ),
                observed=float(len(cluster)),
                limit_value=float(cap),
            )]
        return []


def _pair_correlation(
    correlations: Any,
    left: Optional[str],
    right: Optional[str],
) -> Optional[float]:
    """Order-insensitive pairwise lookup. Returns ``None`` when the pair is absent."""
    if not correlations or left is None or right is None:
        return None
    for key in ((left, right), (right, left)):
        if key in correlations:
            return _require_float(correlations[key], f"correlation {left}/{right}")
    # Nested-mapping form: {"IDEA": {"YESBANK": 0.91}}
    for outer, inner in ((left, right), (right, left)):
        block = correlations.get(outer) if isinstance(correlations, Mapping) else None
        if isinstance(block, Mapping) and inner in block:
            return _require_float(block[inner], f"correlation {left}/{right}")
    return None
