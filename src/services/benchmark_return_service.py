# -*- coding: utf-8 -*-
"""
Benchmark-relative (excess) return measurement.

Why this exists
---------------
The rest of the system scores a signal by its **absolute** return
(``BacktestEngine.evaluate_decision_signal`` -> ``stock_return_pct``). Absolute
return alone cannot answer the only question that matters for skill: did the
call beat the market it was taken in? +15% inside a +20% index is
underperformance, and today nothing in this repo can say so.

This module computes the benchmark leg of that comparison and the excess
(signal - benchmark) difference. It is read-only and self-contained: it does
not touch the scoring pipeline, the outcome service or the schema.

Conventions are deliberately matched to the signal side
-------------------------------------------------------
* Multi-day horizons: the signal is scored close-to-close (anchor day close ->
  close of the N-th forward bar, see ``BacktestEngine.evaluate_decision_signal``).
  ``benchmark_return_pct`` uses the identical convention on the index.
* ``intraday``: ``decision_signal_outcome_service`` scores intraday signals as a
  same-day **open -> close** proxy (entry = anchor day's open, exit = that same
  bar's close). ``intraday_benchmark_return_pct`` therefore uses the index's
  own same-day open -> close move. Comparing an open->close signal against a
  close->close index would be an invalid comparison.

Zero-Hallucination Invariant (AGENTS.md Sec 1.3)
------------------------------------------------
If index data is unavailable the benchmark fields come back ``None`` together
with an explicit ``reason``. A missing benchmark is NEVER coerced to ``0.0``:
that would silently convert "unknown" into the factual claim "the market was
flat", i.e. a fabricated market data point.

Reused, not reinvented
----------------------
* Index symbols come from the existing market metadata:
  ``src/core/market_profile.py`` (``mood_index_code``) plus
  ``data_provider/us_index_mapping.py`` for the US ticker form. ``^NSEI`` /
  ``^BSESN`` are the same tickers already used by
  ``src/services/eod_market_data.py``.
* Bars are fetched through the existing failover entrypoint
  ``DataFetcherManager.get_daily_data(stock_code, start_date, end_date, days)
  -> (DataFrame, source_name)`` (``data_provider/base.py``), the same call used
  by ``eod_market_data.compute_watchlist_movers`` and
  ``intraday_bar_fetcher``. No new fetching mechanism is introduced.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from data_provider.us_index_mapping import get_us_index_yf_symbol
from src.core.market_profile import get_profile

logger = logging.getLogger(__name__)


# Return conventions, mirroring how the signal side scores each horizon.
CONVENTION_CLOSE_TO_CLOSE = "close_to_close"
CONVENTION_INTRADAY_OPEN_TO_CLOSE = "intraday_open_to_close"

# Explicit reasons. Every one of them means "benchmark unknown", never "flat".
REASON_NO_BENCHMARK = "no_benchmark_configured"
REASON_INVALID_START_DATE = "invalid_start_date"
REASON_FETCH_FAILED = "benchmark_data_unavailable"
REASON_EMPTY_DATA = "benchmark_data_empty"
REASON_ANCHOR_BAR_MISSING = "benchmark_anchor_bar_missing"
REASON_INSUFFICIENT_BARS = "insufficient_benchmark_bars"
REASON_INVALID_ANCHOR_PRICE = "invalid_benchmark_anchor_price"
REASON_MISSING_END_CLOSE = "missing_benchmark_end_close"
REASON_MISSING_SIGNAL_RETURN = "missing_signal_return"


@dataclass(frozen=True)
class BenchmarkSpec:
    """The index a market's signals are measured against."""

    market: str
    symbol: str
    name: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _in_benchmark() -> BenchmarkSpec:
    # IN_PROFILE.mood_index_code == "^NSEI"; identical to
    # eod_market_data._INDEX_TICKERS["nifty"].
    return BenchmarkSpec(market="in", symbol=get_profile("in").mood_index_code, name="Nifty 50")


def _us_benchmark() -> BenchmarkSpec:
    # US_PROFILE.mood_index_code == "SPX" -> Yahoo symbol "^GSPC" via the
    # existing US index mapping (the same resolution DataFetcherManager applies
    # when routing US index codes to YfinanceFetcher).
    symbol, _cn_name = get_us_index_yf_symbol(get_profile("us").mood_index_code)
    return BenchmarkSpec(market="us", symbol=symbol, name="S&P 500")


