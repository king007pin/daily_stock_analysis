# -*- coding: utf-8 -*-
"""
====================================================================
Institutional Macroeconomic & Liquidity Radar Service
====================================================================

Implements:
1. FII / DII Daily Institutional Flow & Index Futures Long/Short Ratio.
2. Intermarket Rolling Pearson Correlation Matrix (Nifty 50 vs Brent, USDINR, DXY, US 10Y).
3. Institutional Sentiment Signal Generator.
"""

import logging
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional
from datetime import datetime
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class InstitutionalFlowResult:
    date_str: str
    fii_net_cash_cr: float
    dii_net_cash_cr: float
    net_institutional_cr: float
    fii_index_futures_long_pct: float
    institutional_bias: str  # 'BULLISH', 'BEARISH', 'NEUTRAL'
    is_historical_cache: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IntermarketCorrelationResult:
    benchmark: str
    rolling_window_days: int
    correlations: Dict[str, float]
    macro_regime: str  # 'RISK_ON', 'RISK_OFF', 'STAGFLATION_SHOCK'

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MacroRadarService:
    """Institutional Liquidity & Intermarket Macro Analysis Engine."""

    def __init__(self):
        pass

    def evaluate_institutional_flows(
        self,
        fii_cash_cr: float = 0.0,
        dii_cash_cr: float = 0.0,
        fii_long_contracts: int = 150000,
        fii_short_contracts: int = 100000,
        is_cache: bool = False,
    ) -> InstitutionalFlowResult:
        """Computes institutional net cash and Index Futures Long/Short ratio."""
        net_inst = fii_cash_cr + dii_cash_cr
        total_contracts = max(1, fii_long_contracts + fii_short_contracts)
        long_pct = round((fii_long_contracts / total_contracts) * 100.0, 1)

        if long_pct >= 65.0 or (net_inst > 1500.0 and long_pct >= 50.0):
            bias = "BULLISH"
        elif long_pct <= 35.0 or (net_inst < -2000.0 and long_pct <= 45.0):
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        return InstitutionalFlowResult(
            date_str=datetime.now().strftime("%Y-%m-%d"),
            fii_net_cash_cr=round(fii_cash_cr, 2),
            dii_net_cash_cr=round(dii_cash_cr, 2),
            net_institutional_cr=round(net_inst, 2),
            fii_index_futures_long_pct=long_pct,
            institutional_bias=bias,
            is_historical_cache=is_cache,
        )

    def calculate_intermarket_correlations(
        self,
        benchmark_returns: pd.Series,
        macro_asset_returns: Dict[str, pd.Series],
        rolling_window: int = 30,
    ) -> IntermarketCorrelationResult:
        """
        Computes Pearson correlation between benchmark (e.g. Nifty 50)
        and global macro assets (Brent Crude, USDINR, US 10Y Yield, Gold).
        """
        corrs = {}
        for asset_name, series in macro_asset_returns.items():
            df_aligned = pd.DataFrame({"bench": benchmark_returns, "asset": series}).dropna()
            if len(df_aligned) >= 10:
                c = float(df_aligned["bench"].corr(df_aligned["asset"]))
                corrs[asset_name] = round(c, 3)
            else:
                corrs[asset_name] = 0.0

        # Regime classification
        usd_corr = corrs.get("USDINR", -0.3)
        crude_corr = corrs.get("BRENT_CRUDE", -0.2)

        if usd_corr < -0.6 and crude_corr < -0.5:
            regime = "RISK_OFF"
        elif usd_corr >= -0.2 and crude_corr >= -0.1:
            regime = "RISK_ON"
        else:
            regime = "NEUTRAL"

        return IntermarketCorrelationResult(
            benchmark="NIFTY_50",
            rolling_window_days=rolling_window,
            correlations=corrs,
            macro_regime=regime,
        )
