# -*- coding: utf-8 -*-
"""Offline tests for the NSE transaction-cost model.

Every rate used here is a **synthetic placeholder invented for arithmetic convenience**.
None of these numbers is a real STT / exchange / SEBI / stamp-duty / GST / brokerage
rate, none was recalled from memory as a real rate, and none must ever be copied into
config or shipped as a default. They exist only so the expected rupee figures below can
be worked out by hand and checked. Real rates come from the authorities listed in the
module docstring of ``src/services/transaction_cost_service.py``.

No network, no DB, no config import: the service is a pure function of its arguments.
"""

from __future__ import annotations

import pytest

from src.services.transaction_cost_service import (
    CostSchedule,
    MissingRateError,
    TransactionCostService,
)

pytestmark = pytest.mark.unit


# --- Synthetic schedule (NOT real rates - see module docstring) ---------------------
# Chosen so that every component lands on an exactly representable, hand-checkable
# rupee figure at a turnover of 10,000.
BROKERAGE_RATE = 0.001  # 10 bps of turnover
BROKERAGE_CAP = 20.0  # INR per order
STT_MIS_BUY = 0.0  # deliberate sourced-zero stand-in
STT_MIS_SELL = 0.0002
STT_CNC_BUY = 0.0005
STT_CNC_SELL = 0.0005
EXCHANGE_RATE = 0.0001
SEBI_RATE = 0.000001
STAMP_DUTY_BUY = 0.00005
GST_RATE = 0.18


def synthetic_schedule(**overrides) -> CostSchedule:
    """A fully specified synthetic schedule; ``**overrides`` blanks or tweaks fields."""
    base = dict(
        brokerage_rate=BROKERAGE_RATE,
        brokerage_cap_inr=BROKERAGE_CAP,
        stt_mis_buy_rate=STT_MIS_BUY,
        stt_mis_sell_rate=STT_MIS_SELL,
        stt_cnc_buy_rate=STT_CNC_BUY,
        stt_cnc_sell_rate=STT_CNC_SELL,
        exchange_txn_rate=EXCHANGE_RATE,
        sebi_turnover_rate=SEBI_RATE,
        stamp_duty_buy_rate=STAMP_DUTY_BUY,
        gst_rate=GST_RATE,
    )
    base.update(overrides)
    return CostSchedule(**base)


# Per-rupee-of-turnover cost fractions implied by the synthetic schedule, while the
# brokerage cap is NOT binding. Derived by hand, used for the breakeven closed form.
#   buy  = brokerage + stt_buy + exchange + sebi + stamp + gst*(brokerage+exchange+sebi)
#   sell = brokerage + stt_sell + exchange + sebi + 0     + gst*(brokerage+exchange+sebi)
GST_ON_FEES = GST_RATE * (BROKERAGE_RATE + EXCHANGE_RATE + SEBI_RATE)  # 0.18 * 0.001101
MIS_BUY_FRACTION = BROKERAGE_RATE + STT_MIS_BUY + EXCHANGE_RATE + SEBI_RATE + STAMP_DUTY_BUY + GST_ON_FEES
MIS_SELL_FRACTION = BROKERAGE_RATE + STT_MIS_SELL + EXCHANGE_RATE + SEBI_RATE + GST_ON_FEES


# ---------------------------------------------------------------------------------
# 1. Component-level correctness: buy and sell, MIS and CNC
# ---------------------------------------------------------------------------------


