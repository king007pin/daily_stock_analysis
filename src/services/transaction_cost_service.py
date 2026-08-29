# -*- coding: utf-8 -*-
"""Indian equity (NSE) transaction-cost model.

Purpose
-------
Every return figure this system currently produces is **gross**. This service turns a
gross price move into a net one by pricing the full statutory + broker cost stack of an
NSE cash-segment order, and — most usefully — by answering the only question that
matters before a ticket is taken:

    "How far does the price have to move before I am merely flat?"

That is :meth:`TransactionCostService.breakeven_move_pct`.

Zero-Hallucination Invariant (AGENTS.md section 1.3)
---------------------------------------------------
**No rate is shipped as a default. A remembered rate is a fabricated rate.**

Statutory and brokerage rates change by circular, by segment, by product type, by
state (stamp duty) and by broker plan. A rate recalled from model priors — even one
that happens to be right today — is an unsourced number masquerading as a fact, and it
would silently poison every net-return figure downstream. So every rate field on
:class:`CostSchedule` defaults to ``None``, and computing with an unset rate raises
:class:`MissingRateError` instead of quietly treating it as zero.

Rates the operator MUST source before this service can compute anything
----------------------------------------------------------------------
All rates are **fractions of turnover** (turnover = quantity x price), not percentages:
a published "0.1%" is entered as ``0.001``; "0.00325%" is entered as ``0.0000325``.
The single exception is ``brokerage_cap_inr``, which is an absolute rupee amount per
order, and ``gst_rate``, which is a fraction of a fee base rather than of turnover.

===========================  =========================================================
Field                        Authority the operator must read the number from
===========================  =========================================================
brokerage_rate               The broker's own published tariff / schedule of charges.
brokerage_cap_inr            The broker's own published tariff (the per-order flat cap;
                             Indian discount brokers charge min(pct, flat cap)).
stt_mis_buy_rate             Securities Transaction Tax — STT Act / CBDT notification,
stt_mis_sell_rate            as republished in the exchange's current charges circular.
stt_cnc_buy_rate             Buy-side and sell-side rates differ, and intraday (MIS)
stt_cnc_sell_rate            differs from delivery (CNC). Source all four separately.
exchange_txn_rate            NSE circular on transaction charges (cash segment).
sebi_turnover_rate           SEBI turnover fee circular.
stamp_duty_buy_rate          Indian Stamp Act uniform rate notification (buy side only).
gst_rate                     GST notification / exchange charges circular.
===========================  =========================================================

If a sourced schedule genuinely states a rate is zero for a leg (for example, a
side on which STT is not levied), the operator sets that field to ``0.0`` explicitly.
That zero is then a *sourced* zero, recorded deliberately — which is a different thing
from an unset field, and this module keeps the two distinguishable.

Cost stack applied
------------------
For one order of ``quantity`` shares at ``price`` (turnover = quantity x price):

* ``brokerage``   = min(turnover x brokerage_rate, brokerage_cap_inr)
* ``stt``         = turnover x (STT rate selected by side AND product type)
* ``exchange``    = turnover x exchange_txn_rate                 (both sides)
* ``sebi``        = turnover x sebi_turnover_rate                (both sides)
* ``stamp_duty``  = turnover x stamp_duty_buy_rate               (**buy side only**)
* ``gst``         = gst_rate x (brokerage + exchange + sebi)
  — GST applies to the *service fee* base only. It is **not** levied on STT and not on
  stamp duty, both of which are taxes rather than services.
* ``total``       = sum of the above.

Config wiring
-------------
:meth:`CostSchedule.from_config` reads ``txn_cost_<field>`` attributes off any config
object via ``getattr(..., None)``. This module deliberately does not touch
``src/config.py``; the operator wires the config surface separately.
"""

from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields
from typing import Any, Optional

__all__ = [
    "MissingRateError",
    "CostSchedule",
    "CostBreakdown",
    "RoundTripCost",
    "TransactionCostService",
    "RATE_SOURCES",
    "SIDE_BUY",
    "SIDE_SELL",
    "PRODUCT_MIS",
    "PRODUCT_CNC",
]

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
PRODUCT_MIS = "MIS"  # intraday
PRODUCT_CNC = "CNC"  # delivery

