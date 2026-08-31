# -*- coding: utf-8 -*-
"""Minimum viable holding horizon per instrument.

Why this exists
---------------
A horizon is not a preference; it is a constraint set by three measured quantities:

  reachable(H) = ADR x CAPTURE_FRACTION x H     what the horizon can deliver
  cost         = statutory + spread             what a round trip costs
  stop         >= NOISE_STOP_FRACTION x ADR     a stop normal noise will not trigger

A horizon is viable for an instrument only when a target satisfying the required
net reward-to-risk still fits inside the reachable move.

Measured 2026-08-31 across the live watchlist: with a noise-safe stop and net R:R
2.0 after costs, **0 of 13 instruments support an intraday trade**. The minimum
viable horizon is 5 days, and 10 for most sub-Rs-20 names.

That is the mechanism behind the resolution result: of 21 signals checked against
real 5-minute bars, 15 touched neither stop nor target and none reached its target.
They were not wrong calls - their targets were placed beyond what the horizon could
deliver, so they could never resolve, and a signal that cannot resolve contributes
nothing to the evidence base however long the system runs.

See the vault note "Horizon Selection Standard" for the full derivation.
"""

from __future__ import annotations

from typing import Optional

# 实测：|open-close| / (high-low)，674 根日线，.NS 标的，2026-06-01 起。
CAPTURE_FRACTION = 0.42

# 目标留出余量：取 0.6 意味着不需要"完美的一天"才能到达。
TARGET_HEADROOM = 0.6

# 止损下限。低于此值时，正常日内噪声就会把仓位扫掉 —— 那是在收割噪声，不是风控。
NOISE_STOP_FRACTION = 0.5

# 净回报风险比要求。实测方向性调用的基准命中率是 46.3%，
# R:R 低于约 1.2 时，低于五成的胜率必然亏钱。
REQUIRED_NET_REWARD_RISK = 2.0

# 候选 horizon，由短到长。
CANDIDATE_HORIZONS = ("1d", "3d", "5d", "10d")

HORIZON_DAYS = {"intraday": 1, "1d": 1, "3d": 3, "5d": 5, "10d": 10}

# NSE tick size bands (2025-04-15 revision). Below Rs 250 the tick is one paisa,
# which on a Rs 1.25 stock is 0.8% of price - the dominant cost, not brokerage.
_TICK_BANDS = ((250.0, 0.01), (1000.0, 0.05))
_TICK_ABOVE = 0.10

# Statutory round trip on an intraday equity position, as a percentage of turnover.
# Sourced rates: exchange 0.0000307 (NSE/FA/73061), SEBI 0.000001, STT sell
# 0.00025, stamp buy 0.00003, GST 18% on the fee base, brokerage 0.03% capped.
STATUTORY_ROUND_TRIP_PCT = 0.106


def tick_size(price: float) -> float:
    """NSE tick for a price, in rupees."""
    for ceiling, tick in _TICK_BANDS:
        if price < ceiling:
            return tick
    return _TICK_ABOVE


def round_trip_cost_pct(price: float) -> float:
    """Statutory cost plus two tick crossings, as a percentage of position value.

    The tick term is a **floor**, not the real spread: on thin names the actual
    bid-ask gap is wider. Every figure derived from this is therefore optimistic.
    """
    if not price or price <= 0:
        return STATUTORY_ROUND_TRIP_PCT
    return STATUTORY_ROUND_TRIP_PCT + (tick_size(price) / price * 100.0) * 2.0


def reachable_move_pct(average_daily_range_pct: float, horizon_days: int) -> float:
    """How far the price can realistically travel over the horizon."""
    return average_daily_range_pct * CAPTURE_FRACTION * horizon_days


def noise_safe_stop_pct(average_daily_range_pct: float, price: float) -> float:
    """Smallest stop that normal movement will not trigger by itself."""
    return max(round_trip_cost_pct(price), NOISE_STOP_FRACTION * average_daily_range_pct)


def required_target_pct(average_daily_range_pct: float, price: float) -> float:
    """Target distance needed for the required net reward-to-risk after costs."""
    cost = round_trip_cost_pct(price)
    stop = noise_safe_stop_pct(average_daily_range_pct, price)
    return REQUIRED_NET_REWARD_RISK * (stop + cost) + cost


def minimum_viable_horizon(
    average_daily_range_pct: Optional[float],
    price: Optional[float],
) -> Optional[str]:
    """Shortest horizon that can carry a trade on this instrument.

    Returns None when no horizon up to 10 days works - the instrument is then out
    of universe, and lowering the required reward-to-risk to admit it would be
    fitting the plan to the wish.
    """
    if not average_daily_range_pct or average_daily_range_pct <= 0:
        return None
    if not price or price <= 0:
        return None

    needed = required_target_pct(average_daily_range_pct, price)
    for horizon in CANDIDATE_HORIZONS:
        days = HORIZON_DAYS[horizon]
        if needed <= TARGET_HEADROOM * reachable_move_pct(average_daily_range_pct, days):
            return horizon
    return None


def reachable_target_pct(
    average_daily_range_pct: Optional[float],
    horizon: Optional[str],
) -> Optional[float]:
    """Largest target distance this horizon can be expected to deliver."""
    if not average_daily_range_pct or average_daily_range_pct <= 0:
        return None
    days = HORIZON_DAYS.get((horizon or "").strip().lower())
    if not days:
        return None
    return TARGET_HEADROOM * reachable_move_pct(average_daily_range_pct, days)