def test_cnc_buy_components_match_hand_computation() -> None:
    # turnover = 100 * 100.00 = 10,000
    #   brokerage  = min(10,000 * 0.001, 20.00) = 10.00   (pct wins)
    #   stt        = 10,000 * 0.0005            =  5.00   (CNC buy)
    #   exchange   = 10,000 * 0.0001            =  1.00
    #   sebi       = 10,000 * 0.000001          =  0.01
    #   stamp duty = 10,000 * 0.00005           =  0.50   (buy side)
    #   gst        = 0.18 * (10.00+1.00+0.01)   =  1.9818
    #   total                                   = 18.4918
    cost = TransactionCostService.compute_order_cost("BUY", 100, 100.0, "CNC", synthetic_schedule())

    assert cost.turnover == pytest.approx(10_000.0)
    assert cost.brokerage == pytest.approx(10.0)
    assert cost.stt == pytest.approx(5.0)
    assert cost.exchange_charge == pytest.approx(1.0)
    assert cost.sebi_fee == pytest.approx(0.01)
    assert cost.stamp_duty == pytest.approx(0.5)
    assert cost.gst == pytest.approx(1.9818)
    assert cost.total == pytest.approx(18.4918)


def test_cnc_sell_components_match_hand_computation() -> None:
    # Same as the CNC buy leg except stamp duty is zero (sell side).
    #   total = 10.00 + 5.00 + 1.00 + 0.01 + 0.00 + 1.9818 = 17.9918
    cost = TransactionCostService.compute_order_cost("SELL", 100, 100.0, "CNC", synthetic_schedule())

    assert cost.brokerage == pytest.approx(10.0)
    assert cost.stt == pytest.approx(5.0)
    assert cost.stamp_duty == pytest.approx(0.0)
    assert cost.gst == pytest.approx(1.9818)
    assert cost.total == pytest.approx(17.9918)


def test_mis_buy_and_sell_components_match_hand_computation() -> None:
    schedule = synthetic_schedule()

    # MIS buy: STT buy rate is 0.0 under this synthetic schedule.
    #   total = 10.00 + 0.00 + 1.00 + 0.01 + 0.50 + 1.9818 = 13.4918
    buy = TransactionCostService.compute_order_cost("BUY", 100, 100.0, "MIS", schedule)
    assert buy.stt == pytest.approx(0.0)
    assert buy.stamp_duty == pytest.approx(0.5)
    assert buy.total == pytest.approx(13.4918)

    # MIS sell: stt = 10,000 * 0.0002 = 2.00, no stamp duty.
    #   total = 10.00 + 2.00 + 1.00 + 0.01 + 0.00 + 1.9818 = 14.9918
    sell = TransactionCostService.compute_order_cost("SELL", 100, 100.0, "MIS", schedule)
    assert sell.stt == pytest.approx(2.0)
    assert sell.stamp_duty == pytest.approx(0.0)
    assert sell.total == pytest.approx(14.9918)


def test_breakdown_to_dict_is_complete_and_itemised() -> None:
    cost = TransactionCostService.compute_order_cost("BUY", 100, 100.0, "CNC", synthetic_schedule())
    payload = cost.to_dict()

    assert set(payload) == {
        "side",
        "product_type",
        "quantity",
        "price",
        "turnover",
        "brokerage",
        "stt",
        "exchange_charge",
        "sebi_fee",
        "stamp_duty",
        "gst",
        "total",
        "cost_pct_of_turnover",
    }
    assert payload["side"] == "BUY"
    assert payload["product_type"] == "CNC"
    itemised = sum(
        payload[k] for k in ("brokerage", "stt", "exchange_charge", "sebi_fee", "stamp_duty", "gst")
    )
    assert itemised == pytest.approx(payload["total"])
    assert payload["cost_pct_of_turnover"] == pytest.approx(18.4918 / 10_000.0 * 100.0)


# ---------------------------------------------------------------------------------
# 2. STT: correct side(s), and MIS differs from CNC
# ---------------------------------------------------------------------------------


def test_stt_differs_between_mis_and_cnc_and_between_sides() -> None:
    schedule = synthetic_schedule()

    def stt(side: str, product: str) -> float:
        return TransactionCostService.compute_order_cost(side, 100, 100.0, product, schedule).stt

    # Product type changes the STT charged on the same side.
    assert stt("SELL", "MIS") == pytest.approx(2.0)
    assert stt("SELL", "CNC") == pytest.approx(5.0)
    assert stt("SELL", "MIS") != stt("SELL", "CNC")

    # Side changes the STT charged within the same product type.
    assert stt("BUY", "MIS") == pytest.approx(0.0)
    assert stt("BUY", "CNC") == pytest.approx(5.0)
    assert stt("BUY", "MIS") != stt("SELL", "MIS")