_VALID_SIDES = (SIDE_BUY, SIDE_SELL)
_VALID_PRODUCTS = (PRODUCT_MIS, PRODUCT_CNC)

CONFIG_PREFIX = "txn_cost_"

#: Field name -> the authority the operator must source that rate from.
#: Used verbatim in :class:`MissingRateError` messages so the failure is actionable.
RATE_SOURCES: dict[str, str] = {
    "brokerage_rate": "broker's own published tariff / schedule of charges",
    "brokerage_cap_inr": "broker's own published tariff (per-order flat cap, in INR)",
    "stt_mis_buy_rate": "STT Act / CBDT notification as republished in the exchange charges circular (intraday buy leg)",
    "stt_mis_sell_rate": "STT Act / CBDT notification as republished in the exchange charges circular (intraday sell leg)",
    "stt_cnc_buy_rate": "STT Act / CBDT notification as republished in the exchange charges circular (delivery buy leg)",
    "stt_cnc_sell_rate": "STT Act / CBDT notification as republished in the exchange charges circular (delivery sell leg)",
    "exchange_txn_rate": "NSE circular on cash-segment transaction charges",
    "sebi_turnover_rate": "SEBI turnover fee circular",
    "stamp_duty_buy_rate": "Indian Stamp Act uniform rate notification (buy side only)",
    "gst_rate": "GST notification / exchange charges circular",
}


class MissingRateError(ValueError):
    """Raised when a cost computation needs a rate the operator has not sourced.

    Carries the unset field names so callers (and operators) can see exactly what to
    fill in, and from which authority. Never fall back to a default: an unset rate is
    missing information, not zero.
    """

    def __init__(self, missing: list[str], *, side: str, product_type: str) -> None:
        self.missing = list(missing)
        self.side = side
        self.product_type = product_type
        detail = "\n".join(
            f"  - {name}: source from {RATE_SOURCES.get(name, 'the applicable circular / broker tariff')}"
            for name in self.missing
        )
        super().__init__(
            f"Cannot compute transaction cost for side={side} product_type={product_type}: "
            f"{len(self.missing)} required rate(s) are unset on CostSchedule.\n"
            f"{detail}\n"
            "No default is supplied on purpose (AGENTS.md 1.3): a remembered rate is a "
            "fabricated rate. Set each field from its authority, or set it to 0.0 only if "
            "the sourced schedule states the levy is zero for this leg."
        )


