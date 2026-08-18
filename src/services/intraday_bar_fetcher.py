# -*- coding: utf-8 -*-
"""
Real intraday-bar sourcing for the live intraday scanner.

Zero-Hallucination Invariant (AGENTS.md §1.3): the scanner previously
synthesized prices_1m/volumes_1m/RSI/volume_surge_ratio from the live price
alone. This module replaces that with real data. Two data tiers:

1. Intraday 5m OHLCV — sourced directly from yfinance. This is NOT wrapped by
   data_provider's fetcher-failover chain (no fetcher in data_provider/
   supports intraday intervals today — daily bars only). If this fetch
   fails, callers must skip the symbol rather than fall back to a synthetic
   value — that failure mode is exactly what created the original bug.
2. Daily closes/volume — sourced via DataFetcherManager.get_daily_data(),
   reusing the repo's existing multi-source failover (Yahoo -> Jugaad -> ...)
   already hardened for the India market.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Set

import pandas as pd

from data_provider.base import DataFetcherManager
from src.services.alert_indicators import _calculate_rsi

logger = logging.getLogger(__name__)

_TRADING_MINUTES_PER_DAY = 375  # NSE 09:15-15:30 IST
_FNO_LIST_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static_data", "nse_fno_eligible.json")
_fno_symbols_cache: Optional[Set[str]] = None


def is_fno_eligible(symbol: str) -> bool:
    """Look up a symbol against the partial F&O seed list (data/nse_fno_eligible.json).

    Defaults False for anything not in the list - the safe direction, since
    wrongly granting 5x leverage sizing on a cash-only stock is the dangerous
    failure mode. See the file's own _meta.completeness note: this list is
    NOT exhaustive (~25 of ~208 real F&O names).
    """
    global _fno_symbols_cache
    if _fno_symbols_cache is None:
        try:
            with open(_FNO_LIST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _fno_symbols_cache = {s.upper() for s in data.get("symbols", [])}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[IntradayBars] failed to load F&O eligibility list: %s", exc)
            _fno_symbols_cache = set()

    clean = symbol.replace(".NS", "").replace(".BO", "").upper()
    return clean in _fno_symbols_cache


@dataclass
class IntradayBars:
    symbol: str
    prices_1m: List[float]
    volumes_1m: List[float]
    orb_highs: List[float]
    orb_lows: List[float]
    cumulative_volume: float
    source: str


def fetch_intraday_bars(symbol: str, interval: str = "5m") -> Optional[IntradayBars]:
    """Fetch today's real intraday bars for `symbol` (e.g. 'IDEA.NS').

    Returns None on any failure — callers must skip the symbol, not
    substitute a synthetic series.
    """
    try:
        import yfinance as yf

        df = yf.Ticker(symbol).history(period="1d", interval=interval)
    except Exception as exc:  # noqa: BLE001 - any failure means "no real data"
        logger.warning("[IntradayBars] fetch failed for %s: %s", symbol, exc)
        return None

    if df is None or df.empty or len(df) < 3:
        logger.warning("[IntradayBars] insufficient bars for %s (got %s)", symbol, 0 if df is None else len(df))
        return None

    closes = df["Close"].astype(float).tolist()
    volumes = df["Volume"].astype(float).tolist()

    # Opening range = first 15 minutes (3 x 5m bars from market open).
    orb_window = df.iloc[: min(3, len(df))]
    orb_highs = orb_window["High"].astype(float).tolist()
    orb_lows = orb_window["Low"].astype(float).tolist()

    return IntradayBars(
        symbol=symbol,
        prices_1m=closes,
        volumes_1m=volumes,
        orb_highs=orb_highs,
        orb_lows=orb_lows,
        cumulative_volume=float(sum(volumes)),
        source=f"yfinance_{interval}",
    )


def compute_rsi(symbol: str, fetcher_manager: Optional[DataFetcherManager] = None, period: int = 14) -> Optional[float]:
    """Real RSI(period) off daily closes, via the repo's existing failover chain."""
    manager = fetcher_manager or DataFetcherManager()
    try:
        df, source = manager.get_daily_data(symbol, days=period + 10)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[IntradayBars] RSI daily-data fetch failed for %s: %s", symbol, exc)
        return None

    if df is None or df.empty or len(df) < period + 1:
        logger.warning("[IntradayBars] insufficient daily history for RSI on %s (got %s rows)", symbol, 0 if df is None else len(df))
        return None

    rsi_series = _calculate_rsi(df["close"], period)
    value = float(rsi_series.iloc[-1])
    logger.debug("[IntradayBars] RSI(%s) for %s = %.2f via %s", period, symbol, value, source)
    return value


def compute_volume_surge_ratio(
    symbol: str,
    cumulative_volume_today: float,
    minutes_elapsed_since_open: float,
    fetcher_manager: Optional[DataFetcherManager] = None,
    lookback_days: int = 10,
) -> Optional[float]:
    """Today's volume-so-far vs. an expected baseline for the same elapsed time.

    Baseline = (N-day average daily volume) * (minutes elapsed / full session
    minutes). This assumes roughly uniform intraday volume distribution — an
    approximation, not a true per-5-minute historical baseline (that would
    need historical intraday bars, out of scope for this fix). Documented
    here rather than presented as more precise than it is.
    """
    manager = fetcher_manager or DataFetcherManager()
    try:
        df, _source = manager.get_daily_data(symbol, days=lookback_days + 5)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[IntradayBars] volume-surge daily-data fetch failed for %s: %s", symbol, exc)
        return None

    if df is None or df.empty:
        return None

    recent = df.tail(lookback_days)
    avg_daily_volume = float(recent["volume"].mean())
    if avg_daily_volume <= 0:
        return None

    elapsed_fraction = max(0.01, min(1.0, minutes_elapsed_since_open / _TRADING_MINUTES_PER_DAY))
    baseline = avg_daily_volume * elapsed_fraction
    if baseline <= 0:
        return None

    return cumulative_volume_today / baseline