def test_stt_uses_only_the_rate_for_its_own_leg() -> None:
    # Poison the three legs we are not pricing; the MIS sell leg must be unaffected.
    schedule = synthetic_schedule(
        stt_mis_buy_rate=0.9,
        stt_cnc_buy_rate=0.9,
        stt_cnc_sell_rate=0.9,
    )
    cost = TransactionCostService.compute_order_cost("SELL", 100, 100.0, "MIS", schedule)
    assert cost.stt == pytest.approx(2.0)


# ---------------------------------------------------------------------------------
# 3. Stamp duty: buy side only
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize("product", ["MIS", "CNC"])
def test_stamp_duty_is_charged_on_buy_only(product: str) -> None:
    schedule = synthetic_schedule()
    buy = TransactionCostService.compute_order_cost("BUY", 100, 100.0, product, schedule)
    sell = TransactionCostService.compute_order_cost("SELL", 100, 100.0, product, schedule)

    assert buy.stamp_duty == pytest.approx(0.5)
    assert sell.stamp_duty == 0.0


def test_sell_leg_ignores_stamp_duty_rate_entirely() -> None:
    # A sell leg must not need, or use, the stamp duty rate at all.
    schedule = synthetic_schedule(stamp_duty_buy_rate=None)
    sell = TransactionCostService.compute_order_cost("SELL", 100, 100.0, "CNC", schedule)
    assert sell.stamp_duty == 0.0
    assert sell.total == pytest.approx(17.9918)


# ---------------------------------------------------------------------------------
# 4. GST base: brokerage + exchange + SEBI, never STT or stamp duty
# ---------------------------------------------------------------------------------


def test_gst_applies_to_fee_base_only() -> None:
    cost = TransactionCostService.compute_order_cost("BUY", 100, 100.0, "CNC", synthetic_schedule())

    expected_base = cost.brokerage + cost.exchange_charge + cost.sebi_fee
    assert cost.gst_base == pytest.approx(expected_base)
    assert cost.gst == pytest.approx(GST_RATE * expected_base)

    # Explicitly: STT (5.00) and stamp duty (0.50) are NOT in the GST base.
    assert cost.gst != pytest.approx(GST_RATE * (expected_base + cost.stt))
    assert cost.gst != pytest.approx(GST_RATE * (expected_base + cost.stamp_duty))


def test_gst_is_unchanged_when_only_stt_and_stamp_duty_change() -> None:
    baseline = TransactionCostService.compute_order_cost("BUY", 100, 100.0, "CNC", synthetic_schedule())
    inflated = TransactionCostService.compute_order_cost(
        "BUY",
        100,
        100.0,
        "CNC",
        synthetic_schedule(stt_cnc_buy_rate=0.01, stamp_duty_buy_rate=0.01),
    )

    assert inflated.gst == pytest.approx(baseline.gst)
    assert inflated.total > baseline.total  # the taxes themselves did rise


# ---------------------------------------------------------------------------------
# 5. Brokerage cap: min(percentage, flat cap)
# ---------------------------------------------------------------------------------


def test_brokerage_uses_percentage_when_below_cap() -> None:
    # turnover 10,000 -> pct brokerage 10.00 < cap 20.00
    cost = TransactionCostService.compute_order_cost("BUY", 100, 100.0, "CNC", synthetic_schedule())
    assert cost.brokerage == pytest.approx(10.0)


