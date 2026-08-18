# -*- coding: utf-8 -*-
"""
Real EOD-level market data for the EOD pipeline bridge.

Replaces eod_pipeline_service.py's previous hardcoded Nifty/Sensex closes,
hardcoded "+0.42%"/"Risk-On" template text, and hardcoded top_gainers list.
Zero-Hallucination Invariant (AGENTS.md Sec 1.3): every value here is real
or the caller gets None/empty and must degrade the report honestly, never
substitute a plausible-looking number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from data_provider.base import DataFetcherManager

logger = logging.getLogger(__name__)

_INDEX_TICKERS = {"nifty": "^NSEI", "sensex": "^BSESN"}


@dataclass
class IndexClose:
    close: float
    change_pct: float
    regime: str  # "Risk-On" / "Risk-Off" / "Flat" - derived from this index's own daily sign only


def fetch_index_close(index: str) -> Optional[IndexClose]:
    """Real close + daily % change for 'nifty' or 'sensex'.

    Direct yfinance call, same as the intraday scanner's approach - NOT
    wrapped by data_provider's fetcher-failover chain (no fetcher there
    covers index tickers). Single point of failure, documented not hidden.
    """
    ticker = _INDEX_TICKERS.get(index)
    if ticker is None:
        raise ValueError(f"unknown index '{index}', expected one of {list(_INDEX_TICKERS)}")

    try:
        import yfinance as yf

        df = yf.Ticker(ticker).history(period="5d")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[EODMarketData] index fetch failed for %s: %s", ticker, exc)
        return None

    if df is None or len(df) < 2:
        logger.warning("[EODMarketData] insufficient history for %s (got %s rows)", ticker, 0 if df is None else len(df))
        return None

    close_today = float(df["Close"].iloc[-1])
    close_prev = float(df["Close"].iloc[-2])
    if close_prev == 0:
        return None

    change_pct = ((close_today - close_prev) / close_prev) * 100.0
    if change_pct > 0.05:
        regime = "Risk-On"
    elif change_pct < -0.05:
        regime = "Risk-Off"
    else:
        regime = "Flat"

    return IndexClose(close=close_today, change_pct=round(change_pct, 2), regime=regime)


def compute_watchlist_movers(
    symbols: List[str],
    fetcher_manager: Optional[DataFetcherManager] = None,
    top_n: int = 5,
) -> Dict[str, List[Dict[str, str]]]:
    """Real daily % change for each watchlist symbol, ranked.

    This is watchlist-scoped, NOT NSE-market-wide top gainers/losers - that
    distinction matters, see the vault remediation note. Symbols with no
    fetchable daily history are silently excluded from ranking (not padded
    with a fabricated value), which is the correct behavior for a ranking
    that must stay real.
    """
    manager = fetcher_manager or DataFetcherManager()
    changes: List[Dict[str, float]] = []

    for symbol in symbols:
        try:
            df, _source = manager.get_daily_data(symbol, days=5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[EODMarketData] mover fetch failed for %s: %s", symbol, exc)
            continue

        if df is None or len(df) < 2:
            continue

        close_today = float(df["close"].iloc[-1])
        close_prev = float(df["close"].iloc[-2])
        if close_prev == 0:
            continue

        change_pct = ((close_today - close_prev) / close_prev) * 100.0
        changes.append({"symbol": symbol, "change_pct": round(change_pct, 2)})

    changes.sort(key=lambda x: x["change_pct"], reverse=True)
    gainers = [c for c in changes if c["change_pct"] > 0][:top_n]
    losers = [c for c in changes if c["change_pct"] < 0][-top_n:][::-1]

    return {
        "watchlist_top_gainers": [{"symbol": c["symbol"], "change": f"{c['change_pct']:+.2f}%"} for c in gainers],
        "watchlist_top_losers": [{"symbol": c["symbol"], "change": f"{c['change_pct']:+.2f}%"} for c in losers],
    }


def compute_btst_summary(market: str = "in", horizon: str = "1d") -> Dict[str, object]:
    """Real BTST-equivalent tracked-signal stats, DB-backed.

    There is no literal "BTST" horizon in this codebase's schema
    (HORIZONS = intraday/1d/3d/5d/10d/swing/long, decision_signal_service.py).
    BTST (Buy Today Sell Tomorrow - entered midday, exited next session) maps
    to horizon="1d", matching market_scheduler_service.py's own MIDDAY_BTST
    session-phase naming. Replaces the previous hardcoded "Active Basket: ...
    Exit target gate at +10%" prose, which was static regardless of any real
    signal or outcome.
    """
    try:
        from src.services.decision_signal_outcome_service import DecisionSignalOutcomeService

        service = DecisionSignalOutcomeService()
        stats = service.get_stats(horizons=[horizon])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[EODMarketData] BTST stats fetch failed: %s", exc)
        return {"status": "UNAVAILABLE", "reason": str(exc), "horizon": horizon, "market": market}

    market_rows = stats.get("breakdowns", {}).get("market", [])
    row = next((r for r in market_rows if r.get("value") == market), None)

    if row is None or not row.get("total"):
        return {"status": "NO_SIGNALS_YET", "horizon": horizon, "market": market}

    return {
        "status": "TRACKED",
        "horizon": horizon,
        "market": market,
        "total": row.get("total"),
        "completed": row.get("completed"),
        "hit": row.get("hit"),
        "miss": row.get("miss"),
        "hit_rate_pct": row.get("hit_rate_pct"),
        "avg_stock_return_pct": row.get("avg_stock_return_pct"),
    }