@dataclass(frozen=True)
class CostSchedule:
    """Rate inputs for the NSE cash-segment cost stack.

    Every field defaults to ``None`` (= not sourced yet). See the module docstring for
    the authority behind each rate. All rates except ``brokerage_cap_inr`` (absolute
    INR) and ``gst_rate`` (fraction of the fee base) are fractions of turnover.
    """

    # Broker tariff: Indian discount brokers charge min(percentage, flat cap) per order.
    brokerage_rate: Optional[float] = None
    brokerage_cap_inr: Optional[float] = None

    # Securities Transaction Tax: separate per side AND per product type.
    stt_mis_buy_rate: Optional[float] = None
    stt_mis_sell_rate: Optional[float] = None
    stt_cnc_buy_rate: Optional[float] = None
    stt_cnc_sell_rate: Optional[float] = None

    # Exchange + regulator levies (both sides).
    exchange_txn_rate: Optional[float] = None
    sebi_turnover_rate: Optional[float] = None

    # Stamp duty is levied on the buy side only.
    stamp_duty_buy_rate: Optional[float] = None

    # GST on (brokerage + exchange + SEBI fee) only.
    gst_rate: Optional[float] = None

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in dataclass_fields(cls))

    @classmethod
    def from_config(cls, config: Any) -> "CostSchedule":
        """Build a schedule from ``txn_cost_<field>`` attributes on a config object.

        Missing attributes, ``None`` and blank strings all stay ``None`` (not sourced).
        A present-but-unparseable value is an error, not a silent ``None`` — a typo in a
        tax rate must never degrade into "cost = 0".
        """
        values: dict[str, Optional[float]] = {}
        for name in cls.field_names():
            raw = getattr(config, f"{CONFIG_PREFIX}{name}", None)
            values[name] = _coerce_optional_float(raw, f"{CONFIG_PREFIX}{name}")
        return cls(**values)

    def stt_rate_for(self, side: str, product_type: str) -> Optional[float]:
        """STT rate for a (side, product) leg. ``None`` means not sourced."""
        side = _normalise_side(side)
        product_type = _normalise_product(product_type)
        key = f"stt_{product_type.lower()}_{side.lower()}_rate"
        return getattr(self, key)

    def required_fields(self, side: str, product_type: str) -> tuple[str, ...]:
        """Field names this schedule must have set to price the given leg."""
        side = _normalise_side(side)
        product_type = _normalise_product(product_type)
        required = [
            "brokerage_rate",
            "brokerage_cap_inr",
            "exchange_txn_rate",
            "sebi_turnover_rate",
            "gst_rate",
            f"stt_{product_type.lower()}_{side.lower()}_rate",
        ]
        if side == SIDE_BUY:
            required.append("stamp_duty_buy_rate")
        return tuple(required)

    def missing_fields(self, side: str, product_type: str) -> tuple[str, ...]:
        return tuple(name for name in self.required_fields(side, product_type) if getattr(self, name) is None)

    def validate_for(self, side: str, product_type: str) -> None:
        """Raise :class:`MissingRateError` if any rate needed for this leg is unset."""
        missing = self.missing_fields(side, product_type)
        if missing:
            raise MissingRateError(list(missing), side=_normalise_side(side), product_type=_normalise_product(product_type))

    def to_dict(self) -> dict[str, Optional[float]]:
        return {name: getattr(self, name) for name in self.field_names()}


@dataclass(frozen=True)
class CostBreakdown:
    """Itemised cost of a single order leg. All money values in INR."""

    side: str
    product_type: str
    quantity: int
    price: float
    turnover: float
    brokerage: float
    stt: float
    exchange_charge: float
    sebi_fee: float
    stamp_duty: float
    gst: float
    total: float

    @property
    def gst_base(self) -> float:
        """The base GST was applied to: brokerage + exchange charge + SEBI fee."""
        return self.brokerage + self.exchange_charge + self.sebi_fee

    @property
    def cost_pct_of_turnover(self) -> float:
        """Leg cost as a percentage of leg turnover."""
        if self.turnover <= 0:
            return 0.0
        return self.total / self.turnover * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "product_type": self.product_type,
            "quantity": self.quantity,
            "price": self.price,
            "turnover": self.turnover,
            "brokerage": self.brokerage,
            "stt": self.stt,
            "exchange_charge": self.exchange_charge,
            "sebi_fee": self.sebi_fee,
            "stamp_duty": self.stamp_duty,
            "gst": self.gst,
            "total": self.total,
            "cost_pct_of_turnover": self.cost_pct_of_turnover,
        }


@dataclass(frozen=True)
class RoundTripCost:
    """Entry + exit cost for one position."""

    entry: CostBreakdown
    exit: CostBreakdown
    total: float

    @property
    def entry_turnover(self) -> float:
        return self.entry.turnover

    @property
    def cost_pct_of_entry_turnover(self) -> float:
        if self.entry.turnover <= 0:
            return 0.0
        return self.total / self.entry.turnover * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry.to_dict(),
            "exit": self.exit.to_dict(),
            "total": self.total,
            "entry_turnover": self.entry_turnover,
            "cost_pct_of_entry_turnover": self.cost_pct_of_entry_turnover,
        }