def test_brokerage_is_capped_on_large_turnover() -> None:
    # turnover 100,000 -> pct brokerage 100.00, capped to 20.00
    cost = TransactionCostService.compute_order_cost("BUY", 1_000, 100.0, "CNC", synthetic_schedule())
    assert cost.brokerage == pytest.approx(20.0)

    # GST then rides on the CAPPED brokerage, not the uncapped percentage.
    #   exchange = 10.00, sebi = 0.10 -> base 30.10 -> gst 5.418
    assert cost.exchange_charge == pytest.approx(10.0)
    assert cost.sebi_fee == pytest.approx(0.1)
    assert cost.gst == pytest.approx(GST_RATE * 30.10)


def test_brokerage_cap_exactly_at_the_boundary() -> None:
    # turnover 20,000 -> pct brokerage exactly 20.00 == cap
    cost = TransactionCostService.compute_order_cost("BUY", 200, 100.0, "CNC", synthetic_schedule())
    assert cost.brokerage == pytest.approx(20.0)


# ---------------------------------------------------------------------------------
# 6. THE IMPORTANT ONE: an unset rate must raise, never silently mean zero
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "side", "product"),
    [
        ("brokerage_rate", "BUY", "CNC"),
        ("brokerage_cap_inr", "BUY", "CNC"),
        ("exchange_txn_rate", "BUY", "CNC"),
        ("sebi_turnover_rate", "BUY", "CNC"),
        ("gst_rate", "BUY", "CNC"),
        ("stamp_duty_buy_rate", "BUY", "CNC"),
        ("stt_cnc_buy_rate", "BUY", "CNC"),
        ("stt_cnc_sell_rate", "SELL", "CNC"),
        ("stt_mis_buy_rate", "BUY", "MIS"),
        ("stt_mis_sell_rate", "SELL", "MIS"),
    ],
)
def test_unset_rate_raises_instead_of_being_treated_as_zero(field: str, side: str, product: str) -> None:
    schedule = synthetic_schedule(**{field: None})

    with pytest.raises(MissingRateError) as excinfo:
        TransactionCostService.compute_order_cost(side, 100, 100.0, product, schedule)

    err = excinfo.value
    assert err.missing == [field]
    message = str(err)
    assert field in message
    # The error must tell the operator where to source the number, and must say plainly
    # that no default exists on purpose.
    assert "source from" in message
    assert "fabricated rate" in message


def test_all_rates_default_to_none_so_an_empty_schedule_cannot_compute() -> None:
    empty = CostSchedule()
    assert all(value is None for value in empty.to_dict().values())

    with pytest.raises(MissingRateError) as excinfo:
        TransactionCostService.compute_order_cost("BUY", 100, 100.0, "CNC", empty)

    # Every required field for a CNC buy leg is reported at once, not one per round trip.
    assert set(excinfo.value.missing) == {
        "brokerage_rate",
        "brokerage_cap_inr",
        "exchange_txn_rate",
        "sebi_turnover_rate",
        "gst_rate",
        "stt_cnc_buy_rate",
        "stamp_duty_buy_rate",
    }


def test_missing_rate_error_is_a_valueerror_and_not_swallowed_by_helpers() -> None:
    schedule = synthetic_schedule(gst_rate=None)
    assert issubclass(MissingRateError, ValueError)

    with pytest.raises(MissingRateError):
        TransactionCostService.round_trip_cost(100, 100.0, 101.0, "MIS", schedule)
    with pytest.raises(MissingRateError):
        TransactionCostService.breakeven_move_pct(100, 100.0, "MIS", schedule)


def test_breakeven_validates_the_exit_leg_rate_too() -> None:
    # Only the SELL-leg STT is unset. A buy-only validation would miss this; the
    # breakeven solve must still refuse to run.
    schedule = synthetic_schedule(stt_mis_sell_rate=None)

    with pytest.raises(MissingRateError) as excinfo:
        TransactionCostService.breakeven_move_pct(100, 100.0, "MIS", schedule)
    assert excinfo.value.missing == ["stt_mis_sell_rate"]