def _cn_benchmark() -> BenchmarkSpec:
    # CN_PROFILE.mood_index_code == "000001" (上证指数). The bare 6-digit form is
    # ambiguous with the stock 000001 (平安银行), so the repo's index code form
    # "sh000001" is used - the same key YfinanceFetcher.get_main_indices maps to
    # "000001.SS" and AkshareFetcher labels 上证指数.
    return BenchmarkSpec(market="cn", symbol="sh" + get_profile("cn").mood_index_code, name="上证指数")


# Markets with a benchmark wired up. Any other market (hk / jp / kr / tw /
# unknown) intentionally resolves to no benchmark and yields the explicit
# no-benchmark result rather than being silently measured against a proxy index.
_BENCHMARKS: Dict[str, BenchmarkSpec] = {
    spec.market: spec for spec in (_in_benchmark(), _us_benchmark(), _cn_benchmark())
}


@dataclass
class BenchmarkWindowReturn:
    """The index's own return over one evaluation window (or why it is unknown)."""

    market: str
    benchmark_symbol: Optional[str]
    benchmark_name: Optional[str]
    convention: str
    start_date: Optional[str]
    eval_window_days: Optional[int]
    benchmark_return_pct: Optional[float]
    start_price: Optional[float] = None
    end_close: Optional[float] = None
    end_date: Optional[str] = None
    source: Optional[str] = None
    reason: Optional[str] = None

    @property
    def is_available(self) -> bool:
        return self.benchmark_return_pct is not None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExcessReturnResult:
    """Absolute vs benchmark vs excess for one scored signal."""

    market: str
    benchmark_symbol: Optional[str]
    benchmark_name: Optional[str]
    convention: str
    start_date: Optional[str]
    eval_window_days: Optional[int]
    # "absolute return" of the signal, as produced by the existing scoring path.
    signal_return_pct: Optional[float]
    benchmark_return_pct: Optional[float]
    excess_return_pct: Optional[float]
    benchmark_source: Optional[str] = None
    reason: Optional[str] = None

    @property
    def is_benchmark_relative(self) -> bool:
        """True only when the excess number is real, not merely absent."""
        return self.excess_return_pct is not None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_benchmark_spec(market: str) -> Optional[BenchmarkSpec]:
    """Default benchmark index for a market, or None when none is configured."""
    return _BENCHMARKS.get((market or "").strip().lower())


def excess_return_pct(
    signal_return_pct: Optional[float],
    benchmark_return_pct: Optional[float],
) -> Optional[float]:
    """signal - benchmark, in percentage points.

    Returns ``None`` - never ``0.0`` - when either leg is unknown or
    non-finite. ``0.0`` is a real claim ("matched the index exactly") and must
    not be produced from missing data.
    """
    signal = _finite_optional_float(signal_return_pct)
    benchmark = _finite_optional_float(benchmark_return_pct)
    if signal is None or benchmark is None:
        return None
    return round(signal - benchmark, 6)


def _finite_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    return numeric


def _coerce_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    # pandas Timestamp / numpy datetime64 and friends
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        try:
            return to_pydatetime().date()
        except Exception:  # noqa: BLE001
            return None
    return None


@dataclass(frozen=True)
class _IndexBar:
    bar_date: date
    open: Optional[float]
    close: Optional[float]