class TransactionCostService:
    """Pure, offline transaction-cost calculator for NSE cash-segment equity orders.

    Stateless: every method is a pure function of its arguments plus the supplied
    :class:`CostSchedule`. No network, no DB, no config import.
    """

    # Fixed-point tolerance/iteration cap for the breakeven solve. The cost fractions
    # involved are tiny, so the iteration is a strong contraction and converges in a
    # handful of steps; the cap only guards against pathological schedules.
    _BREAKEVEN_TOL = 1e-12
    _BREAKEVEN_MAX_ITER = 64

    @staticmethod
    def compute_order_cost(
        side: str,
        quantity: int,
        price: float,
        product_type: str,
        schedule: CostSchedule,
    ) -> CostBreakdown:
        """Itemised cost of one order leg.

        Raises:
            MissingRateError: if any rate required for this leg is unset.
            ValueError: on an invalid side, product type, quantity or price.
        """
        side = _normalise_side(side)
        product_type = _normalise_product(product_type)
        quantity = _validate_quantity(quantity)
        price = _validate_price(price)

        schedule.validate_for(side, product_type)

        turnover = quantity * price

        pct_brokerage = turnover * float(schedule.brokerage_rate)  # type: ignore[arg-type]
        brokerage = min(pct_brokerage, float(schedule.brokerage_cap_inr))  # type: ignore[arg-type]

        stt_rate = schedule.stt_rate_for(side, product_type)
        stt = turnover * float(stt_rate)  # type: ignore[arg-type]

        exchange_charge = turnover * float(schedule.exchange_txn_rate)  # type: ignore[arg-type]
        sebi_fee = turnover * float(schedule.sebi_turnover_rate)  # type: ignore[arg-type]

        # Stamp duty is a buy-side levy only.
        stamp_duty = turnover * float(schedule.stamp_duty_buy_rate) if side == SIDE_BUY else 0.0  # type: ignore[arg-type]

        # GST applies to the service-fee base only — never to STT or stamp duty.
        gst = float(schedule.gst_rate) * (brokerage + exchange_charge + sebi_fee)  # type: ignore[arg-type]

        total = brokerage + stt + exchange_charge + sebi_fee + stamp_duty + gst

        return CostBreakdown(
            side=side,
            product_type=product_type,
            quantity=quantity,
            price=price,
            turnover=turnover,
            brokerage=brokerage,
            stt=stt,
            exchange_charge=exchange_charge,
            sebi_fee=sebi_fee,
            stamp_duty=stamp_duty,
            gst=gst,
            total=total,
        )

    @classmethod
    def round_trip_cost(
        cls,
        quantity: int,
        entry_price: float,
        exit_price: float,
        product_type: str,
        schedule: CostSchedule,
        *,
        entry_side: str = SIDE_BUY,
    ) -> RoundTripCost:
        """Entry + exit cost for a position.

        ``entry_side`` defaults to ``BUY`` (long); pass ``SELL`` for a short, in which
        case the entry leg is the sell and the exit leg is the buy.
        """
        entry_side = _normalise_side(entry_side)
        exit_side = SIDE_SELL if entry_side == SIDE_BUY else SIDE_BUY

        entry = cls.compute_order_cost(entry_side, quantity, entry_price, product_type, schedule)
        exit_leg = cls.compute_order_cost(exit_side, quantity, exit_price, product_type, schedule)
        return RoundTripCost(entry=entry, exit=exit_leg, total=entry.total + exit_leg.total)

    @classmethod
    def breakeven_move_pct(
        cls,
        quantity: int,
        entry_price: float,
        product_type: str,
        schedule: CostSchedule,
        *,
        entry_side: str = SIDE_BUY,
    ) -> float:
        """Percentage price move required just to cover round-trip costs.

        For a long: the percentage the price must rise above ``entry_price`` before the
        position is flat net of all costs. For a short: the percentage it must fall
        (returned as a positive number — it is a required *move*, not a signed return).

        The exit leg's costs scale with the exit price, which itself depends on the
        answer, so this is solved by fixed-point iteration rather than a closed form.
        A closed form exists only while the brokerage cap is not binding; the iteration
        handles the capped (piecewise) case correctly too.

        Note on leverage: MIS leverage changes the capital deployed, not the notional
        turnover the levies are charged on. The breakeven *price move* is therefore
        independent of leverage; leverage magnifies the return on capital on both sides
        of that breakeven.
        """
        entry_side = _normalise_side(entry_side)
        product_type = _normalise_product(product_type)
        quantity = _validate_quantity(quantity)
        entry_price = _validate_price(entry_price)

        # Validate both legs up front so a missing rate fails loudly before iterating.
        exit_side = SIDE_SELL if entry_side == SIDE_BUY else SIDE_BUY
        schedule.validate_for(entry_side, product_type)
        schedule.validate_for(exit_side, product_type)

        entry_turnover = quantity * entry_price
        entry_cost = cls.compute_order_cost(entry_side, quantity, entry_price, product_type, schedule).total

        direction = 1.0 if entry_side == SIDE_BUY else -1.0
        move = 0.0
        for _ in range(cls._BREAKEVEN_MAX_ITER):
            exit_price = entry_price * (1.0 + direction * move)
            if exit_price <= 0:
                raise ValueError("Breakeven solve diverged: implied exit price is non-positive.")
            exit_cost = cls.compute_order_cost(exit_side, quantity, exit_price, product_type, schedule).total
            next_move = (entry_cost + exit_cost) / entry_turnover
            if abs(next_move - move) <= cls._BREAKEVEN_TOL:
                move = next_move
                break
            move = next_move
        else:
            raise ValueError("Breakeven solve did not converge; check the CostSchedule for implausible rates.")

        return move * 100.0

    @classmethod
    def net_pnl(
        cls,
        quantity: int,
        entry_price: float,
        exit_price: float,
        product_type: str,
        schedule: CostSchedule,
        *,
        entry_side: str = SIDE_BUY,
    ) -> dict[str, Any]:
        """Gross P&L, round-trip cost and net P&L for a completed trade, in INR."""
        entry_side = _normalise_side(entry_side)
        trip = cls.round_trip_cost(
            quantity, entry_price, exit_price, product_type, schedule, entry_side=entry_side
        )
        direction = 1.0 if entry_side == SIDE_BUY else -1.0
        gross = direction * (exit_price - entry_price) * quantity
        net = gross - trip.total
        entry_turnover = trip.entry.turnover
        return {
            "gross_pnl": gross,
            "cost": trip.total,
            "net_pnl": net,
            "gross_pct": (gross / entry_turnover * 100.0) if entry_turnover else 0.0,
            "net_pct": (net / entry_turnover * 100.0) if entry_turnover else 0.0,
            "round_trip": trip.to_dict(),
        }