def test_sourced_zero_is_honoured_and_is_distinct_from_unset() -> None:
    # 0.0 set deliberately by the operator computes fine; None does not.
    ok = TransactionCostService.compute_order_cost("BUY", 100, 100.0, "MIS", synthetic_schedule())
    assert ok.stt == 0.0

    with pytest.raises(MissingRateError):
        TransactionCostService.compute_order_cost(
            "BUY", 100, 100.0, "MIS", synthetic_schedule(stt_mis_buy_rate=None)
        )


# ---------------------------------------------------------------------------------
# 7. from_config
# ---------------------------------------------------------------------------------


class _FakeConfig:
    """Stand-in for the real config object; this test never imports src.config."""

    txn_cost_brokerage_rate = 0.001
    txn_cost_brokerage_cap_inr = "20.0"  # env vars arrive as strings
    txn_cost_gst_rate = 0.18
    txn_cost_exchange_txn_rate = ""  # blank env var == not sourced
    # every other txn_cost_* attribute is absent entirely


def test_from_config_reads_txn_cost_attributes_and_leaves_the_rest_none() -> None:
    schedule = CostSchedule.from_config(_FakeConfig())

    assert schedule.brokerage_rate == pytest.approx(0.001)
    assert schedule.brokerage_cap_inr == pytest.approx(20.0)  # string coerced
    assert schedule.gst_rate == pytest.approx(0.18)
    assert schedule.exchange_txn_rate is None  # blank -> unset, not 0.0
    assert schedule.stt_cnc_buy_rate is None  # absent attribute -> unset
    assert schedule.sebi_turnover_rate is None


def test_from_config_on_a_bare_object_yields_an_all_none_schedule() -> None:
    schedule = CostSchedule.from_config(object())
    assert all(value is None for value in schedule.to_dict().values())


def test_from_config_rejects_a_present_but_unparseable_rate() -> None:
    class BadConfig:
        txn_cost_gst_rate = "eighteen percent"

    with pytest.raises(ValueError, match="txn_cost_gst_rate"):
        CostSchedule.from_config(BadConfig())


# ---------------------------------------------------------------------------------
# 8. round_trip_cost
# ---------------------------------------------------------------------------------


def test_round_trip_cost_is_entry_plus_exit() -> None:
    trip = TransactionCostService.round_trip_cost(100, 100.0, 100.0, "MIS", synthetic_schedule())

    assert trip.entry.side == "BUY"
    assert trip.exit.side == "SELL"
    assert trip.entry.total == pytest.approx(13.4918)
    assert trip.exit.total == pytest.approx(14.9918)
    assert trip.total == pytest.approx(28.4836)
    assert trip.to_dict()["total"] == pytest.approx(28.4836)
    assert trip.cost_pct_of_entry_turnover == pytest.approx(28.4836 / 10_000.0 * 100.0)


def test_round_trip_cost_for_a_short_flips_the_legs() -> None:
    trip = TransactionCostService.round_trip_cost(
        100, 100.0, 100.0, "MIS", synthetic_schedule(), entry_side="SELL"
    )
    assert trip.entry.side == "SELL"
    assert trip.exit.side == "BUY"
    assert trip.total == pytest.approx(28.4836)  # same two legs, opposite order


# ---------------------------------------------------------------------------------
# 9. breakeven_move_pct against a closed form worked out by hand
# ---------------------------------------------------------------------------------


