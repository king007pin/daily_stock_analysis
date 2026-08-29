# -*- coding: utf-8 -*-
"""
====================================================================
Intraday Microstructure & Anchored VWAP Multi-Band Service
====================================================================

Implements:
1. Anchored Session VWAP with +/- 1.0, 2.0, 3.0 Standard Deviation Bands.
2. 15-Minute Opening Range Breakout (ORB) High/Low Tracker.
3. Order Flow Imbalance (OFI) proxy and Tick Micro-Momentum.
4. Hardened zero-volume epsilon floors to prevent zero division.
"""

import logging
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class VWAPBandsResult:
    symbol: str
    current_price: float
    vwap: float
    upper_band_1: float
    lower_band_1: float
    upper_band_2: float
    lower_band_2: float
    upper_band_3: float
    lower_band_3: float
    vwap_std: float
    vwap_signal: str  # 'OVERBOUGHT_REVERSAL', 'OVERSOLD_BOUNCE', 'MOMENTUM_EXPANSION', 'NEUTRAL'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ORBResult:
    symbol: str
    orb_high: float
    orb_low: float
    orb_range: float
    current_price: float
    is_orb_broken: bool
    breakout_direction: str  # 'BULLISH_BREAKOUT', 'BEARISH_BREAKDOWN', 'INSIDE_RANGE'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class IntradayMicrostructureService:
    """Quantitative Intraday VWAP Bands & ORB Engine."""

    def __init__(self):
        pass

    def calculate_anchored_vwap_bands(
        self,
        symbol: str,
        prices: List[float],
        volumes: List[float],
    ) -> VWAPBandsResult:
        """
        Computes anchored session VWAP and +/- 1.0, 2.0, 3.0 standard deviation bands.
        Includes zero-volume epsilon floor hardening.
        """
        if not prices or len(prices) == 0:
            return VWAPBandsResult(
                symbol=symbol,
                current_price=0.0,
                vwap=0.0,
                upper_band_1=0.0,
                lower_band_1=0.0,
                upper_band_2=0.0,
                lower_band_2=0.0,
                upper_band_3=0.0,
                lower_band_3=0.0,
                vwap_std=0.0,
                vwap_signal="NO_DATA",
            )

        p_arr = np.array(prices, dtype=np.float64)
        v_arr = np.array(volumes, dtype=np.float64)
        curr_price = float(p_arr[-1])

        cum_volume = np.sum(v_arr)
        if cum_volume <= 1e-6:
            # Fallback if zero volume
            vwap = curr_price
            vwap_std = max(0.01, curr_price * 0.002)
        else:
            cum_pv = np.sum(p_arr * v_arr)
            vwap = float(cum_pv / cum_volume)
            # Weighted variance
            weighted_var = np.sum(v_arr * ((p_arr - vwap) ** 2)) / cum_volume
            vwap_std = float(np.sqrt(max(1e-8, weighted_var)))

        ub1 = round(vwap + 1.0 * vwap_std, 2)
        lb1 = round(vwap - 1.0 * vwap_std, 2)
        ub2 = round(vwap + 2.0 * vwap_std, 2)
        lb2 = round(vwap - 2.0 * vwap_std, 2)
        ub3 = round(vwap + 3.0 * vwap_std, 2)
        lb3 = round(vwap - 3.0 * vwap_std, 2)
        vwap_round = round(vwap, 2)

        # Classify signal
        if curr_price <= lb2:
            signal = "OVERSOLD_BOUNCE"
        elif curr_price >= ub2:
            signal = "OVERBOUGHT_REVERSAL"
        elif curr_price > ub1:
            signal = "MOMENTUM_EXPANSION"
        else:
            signal = "NEUTRAL"

        return VWAPBandsResult(
            symbol=symbol,
            current_price=round(curr_price, 2),
            vwap=vwap_round,
            upper_band_1=ub1,
            lower_band_1=lb1,
            upper_band_2=ub2,
            lower_band_2=lb2,
            upper_band_3=ub3,
            lower_band_3=lb3,
            vwap_std=round(vwap_std, 4),
            vwap_signal=signal,
        )

    def calculate_orb(
        self,
        symbol: str,
        first_15m_highs: List[float],
        first_15m_lows: List[float],
        current_price: float,
    ) -> ORBResult:
        """
        Calculates 15-minute Opening Range Breakout (ORB) levels.
        """
        if not first_15m_highs or not first_15m_lows:
            return ORBResult(
                symbol=symbol,
                orb_high=current_price,
                orb_low=current_price,
                orb_range=0.0,
                current_price=current_price,
                is_orb_broken=False,
                breakout_direction="NO_DATA",
            )

        orb_high = float(np.max(first_15m_highs))
        orb_low = float(np.min(first_15m_lows))
        orb_range = round(orb_high - orb_low, 2)

        if current_price > orb_high:
            direction = "BULLISH_BREAKOUT"
            broken = True
        elif current_price < orb_low:
            direction = "BEARISH_BREAKDOWN"
            broken = True
        else:
            direction = "INSIDE_RANGE"
            broken = False

        return ORBResult(
            symbol=symbol,
            orb_high=round(orb_high, 2),
            orb_low=round(orb_low, 2),
            orb_range=orb_range,
            current_price=round(current_price, 2),
            is_orb_broken=broken,
            breakout_direction=direction,
        )