def _normalise_side(side: str) -> str:
    if not isinstance(side, str):
        raise ValueError(f"side must be a string, got {type(side).__name__}")
    value = side.strip().upper()
    if value not in _VALID_SIDES:
        raise ValueError(f"side must be one of {_VALID_SIDES}, got {side!r}")
    return value


def _normalise_product(product_type: str) -> str:
    if not isinstance(product_type, str):
        raise ValueError(f"product_type must be a string, got {type(product_type).__name__}")
    value = product_type.strip().upper()
    if value not in _VALID_PRODUCTS:
        raise ValueError(f"product_type must be one of {_VALID_PRODUCTS} (MIS=intraday, CNC=delivery), got {product_type!r}")
    return value


def _validate_quantity(quantity: int) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError(f"quantity must be a positive int, got {quantity!r}")
    if quantity <= 0:
        raise ValueError(f"quantity must be a positive int, got {quantity!r}")
    return quantity


def _validate_price(price: float) -> float:
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        raise ValueError(f"price must be a positive number, got {price!r}")
    value = float(price)
    if value <= 0:
        raise ValueError(f"price must be a positive number, got {price!r}")
    return value


def _coerce_optional_float(raw: Any, attr_name: str) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{attr_name} must be a number or unset, got bool {raw!r}")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError as exc:
            raise ValueError(
                f"{attr_name} is set to {raw!r}, which is not a number. "
                "Fix the configured value; it must not silently degrade to an unset rate."
            ) from exc
    raise ValueError(f"{attr_name} must be a number or unset, got {type(raw).__name__}")