def test_breakeven_move_pct_matches_the_hand_derived_closed_form() -> None:
    # With the brokerage cap NOT binding, cost is linear in turnover, so:
    #   Q*E*x = buy_frac*Q*E + sell_frac*Q*E*(1+x)
    #   =>  x = (buy_frac + sell_frac) / (1 - sell_frac)
    #
    # buy_frac  = 0.001 + 0.0 + 0.0001 + 0.000001 + 0.00005 + 0.18*0.001101
    #           = 0.001151 + 0.00019818 = 0.00134918
    # sell_frac = 0.001 + 0.0002 + 0.0001 + 0.000001 + 0.18*0.001101
    #           = 0.001301 + 0.00019818 = 0.00149918
    #   x = 0.00284836 / 0.99850082 = 0.002852636...  ->  0.28526% price move
    assert MIS_BUY_FRACTION == pytest.approx(0.00134918)
    assert MIS_SELL_FRACTION == pytest.approx(0.00149918)

    closed_form_pct = (MIS_BUY_FRACTION + MIS_SELL_FRACTION) / (1.0 - MIS_SELL_FRACTION) * 100.0

    solved = TransactionCostService.breakeven_move_pct(100, 100.0, "MIS", synthetic_schedule())

    assert solved == pytest.approx(closed_form_pct, rel=1e-9)
    assert solved == pytest.approx(0.2852636, abs=1e-6)


def test_breakeven_move_actually_lands_the_trade_flat() -> None:
    schedule = synthetic_schedule()
    entry = 100.0
    qty = 100

    breakeven_pct = TransactionCostService.breakeven_move_pct(qty, entry, "MIS", schedule)
    exit_price = entry * (1.0 + breakeven_pct / 100.0)

    result = TransactionCostService.net_pnl(qty, entry, exit_price, "MIS", schedule)
    assert result["net_pnl"] == pytest.approx(0.0, abs=1e-9)

    # One paisa either side of it is a real loss / a real gain.
    assert TransactionCostService.net_pnl(qty, entry, exit_price - 0.01, "MIS", schedule)["net_pnl"] < 0
    assert TransactionCostService.net_pnl(qty, entry, exit_price + 0.01, "MIS", schedule)["net_pnl"] > 0


def test_breakeven_is_higher_for_cnc_than_mis_under_this_schedule() -> None:
    schedule = synthetic_schedule()
    mis = TransactionCostService.breakeven_move_pct(100, 100.0, "MIS", schedule)
    cnc = TransactionCostService.breakeven_move_pct(100, 100.0, "CNC", schedule)
    assert cnc > mis  # CNC carries STT on both legs under the synthetic schedule


def test_breakeven_is_independent_of_price_scale_while_the_cap_is_not_binding() -> None:
    schedule = synthetic_schedule()
    small = TransactionCostService.breakeven_move_pct(10, 50.0, "MIS", schedule)
    large = TransactionCostService.breakeven_move_pct(20, 25.0, "MIS", schedule)
    assert small == pytest.approx(large, rel=1e-12)


def test_breakeven_shrinks_once_the_brokerage_cap_binds() -> None:
    schedule = synthetic_schedule()
    uncapped = TransactionCostService.breakeven_move_pct(100, 100.0, "MIS", schedule)  # turnover 10k
    capped = TransactionCostService.breakeven_move_pct(10_000, 100.0, "MIS", schedule)  # turnover 1M
    assert capped < uncapped  # the flat cap dilutes as turnover grows


# ---------------------------------------------------------------------------------
# 10. Realistic ticket this system actually produced: PCJEWELLER.NS
# ---------------------------------------------------------------------------------