class BenchmarkReturnService:
    """Computes benchmark and excess returns for scored signals.

    ``fetcher_manager`` is injectable so callers (and tests) can supply their
    own ``DataFetcherManager``-shaped object; the real one is constructed lazily
    only when a fetch actually happens.
    """

    def __init__(self, fetcher_manager: Optional[Any] = None) -> None:
        self._fetcher_manager = fetcher_manager

    # ---------------------------------------------------------------- fetching

    def _manager(self) -> Any:
        if self._fetcher_manager is None:
            from data_provider.base import DataFetcherManager

            self._fetcher_manager = DataFetcherManager()
        return self._fetcher_manager

    def _load_index_bars(
        self,
        symbol: str,
        start: date,
        span_days: int,
    ) -> Tuple[List[_IndexBar], Optional[str], Optional[str]]:
        """(bars, source, reason). Reason is set only when bars are unusable."""
        end = start + timedelta(days=span_days)
        try:
            df, source = self._manager().get_daily_data(
                symbol,
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
                days=span_days,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[BenchmarkReturn] index fetch failed for %s: %s", symbol, exc)
            return [], None, REASON_FETCH_FAILED

        if df is None or len(df) == 0:
            logger.warning("[BenchmarkReturn] empty index data for %s (%s..%s)", symbol, start, end)
            return [], source, REASON_EMPTY_DATA

        if "date" not in df.columns or "close" not in df.columns:
            logger.warning("[BenchmarkReturn] index data for %s missing date/close columns", symbol)
            return [], source, REASON_EMPTY_DATA

        has_open = "open" in df.columns
        bars: List[_IndexBar] = []
        for row in df.itertuples(index=False):
            bar_date = _coerce_date(getattr(row, "date", None))
            if bar_date is None:
                continue
            bars.append(
                _IndexBar(
                    bar_date=bar_date,
                    open=_finite_optional_float(getattr(row, "open", None)) if has_open else None,
                    close=_finite_optional_float(getattr(row, "close", None)),
                )
            )

        if not bars:
            return [], source, REASON_EMPTY_DATA

        bars.sort(key=lambda bar: bar.bar_date)
        return bars, source, None

    # ------------------------------------------------------------- benchmark

    def benchmark_return_pct(
        self,
        market: str,
        start_date: Any,
        eval_window_days: int,
    ) -> BenchmarkWindowReturn:
        """Index return over the SAME window used to score the signal.

        Close-to-close: anchor day's close -> close of the ``eval_window_days``-th
        forward bar, matching ``BacktestEngine.evaluate_decision_signal``.
        """
        window_days = int(eval_window_days)
        if window_days <= 0:
            raise ValueError("eval_window_days must be positive")

        spec = get_benchmark_spec(market)
        if spec is None:
            return self._no_benchmark_window(
                market, CONVENTION_CLOSE_TO_CLOSE, start_date, window_days
            )

        anchor = _coerce_date(start_date)
        if anchor is None:
            return self._window_failure(
                spec, CONVENTION_CLOSE_TO_CLOSE, None, window_days, REASON_INVALID_START_DATE
            )

        # Calendar padding so `window_days` *trading* bars fall inside the range
        # (weekends + holidays), same spirit as the callers of get_daily_data.
        bars, source, reason = self._load_index_bars(
            spec.symbol, anchor, span_days=window_days * 3 + 10
        )
        anchor_str = anchor.strftime("%Y-%m-%d")
        if reason is not None:
            return self._window_failure(
                spec, CONVENTION_CLOSE_TO_CLOSE, anchor_str, window_days, reason, source=source
            )

        anchor_bar = next((bar for bar in bars if bar.bar_date == anchor), None)
        if anchor_bar is None:
            return self._window_failure(
                spec,
                CONVENTION_CLOSE_TO_CLOSE,
                anchor_str,
                window_days,
                REASON_ANCHOR_BAR_MISSING,
                source=source,
            )

        start_price = anchor_bar.close
        if start_price is None or start_price <= 0:
            return self._window_failure(
                spec,
                CONVENTION_CLOSE_TO_CLOSE,
                anchor_str,
                window_days,
                REASON_INVALID_ANCHOR_PRICE,
                source=source,
            )

        forward = [bar for bar in bars if bar.bar_date > anchor]
        if len(forward) < window_days:
            return self._window_failure(
                spec,
                CONVENTION_CLOSE_TO_CLOSE,
                anchor_str,
                window_days,
                REASON_INSUFFICIENT_BARS,
                source=source,
                start_price=start_price,
            )

        end_bar = forward[window_days - 1]
        if end_bar.close is None:
            return self._window_failure(
                spec,
                CONVENTION_CLOSE_TO_CLOSE,
                anchor_str,
                window_days,
                REASON_MISSING_END_CLOSE,
                source=source,
                start_price=start_price,
            )

        return BenchmarkWindowReturn(
            market=spec.market,
            benchmark_symbol=spec.symbol,
            benchmark_name=spec.name,
            convention=CONVENTION_CLOSE_TO_CLOSE,
            start_date=anchor_str,
            eval_window_days=window_days,
            benchmark_return_pct=round((end_bar.close - start_price) / start_price * 100.0, 6),
            start_price=start_price,
            end_close=end_bar.close,
            end_date=end_bar.bar_date.strftime("%Y-%m-%d"),
            source=source,
        )

    def intraday_benchmark_return_pct(
        self,
        market: str,
        start_date: Any,
    ) -> BenchmarkWindowReturn:
        """Index same-day open -> close move for the anchor session.

        Matches the intraday signal convention in
        ``decision_signal_outcome_service._evaluate_signal_horizon`` (entry at the
        anchor day's open, exit at that same bar's close, window = 1 day).
        """
        spec = get_benchmark_spec(market)
        if spec is None:
            return self._no_benchmark_window(
                market, CONVENTION_INTRADAY_OPEN_TO_CLOSE, start_date, 1
            )

        anchor = _coerce_date(start_date)
        if anchor is None:
            return self._window_failure(
                spec, CONVENTION_INTRADAY_OPEN_TO_CLOSE, None, 1, REASON_INVALID_START_DATE
            )

        bars, source, reason = self._load_index_bars(spec.symbol, anchor, span_days=7)
        anchor_str = anchor.strftime("%Y-%m-%d")
        if reason is not None:
            return self._window_failure(
                spec, CONVENTION_INTRADAY_OPEN_TO_CLOSE, anchor_str, 1, reason, source=source
            )

        anchor_bar = next((bar for bar in bars if bar.bar_date == anchor), None)
        if anchor_bar is None:
            return self._window_failure(
                spec,
                CONVENTION_INTRADAY_OPEN_TO_CLOSE,
                anchor_str,
                1,
                REASON_ANCHOR_BAR_MISSING,
                source=source,
            )

        start_price = anchor_bar.open
        if start_price is None or start_price <= 0:
            return self._window_failure(
                spec,
                CONVENTION_INTRADAY_OPEN_TO_CLOSE,
                anchor_str,
                1,
                REASON_INVALID_ANCHOR_PRICE,
                source=source,
            )
        if anchor_bar.close is None:
            return self._window_failure(
                spec,
                CONVENTION_INTRADAY_OPEN_TO_CLOSE,
                anchor_str,
                1,
                REASON_MISSING_END_CLOSE,
                source=source,
                start_price=start_price,
            )

        return BenchmarkWindowReturn(
            market=spec.market,
            benchmark_symbol=spec.symbol,
            benchmark_name=spec.name,
            convention=CONVENTION_INTRADAY_OPEN_TO_CLOSE,
            start_date=anchor_str,
            eval_window_days=1,
            benchmark_return_pct=round(
                (anchor_bar.close - start_price) / start_price * 100.0, 6
            ),
            start_price=start_price,
            end_close=anchor_bar.close,
            end_date=anchor_str,
            source=source,
        )

    # ----------------------------------------------------------------- excess

    def evaluate_excess_return(
        self,
        market: str,
        signal_return_pct: Optional[float],
        start_date: Any,
        eval_window_days: int = 1,
        intraday: bool = False,
    ) -> ExcessReturnResult:
        """Absolute + benchmark + excess for one scored signal.

        ``intraday=True`` uses the index's same-day open->close move and ignores
        ``eval_window_days`` (the intraday window is that single session).
        """
        if intraday:
            window = self.intraday_benchmark_return_pct(market, start_date)
        else:
            window = self.benchmark_return_pct(market, start_date, eval_window_days)

        signal = _finite_optional_float(signal_return_pct)
        excess = excess_return_pct(signal, window.benchmark_return_pct)

        reason = window.reason
        if reason is None and signal is None:
            # Benchmark is fine; the absolute leg is what's missing. Still no
            # excess number, and still not zero.
            reason = REASON_MISSING_SIGNAL_RETURN

        return ExcessReturnResult(
            market=window.market,
            benchmark_symbol=window.benchmark_symbol,
            benchmark_name=window.benchmark_name,
            convention=window.convention,
            start_date=window.start_date,
            eval_window_days=window.eval_window_days,
            signal_return_pct=signal,
            benchmark_return_pct=window.benchmark_return_pct,
            excess_return_pct=excess,
            benchmark_source=window.source,
            reason=reason,
        )

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _no_benchmark_window(
        market: str,
        convention: str,
        start_date: Any,
        eval_window_days: Optional[int],
    ) -> BenchmarkWindowReturn:
        anchor = _coerce_date(start_date)
        return BenchmarkWindowReturn(
            market=(market or "").strip().lower(),
            benchmark_symbol=None,
            benchmark_name=None,
            convention=convention,
            start_date=anchor.strftime("%Y-%m-%d") if anchor else None,
            eval_window_days=eval_window_days,
            benchmark_return_pct=None,
            reason=REASON_NO_BENCHMARK,
        )

    @staticmethod
    def _window_failure(
        spec: BenchmarkSpec,
        convention: str,
        start_date: Optional[str],
        eval_window_days: int,
        reason: str,
        source: Optional[str] = None,
        start_price: Optional[float] = None,
    ) -> BenchmarkWindowReturn:
        return BenchmarkWindowReturn(
            market=spec.market,
            benchmark_symbol=spec.symbol,
            benchmark_name=spec.name,
            convention=convention,
            start_date=start_date,
            eval_window_days=eval_window_days,
            benchmark_return_pct=None,
            start_price=start_price,
            source=source,
            reason=reason,
        )