def test_pcjeweller_mis_ticket_breakeven_and_target_clearance() -> None:
    """PCJEWELLER.NS intraday ticket: entry 10.70, 233 shares, 2x MIS, +2.1% / -0.8%.

    Under the SYNTHETIC schedule defined in this file (not real rates), the round trip
    costs about 0.2853% of turnover, so:

      * the +2.1% target CLEARS breakeven comfortably (~7.4x the breakeven move) and
        still nets roughly +1.81% on notional;
      * the -0.8% stop is made WORSE by costs, losing roughly -1.09% on notional
        rather than the headline -0.8%.

    Note on the 2x MIS leverage: the levies are charged on the full notional turnover,
    not on the margin posted, so leverage does not move the breakeven *price* level at
    all. It scales the return on deployed capital on both sides of that level, which is
    why the cost drag on capital is 2x the drag on notional shown here.
    """
    schedule = synthetic_schedule()
    entry = 10.70
    qty = 233
    notional = qty * entry  # 2,493.10 - well under the 20.00 brokerage cap at 10 bps

    breakeven_pct = TransactionCostService.breakeven_move_pct(qty, entry, "MIS", schedule)

    # Breakeven is computed, finite and positive.
    assert breakeven_pct > 0.0
    assert breakeven_pct == pytest.approx(0.2852636, abs=1e-6)

    # Documented verdict: the +2.1% target clears breakeven under these synthetic rates.
    target_pct = 2.1
    stop_pct = -0.8
    assert target_pct > breakeven_pct
    assert target_pct / breakeven_pct > 7.0

    target_price = entry * (1.0 + target_pct / 100.0)
    at_target = TransactionCostService.net_pnl(qty, entry, target_price, "MIS", schedule)
    assert at_target["gross_pct"] == pytest.approx(2.1, abs=1e-9)
    assert at_target["net_pnl"] > 0.0
    # Exact, by hand: net% = 2.1 - buy_frac*100 - sell_frac*100*1.021
    #               = 2.1 - 0.134918 - 0.153066278 = 1.812015722
    exact_net_pct = target_pct - MIS_BUY_FRACTION * 100.0 - MIS_SELL_FRACTION * 100.0 * (1.0 + target_pct / 100.0)
    assert exact_net_pct == pytest.approx(1.812015722, abs=1e-9)
    assert at_target["net_pct"] == pytest.approx(exact_net_pct, rel=1e-12)
    # ...which is target minus breakeven, up to the exit leg's costs being charged on
    # the (larger) target turnover rather than on the entry turnover.
    assert at_target["net_pct"] == pytest.approx(target_pct - breakeven_pct, abs=5e-3)
    assert at_target["net_pct"] == pytest.approx(1.81, abs=0.01)

    # The stop is worse than its headline number once costs are paid.
    stop_price = entry * (1.0 + stop_pct / 100.0)
    at_stop = TransactionCostService.net_pnl(qty, entry, stop_price, "MIS", schedule)
    assert at_stop["net_pnl"] < 0.0
    assert at_stop["net_pct"] < stop_pct
    assert at_stop["net_pct"] == pytest.approx(-1.09, abs=0.01)

    # Costs are charged on the full notional, not on the 2x-leveraged margin.
    trip = TransactionCostService.round_trip_cost(qty, entry, target_price, "MIS", schedule)
    assert trip.entry.turnover == pytest.approx(notional)
    assert trip.entry.brokerage < 20.0  # cap not binding on a ~2.5k ticket

    # Realised R:R degrades once costs are applied - the point of having this model.
    gross_rr = target_pct / abs(stop_pct)
    net_rr = at_target["net_pct"] / abs(at_stop["net_pct"])
    assert net_rr < gross_rr


# ---------------------------------------------------------------------------------
# 11. Input validation
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"side": "LONG", "quantity": 1, "price": 10.0, "product_type": "MIS"},
        {"side": "BUY", "quantity": 1, "price": 10.0, "product_type": "NRML"},
        {"side": "BUY", "quantity": 0, "price": 10.0, "product_type": "MIS"},
        {"side": "BUY", "quantity": -5, "price": 10.0, "product_type": "MIS"},
        {"side": "BUY", "quantity": 1, "price": 0.0, "product_type": "MIS"},
        {"side": "BUY", "quantity": 1, "price": -10.0, "product_type": "MIS"},
    ],
)
def test_invalid_inputs_raise_valueerror(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        TransactionCostService.compute_order_cost(schedule=synthetic_schedule(), **kwargs)


def test_side_and_product_are_case_and_whitespace_tolerant() -> None:
    cost = TransactionCostService.compute_order_cost(" buy ", 100, 100.0, " cnc ", synthetic_schedule())
    assert cost.side == "BUY"
    assert cost.product_type == "CNC"
    assert cost.total == pytest.approx(18.4918)
