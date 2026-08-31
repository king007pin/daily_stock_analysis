# -*- coding: utf-8 -*-
"""DecisionSignal feedback, forward outcome, and stats service."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
import json
import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.core.backtest_engine import BacktestEngine, EvaluationConfig
from src.repositories.decision_signal_outcome_repo import (
    DecisionSignalOutcomeRepository,
    OutcomeStatsRow,
)
from src.repositories.decision_signal_repo import DecisionSignalRepository
from src.repositories.stock_repo import StockRepository
from src.schemas.decision_profile import VALID_DECISION_PROFILES
from src.services.benchmark_return_service import BenchmarkReturnService
from src.services.decision_signal_data_quality import normalize_decision_signal_data_quality
from src.services.decision_signal_service import (
    HORIZONS,
    SIGNAL_STATUSES,
    SOURCE_TYPES,
    DecisionSignalNotFoundError,
    DecisionSignalService,
)
from src.storage import (
    DatabaseManager,
    DecisionSignalFeedbackRecord,
    DecisionSignalOutcomeRecord,
    DecisionSignalRecord,
)
from src.utils.sanitize import sanitize_decision_signal_text


logger = logging.getLogger(__name__)

# v2 (2026-08-24): scoring semantics changed, so previously stored outcomes are
# not comparable and must be recomputed rather than mixed into the same stats.
#   - "not_up" gained a neutral band (was: any move under +band scored a hit,
#     which made every defensive call ~89% correct against real bars).
#   - The neutral band is now read from config per horizon instead of being
#     hardcoded to 2.0.
# Rows written by v1 are ignored by the candidate scan, so they are re-evaluated
# automatically on the next run.
DECISION_SIGNAL_OUTCOME_ENGINE_VERSION = "decision-signal-v2"
SUPPORTED_OUTCOME_HORIZONS = {
    "intraday": 1,
    "1d": 1,
    "3d": 3,
    "5d": 5,
    "10d": 10,
}
DEFAULT_STATS_STATUSES = ("active", "expired", "invalidated", "closed")
OUTCOME_VALUES = frozenset({"hit", "miss", "neutral"})
EVAL_STATUSES = frozenset({"completed", "unable"})
FEEDBACK_VALUES = frozenset({"useful", "not_useful"})
FEEDBACK_SOURCES = frozenset({"web", "api"})
HOLDING_STATES = frozenset({"holding", "empty", "unknown"})
# Direction each canonical action asserts about future price.
DIRECTION_BY_ACTION = {
    "buy": "up",
    "add": "up",
    "hold": "not_down",
    "reduce": "not_up",
    "sell": "not_up",
    "avoid": "not_up",
}

# Actions that assert no direction at all, and so cannot be scored.
#
# This is deliberate, not an omission. "watch" was measured against 1,476 real
# daily bars: scoring it as "not_down" (which hits on any return >= 0) yields a
# ~78% win rate, and "alert" as "not_up" yields ~89%, purely from market drift.
# A genuine directional call on the same bars scores 41-46%. Treating either as
# directional would manufacture a track record rather than measure one.
#
# The correct handling of a high non-directional rate is to report it as a
# coverage problem in signal generation, not to score around it.
NON_DIRECTIONAL_ACTIONS = frozenset({"watch", "alert"})

# Which side of the bar counts as "adverse" when describing max excursion.
#
# Deliberately broader than DIRECTION_BY_ACTION, and that difference is the
# point rather than an inconsistency: max adverse excursion is a *descriptive*
# statistic about what the price did afterwards, not a scored outcome. A
# "watch" on a name being considered for a long entry has a meaningful downside
# excursion even though the signal itself asserts no direction and is never
# scored. These two mappings must stay separately named so the distinction
# survives; before this they were duplicate literal sets that silently
# disagreed.
LONG_SIDE_EXCURSION_ACTIONS = frozenset({"buy", "add", "hold", "watch", "alert"})
SHORT_SIDE_EXCURSION_ACTIONS = frozenset({"sell", "reduce", "avoid"})

RETRYABLE_UNABLE_REASONS = frozenset({
    "missing_anchor_price",
    "invalid_anchor_price",
    "insufficient_forward_bars",
    "missing_end_close",
    "invalid_end_close",
})
BATCH_CANDIDATE_SCAN_PAGE_SIZE = 500
MIN_PROFILE_CALIBRATION_SAMPLE_SIZE = 30
PROFILE_SOURCES = frozenset({
    "auto_default",
    "backfill_defaulted",
    "legacy_unknown",
    "user_selected",
})
PROFILE_CALIBRATION_BREAKDOWN_DIMENSIONS = (
    ("decision_profile", ("decision_profile",)),
    ("decision_profile_action", ("decision_profile", "action")),
    ("decision_profile_horizon", ("decision_profile", "horizon")),
    ("decision_profile_market_phase", ("decision_profile", "market_phase")),
    (
        "decision_profile_data_quality_level",
        ("decision_profile", "data_quality_level"),
    ),
    ("profile_source", ("profile_source",)),
)


class DecisionSignalOutcomeService:
    """Business logic for signal outcomes, stats, and feedback."""

    def __init__(
        self,
        *,
        repo: Optional[DecisionSignalOutcomeRepository] = None,
        signal_repo: Optional[DecisionSignalRepository] = None,
        stock_repo: Optional[StockRepository] = None,
        db_manager: Optional[DatabaseManager] = None,
        refill_enabled: Optional[bool] = None,
        benchmark_service: Optional[BenchmarkReturnService] = None,
    ):
        self.repo = repo or DecisionSignalOutcomeRepository(db_manager)
        self.signal_repo = signal_repo or DecisionSignalRepository(db_manager)
        self.stock_repo = stock_repo or StockRepository(db_manager)
        # 基准腿要走网络取指数数据，因此与上面的 refill 一样是 opt-in：
        # 不配置时行为与之前完全一致，离线测试套件也就仍然是离线的。
        # 显式注入 benchmark_service（测试或调度环境）时视为已启用。
        self._benchmark_enabled = (
            True if benchmark_service is not None
            else self._benchmark_enabled_from_config()
        )
        self._benchmark_service = benchmark_service or BenchmarkReturnService()
        # Refilling missing daily bars means a network fetch, so it is opt-in:
        # unset behaves exactly as before, which also keeps the offline test
        # suite offline. Enable it in the scheduled environment, where a stale
        # stock_daily is precisely what stops recorded signals from maturing.
        self._refill_enabled = (
            bool(refill_enabled)
            if refill_enabled is not None
            else self._refill_enabled_from_config()
        )

    @staticmethod
    def _benchmark_enabled_from_config() -> bool:
        try:
            from src.config import get_config

            return bool(
                getattr(get_config(), "decision_outcome_benchmark_enabled", False)
            )
        except Exception:  # noqa: BLE001 - config must never break evaluation
            return False

    @staticmethod
    def _refill_enabled_from_config() -> bool:
        try:
            from src.config import get_config

            return bool(getattr(get_config(), "decision_outcome_daily_refill_enabled", False))
        except Exception:  # noqa: BLE001 - config must never break evaluation
            return False

    def run_outcomes(
        self,
        *,
        signal_id: Optional[int] = None,
        horizons: Optional[List[str]] = None,
        force: bool = False,
        market: Optional[str] = None,
        stock_code: Optional[str] = None,
        action: Optional[str] = None,
        source_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        signal_id_norm = self._optional_positive_int(signal_id, "signal_id")
        market_norm = DecisionSignalService._normalize_optional_market(market)
        action_norm = DecisionSignalService._normalize_optional_action(action)
        source_type_norm = self._normalize_optional_enum(source_type, SOURCE_TYPES, "source_type")
        status_norm = self._normalize_optional_enum(status, SIGNAL_STATUSES, "status")
        stock_codes_norm = DecisionSignalService._stock_filter_codes(stock_code, market=market_norm)
        horizons_norm = self._normalize_horizons(horizons)
        safe_limit = max(1, min(int(limit), 500))

        statuses = [status_norm] if status_norm else None
        if signal_id_norm is None and statuses is None:
            statuses = list(DEFAULT_STATS_STATUSES)

        if signal_id_norm is None and not force:
            signals = self._list_actionable_candidate_signals(
                stock_codes=stock_codes_norm,
                market=market_norm,
                action=action_norm,
                source_type=source_type_norm,
                statuses=statuses,
                requested_horizons=horizons_norm,
                limit=safe_limit,
            )
        else:
            signals = self.repo.list_candidate_signals(
                signal_id=signal_id_norm,
                stock_codes=stock_codes_norm,
                market=market_norm,
                action=action_norm,
                source_type=source_type_norm,
                statuses=statuses,
                limit=safe_limit,
            )
        if signal_id_norm is not None and not signals:
            raise DecisionSignalNotFoundError(f"Decision signal not found: {signal_id_norm}")

        items: List[Dict[str, Any]] = []
        created_count = 0
        updated_count = 0
        skipped_count = 0

        for signal in signals:
            for horizon in self._horizons_for_signal(signal, horizons_norm):
                existing = self.repo.get_outcome(
                    signal_id=signal.id,
                    horizon=horizon,
                    engine_version=DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
                )
                if existing is not None and not force and not self._should_recompute_outcome(existing):
                    skipped_count += 1
                    items.append(self._serialize_outcome(existing))
                    continue

                fields = self._evaluate_signal_horizon(signal, horizon)
                row, created = self.repo.upsert_outcome(fields)
                if created:
                    created_count += 1
                else:
                    updated_count += 1
                items.append(self._serialize_outcome(row))

        return {
            "items": items,
            "evaluated": created_count + updated_count,
            "created": created_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "engine_version": DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
        }

    def _list_actionable_candidate_signals(
        self,
        *,
        stock_codes: Optional[List[str]],
        market: Optional[str],
        action: Optional[str],
        source_type: Optional[str],
        statuses: Optional[List[str]],
        requested_horizons: Optional[List[str]],
        limit: int,
    ) -> List[DecisionSignalRecord]:
        selected: List[DecisionSignalRecord] = []
        selected_ids = set()
        retryable_reserve: List[Tuple[datetime, int, DecisionSignalRecord]] = []
        retryable_ids = set()
        offset = 0

        while len(selected) < limit:
            page = self.repo.list_candidate_signals(
                stock_codes=stock_codes,
                market=market,
                action=action,
                source_type=source_type,
                statuses=statuses,
                offset=offset,
                limit=BATCH_CANDIDATE_SCAN_PAGE_SIZE,
            )
            if not page:
                break

            outcomes = self.repo.list_outcomes_for_signals(
                signal_ids=[int(signal.id) for signal in page],
                engine_version=DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
            )
            outcomes_by_key: Dict[Tuple[int, str], DecisionSignalOutcomeRecord] = {
                (int(row.signal_id), row.horizon): row
                for row in outcomes
            }

            for signal in page:
                actionability, retryable_at = self._candidate_actionability(
                    signal,
                    requested_horizons=requested_horizons,
                    outcomes_by_key=outcomes_by_key,
                )
                signal_id = int(signal.id)
                if actionability == "missing":
                    if signal_id not in selected_ids:
                        selected.append(signal)
                        selected_ids.add(signal_id)
                    if len(selected) >= limit:
                        break
                elif actionability == "retryable" and signal_id not in retryable_ids:
                    retryable_reserve.append((retryable_at, signal_id, signal))
                    retryable_ids.add(signal_id)

            offset += len(page)
            if len(page) < BATCH_CANDIDATE_SCAN_PAGE_SIZE:
                break

        if len(selected) < limit:
            retryable_reserve.sort(key=lambda item: (item[0], item[1]))
            for _retryable_at, signal_id, signal in retryable_reserve:
                signal_id = int(signal.id)
                if signal_id in selected_ids:
                    continue
                selected.append(signal)
                selected_ids.add(signal_id)
                if len(selected) >= limit:
                    break

        return selected

    def _candidate_actionability(
        self,
        signal: DecisionSignalRecord,
        *,
        requested_horizons: Optional[List[str]],
        outcomes_by_key: Dict[Tuple[int, str], DecisionSignalOutcomeRecord],
    ) -> Tuple[Optional[str], Optional[datetime]]:
        retryable_times: List[datetime] = []
        signal_id = int(signal.id)
        for horizon in self._horizons_for_signal(signal, requested_horizons):
            existing = outcomes_by_key.get((signal_id, horizon))
            if existing is None:
                return "missing", None
            if self._should_recompute_outcome(existing):
                retryable_times.append(self._outcome_retryable_sort_time(existing))
        if retryable_times:
            return "retryable", min(retryable_times)
        return None, None

    @staticmethod
    def _outcome_retryable_sort_time(row: DecisionSignalOutcomeRecord) -> datetime:
        return row.updated_at or row.created_at or datetime.min

    @staticmethod
    def _should_recompute_outcome(row: DecisionSignalOutcomeRecord) -> bool:
        return row.eval_status == "unable" and row.unable_reason in RETRYABLE_UNABLE_REASONS

    def list_outcomes(
        self,
        *,
        signal_id: Optional[int] = None,
        horizon: Optional[str] = None,
        engine_version: Optional[str] = None,
        eval_status: Optional[str] = None,
        outcome: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        signal_id_norm = self._optional_positive_int(signal_id, "signal_id")
        horizon_norm = self._normalize_optional_enum(horizon, HORIZONS, "horizon")
        engine_version_norm = str(engine_version or DECISION_SIGNAL_OUTCOME_ENGINE_VERSION).strip()
        eval_status_norm = self._normalize_optional_enum(eval_status, EVAL_STATUSES, "eval_status")
        outcome_norm = self._normalize_optional_enum(outcome, OUTCOME_VALUES, "outcome")
        safe_page = max(1, int(page))
        safe_page_size = max(1, min(int(page_size), 100))
        rows, total = self.repo.list_outcomes(
            signal_id=signal_id_norm,
            horizon=horizon_norm,
            engine_version=engine_version_norm,
            eval_status=eval_status_norm,
            outcome=outcome_norm,
            page=safe_page,
            page_size=safe_page_size,
        )
        return {
            "items": [self._serialize_outcome(row) for row in rows],
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
        }

    def list_signal_outcomes(self, signal_id: int) -> Dict[str, Any]:
        signal_id_norm = self._require_existing_signal(signal_id).id
        return self.list_outcomes(
            signal_id=signal_id_norm,
            engine_version=DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
            page=1,
            page_size=100,
        )

    def get_stats(
        self,
        *,
        horizons: Optional[List[str]] = None,
        engine_version: Optional[str] = None,
        statuses: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        engine_version_norm = str(engine_version or DECISION_SIGNAL_OUTCOME_ENGINE_VERSION).strip()
        horizons_norm = self._normalize_horizons(horizons)
        statuses_norm = (
            [self._normalize_enum(item, SIGNAL_STATUSES, "status") for item in statuses]
            if statuses
            else list(DEFAULT_STATS_STATUSES)
        )
        stats_rows = self.repo.list_stats_rows(
            engine_version=engine_version_norm,
            horizons=horizons_norm,
            statuses=statuses_norm,
        )
        rows = [stats_row.outcome for stats_row in stats_rows]
        dimensions = (
            "action",
            "market",
            "market_phase",
            "source_type",
            "source_agent",
            "plan_quality",
            "data_quality_level",
            "holding_state",
        )
        breakdowns = {
            dimension: self._breakdown(rows, dimension)
            for dimension in dimensions
        }
        return {
            **self._aggregate(rows),
            "engine_version": engine_version_norm,
            "horizons": horizons_norm,
            "statuses": statuses_norm,
            "breakdowns": breakdowns,
            "profile_calibration": self._profile_calibration(stats_rows),
        }

    def get_feedback(self, signal_id: int) -> Dict[str, Any]:
        signal = self._require_existing_signal(signal_id)
        row = self.repo.get_feedback(signal_id=signal.id)
        if row is None:
            return {
                "signal_id": signal.id,
                "feedback_value": None,
                "reason_code": None,
                "note": None,
                "source": None,
                "created_at": None,
                "updated_at": None,
            }
        return self._serialize_feedback(row)

    def put_feedback(
        self,
        signal_id: int,
        *,
        feedback_value: str,
        reason_code: Optional[str] = None,
        note: Optional[str] = None,
        source: str = "api",
    ) -> Dict[str, Any]:
        signal = self._require_existing_signal(signal_id)
        fields = {
            "signal_id": signal.id,
            "feedback_value": self._normalize_enum(feedback_value, FEEDBACK_VALUES, "feedback_value"),
            "reason_code": self._optional_public_text(reason_code, "reason_code", max_length=64),
            "note": self._optional_public_text(note, "note", max_length=1000),
            "source": self._normalize_enum(source or "api", FEEDBACK_SOURCES, "source"),
        }
        row = self.repo.upsert_feedback(fields)
        return self._serialize_feedback(row)

    def _bars_are_inadequate(
        self,
        stock_code: str,
        anchor_date: date,
        horizon: str,
        eval_days: int,
        start_bar: Any,
    ) -> bool:
        """True when stock_daily lacks the bars this evaluation needs.

        Kept deliberately cheap: it answers "is a refill worth attempting",
        not "will evaluation succeed". A false positive costs one fetch.
        """
        if start_bar is None:
            return True
        if horizon == "intraday":
            # The anchor day's own bar is the whole window.
            return False
        try:
            forward = self.stock_repo.get_forward_bars(
                code=stock_code,
                analysis_date=anchor_date,
                eval_window_days=eval_days,
            )
        except Exception:  # noqa: BLE001 - a probe must never break evaluation
            return False
        return len(forward or []) < int(eval_days)

    def _try_fill_daily_data(
        self,
        *,
        code: str,
        anchor_date: date,
        eval_window_days: int,
    ) -> None:
        """Fetch and persist daily bars covering the evaluation window.

        Mirrors ``BacktestService._try_fill_daily_data`` deliberately — same
        fetcher chain, same persistence call — rather than introducing a second
        way of doing this. Fail-soft: a data-source outage must degrade the
        outcome to "unable" as before, never raise into the evaluation loop.
        """
        refill_code = str(code or "").strip()
        if not refill_code:
            return

        try:
            from datetime import timedelta

            from data_provider.base import DataFetcherManager

            end_date = anchor_date + timedelta(days=max(int(eval_window_days) * 2, 30))
            manager = DataFetcherManager()
            df, source = manager.get_daily_data(
                stock_code=refill_code,
                start_date=anchor_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                days=int(eval_window_days) * 2,
            )
            if df is None or getattr(df, "empty", True):
                return
            saved = self.stock_repo.save_dataframe(df, refill_code, source)
            logger.info(
                "[DecisionSignalOutcome] refilled daily bars code=%s anchor=%s saved=%s source=%s",
                refill_code,
                anchor_date,
                saved,
                source,
            )
        except Exception as exc:  # noqa: BLE001 - fail soft by design
            logger.warning("[DecisionSignalOutcome] daily refill failed (%s): %s", refill_code, exc)

    def _evaluate_signal_horizon(self, signal: DecisionSignalRecord, horizon: str) -> Dict[str, Any]:
        base = self._snapshot_fields(signal, horizon)
        direction = self._direction_for_action(signal.action)
        if direction is None:
            return self._unable_fields(base, reason="non_directional_action")

        eval_days = SUPPORTED_OUTCOME_HORIZONS.get(horizon)
        if eval_days is None:
            return self._unable_fields(base, reason="unsupported_horizon", direction_expected=direction)

        anchor_date = self._anchor_date(signal)
        if anchor_date is None:
            return self._unable_fields(base, reason="missing_anchor_date", direction_expected=direction)

        start_bar = self.stock_repo.get_daily_on_date(code=signal.stock_code, target_date=anchor_date)
        if self._refill_enabled and self._bars_are_inadequate(
            signal.stock_code, anchor_date, horizon, eval_days, start_bar
        ):
            # Self-heal a gap in stock_daily rather than recording a permanent
            # "unable" for what is only a missing-data problem. BacktestService
            # has done this since it was written (_try_fill_daily_data, called
            # from run_backtest); this path had no equivalent, so whenever the
            # scheduled analysis jobs stopped running, bars went stale and
            # already-recorded signals could never mature. Verified 2026-08-24:
            # outcomes sat at insufficient_forward_bars until bars were
            # backfilled by hand.
            self._try_fill_daily_data(
                code=signal.stock_code,
                anchor_date=anchor_date,
                eval_window_days=eval_days,
            )
            start_bar = self.stock_repo.get_daily_on_date(
                code=signal.stock_code, target_date=anchor_date
            )

        if horizon == "intraday":
            # Same-day proxy, not a true intraday-bar evaluation: no sub-daily
            # historical data source is wired in yet (tracked separately as a
            # forecasting-granularity gap). get_forward_bars() excludes the
            # anchor day by design and is built for swing/EOD semantics (enter
            # at close, evaluate N days later) — wrong for a same-day signal.
            # Entry = anchor day's open; the "forward window" is that SAME
            # day's own bar, i.e. a forced same-day square-off. Stop-loss and
            # take-profit hit ordering within that single bar is still
            # unresolvable from daily OHLC alone (BacktestEngine falls back to
            # its documented ambiguous/stop-loss-first assumption) — this
            # fixes the window being wrong, not the intraday ordering gap.
            start_price = getattr(start_bar, "open", None)
            forward_bars = [start_bar] if start_bar is not None else []
        else:
            start_price = getattr(start_bar, "close", None)
            forward_bars = self.stock_repo.get_forward_bars(
                code=signal.stock_code,
                analysis_date=anchor_date,
                eval_window_days=eval_days,
            )

        if start_price is None:
            return self._unable_fields(
                base,
                reason="missing_anchor_price",
                direction_expected=direction,
                anchor_date=anchor_date,
                eval_window_days=eval_days,
            )
        if not self._is_positive_finite(start_price):
            return self._unable_fields(
                base,
                reason="invalid_anchor_price",
                direction_expected=direction,
                anchor_date=anchor_date,
                eval_window_days=eval_days,
                start_price=start_price,
            )
        evaluation = BacktestEngine.evaluate_decision_signal(
            direction_expected=direction,
            anchor_date=anchor_date,
            start_price=float(start_price),
            forward_bars=forward_bars,
            config=EvaluationConfig(
                eval_window_days=eval_days,
                neutral_band_pct=self._neutral_band_pct_for(horizon),
                engine_version=DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
            ),
        )
        fields = {
            **base,
            "eval_status": evaluation.get("eval_status"),
            "outcome": evaluation.get("outcome"),
            "direction_expected": direction,
            "direction_correct": evaluation.get("direction_correct"),
            "unable_reason": evaluation.get("unable_reason"),
            "anchor_date": anchor_date,
            "eval_window_days": eval_days,
            "start_price": evaluation.get("start_price", start_price),
            "end_close": evaluation.get("end_close"),
            "max_high": evaluation.get("max_high"),
            "min_low": evaluation.get("min_low"),
            "stock_return_pct": evaluation.get("stock_return_pct"),
        }
        fields.update(
            self._benchmark_fields(
                market=signal.market,
                signal_return_pct=evaluation.get("stock_return_pct"),
                anchor_date=anchor_date,
                eval_window_days=eval_days,
                intraday=horizon == "intraday",
            )
        )
        return fields

    def _benchmark_fields(
        self,
        *,
        market: Optional[str],
        signal_return_pct: Optional[float],
        anchor_date: date,
        eval_window_days: int,
        intraday: bool,
    ) -> Dict[str, Any]:
        """Benchmark leg for one scored signal.

        绝对收益本身回答不了"这一笔是否跑赢了它所处的市场"——+15% 落在 +20%
        的指数里是跑输。这里补上基准腿与超额收益。

        这些字段是**补充**，不改变 ``outcome`` / ``stock_return_pct`` 的含义，
        因此不需要新的 ``engine_version``。基准取不到时写入 ``benchmark_reason``
        并让数值保持为 None —— 缺失不得被记作 0。
        """
        if not self._benchmark_enabled:
            return {"benchmark_reason": "benchmark_disabled"}

        try:
            result = self._benchmark_service.evaluate_excess_return(
                market=market or "",
                signal_return_pct=signal_return_pct,
                start_date=anchor_date,
                eval_window_days=eval_window_days,
                intraday=intraday,
            )
        except Exception:
            logger.warning(
                "[DecisionSignalOutcome] benchmark evaluation failed market=%s anchor=%s",
                market,
                anchor_date,
                exc_info=True,
            )
            return {"benchmark_reason": "benchmark_evaluation_failed"}

        return {
            "benchmark_symbol": result.benchmark_symbol,
            "benchmark_return_pct": result.benchmark_return_pct,
            "excess_return_pct": result.excess_return_pct,
            "benchmark_reason": result.reason,
        }

    @staticmethod
    def _neutral_band_pct_for(horizon: str) -> float:
        """Neutral band for one horizon, from config.

        Previously hardcoded to 2.0 here, which ignored the configured
        ``BACKTEST_NEUTRAL_BAND_PCT`` that the rest of the codebase already
        honours (see ``backtest_service``). Falls back to the flat value when
        no per-horizon override is set, so an unconfigured deployment behaves
        exactly as before.
        """
        try:
            from src.config import get_config

            config = get_config()
        except Exception:  # noqa: BLE001 - config must never break evaluation
            return 2.0

        by_horizon = getattr(config, "backtest_neutral_band_pct_by_horizon", None) or {}
        flat = float(getattr(config, "backtest_neutral_band_pct", 2.0))
        try:
            return float(by_horizon.get(horizon, flat))
        except (TypeError, ValueError):
            return flat

    @staticmethod
    def _direction_for_action(action: Optional[str]) -> Optional[str]:
        """Map a canonical action to an expected direction, or None if it has none.

        Every member of ``DecisionAction`` must appear in exactly one of
        ``DIRECTION_BY_ACTION`` or ``NON_DIRECTIONAL_ACTIONS``; see
        ``test_every_decision_action_is_explicitly_classified``. Falling
        through silently is what previously let a whole action class go
        permanently unscored without anything reporting it.
        """
        if action in NON_DIRECTIONAL_ACTIONS:
            return None
        return DIRECTION_BY_ACTION.get(action or "")

    def _snapshot_fields(self, signal: DecisionSignalRecord, horizon: str) -> Dict[str, Any]:
        data_quality_level = self._data_quality_level(signal)
        holding_state = self._holding_state(signal)
        return {
            "signal_id": signal.id,
            "horizon": horizon,
            "engine_version": DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
            "action": signal.action,
            "market": signal.market,
            "market_phase": signal.market_phase,
            "source_type": signal.source_type,
            "source_agent": signal.source_agent,
            "plan_quality": signal.plan_quality,
            "data_quality_level": data_quality_level,
            "holding_state": holding_state,
        }

    @staticmethod
    def _unable_fields(
        base: Dict[str, Any],
        *,
        reason: str,
        direction_expected: Optional[str] = None,
        anchor_date: Optional[date] = None,
        eval_window_days: Optional[int] = None,
        start_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        return {
            **base,
            "eval_status": "unable",
            "outcome": None,
            "direction_expected": direction_expected,
            "direction_correct": None,
            "unable_reason": reason,
            "anchor_date": anchor_date,
            "eval_window_days": eval_window_days,
            "start_price": start_price,
            "end_close": None,
            "max_high": None,
            "min_low": None,
            "stock_return_pct": None,
        }

    def _anchor_date(self, signal: DecisionSignalRecord) -> Optional[date]:
        metadata = self._json_loads(signal.metadata_json)
        if isinstance(metadata, dict):
            summary = metadata.get("market_phase_summary")
            if isinstance(summary, dict):
                parsed = self._parse_date(summary.get("session_date"))
                if parsed is not None:
                    return parsed
        return self._parse_date(signal.created_at)

    def _data_quality_level(self, signal: DecisionSignalRecord) -> str:
        raw_summary = signal.data_quality_summary_json
        if raw_summary and raw_summary.strip():
            try:
                summary = json.loads(raw_summary)
            except json.JSONDecodeError as exc:
                logger.warning("Invalid decision signal sidecar source JSON: %s", exc)
                return "unknown"
            explicit_level = self._explicit_data_quality_level(summary)
            if explicit_level is not None:
                return self._short_label(explicit_level)
        metadata = self._json_loads(signal.metadata_json)
        if isinstance(metadata, dict):
            return normalize_decision_signal_data_quality(metadata.get("data_quality_level"))
        return "unknown"

    @staticmethod
    def _explicit_data_quality_level(value: Any) -> Optional[Any]:
        if isinstance(value, dict):
            for key in ("level", "quality_level"):
                level = value.get(key)
                if level not in (None, ""):
                    return level
            nested = value.get("data_quality")
            if isinstance(nested, dict) and nested.get("level") not in (None, ""):
                return nested.get("level")
            return None
        if isinstance(value, str) and value.strip():
            return value
        return None

    def _holding_state(self, signal: DecisionSignalRecord) -> str:
        metadata = self._json_loads(signal.metadata_json)
        if isinstance(metadata, dict):
            value = str(metadata.get("holding_state") or "").strip().lower()
            if value in HOLDING_STATES:
                return value
        return "unknown"

    @staticmethod
    def _short_label(value: Any) -> str:
        text = str(value or "").strip().lower()
        return text[:24] or "unknown"

    @staticmethod
    def _json_loads(value: Optional[str]) -> Any:
        if not value:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            logger.warning("Invalid decision signal sidecar source JSON: %s", exc)
            return None

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
        return None

    @staticmethod
    def _is_positive_finite(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0

    def _horizons_for_signal(self, signal: DecisionSignalRecord, requested: Optional[List[str]]) -> List[str]:
        if requested:
            return requested
        horizon = str(signal.horizon or "").strip()
        if horizon:
            return [horizon]
        return list(SUPPORTED_OUTCOME_HORIZONS.keys())

    def _require_existing_signal(self, signal_id: int) -> DecisionSignalRecord:
        signal_id_norm = self._optional_positive_int(signal_id, "signal_id")
        row = self.signal_repo.get(signal_id_norm)
        if row is None:
            raise DecisionSignalNotFoundError(f"Decision signal not found: {signal_id_norm}")
        return row

    @staticmethod
    def _optional_positive_int(value: Any, field_name: str) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
        if number <= 0:
            raise ValueError(f"{field_name} must be positive")
        return number

    @staticmethod
    def _normalize_enum(value: Any, allowed: Iterable[str], field_name: str) -> str:
        text = str(value or "").strip()
        allowed_set = set(allowed)
        if text not in allowed_set:
            allowed_text = ", ".join(sorted(allowed_set))
            raise ValueError(f"{field_name} must be one of {allowed_text}")
        return text

    @classmethod
    def _normalize_optional_enum(cls, value: Any, allowed: Iterable[str], field_name: str) -> Optional[str]:
        if value in (None, ""):
            return None
        return cls._normalize_enum(value, allowed, field_name)

    def _normalize_horizons(self, values: Optional[List[str]]) -> Optional[List[str]]:
        if not values:
            return None
        out: List[str] = []
        for value in values:
            horizon = self._normalize_enum(value, HORIZONS, "horizon")
            if horizon not in out:
                out.append(horizon)
        return out

    @staticmethod
    def _optional_public_text(value: Any, field_name: str, *, max_length: int) -> Optional[str]:
        if value in (None, ""):
            return None
        text = sanitize_decision_signal_text(value)
        if not text:
            return None
        if len(text) > max_length:
            raise ValueError(f"{field_name} must be at most {max_length} characters")
        return text

    @staticmethod
    def _serialize_outcome(row: DecisionSignalOutcomeRecord) -> Dict[str, Any]:
        return {
            "id": row.id,
            "signal_id": row.signal_id,
            "horizon": row.horizon,
            "engine_version": row.engine_version,
            "eval_status": row.eval_status,
            "outcome": row.outcome,
            "direction_expected": row.direction_expected,
            "direction_correct": row.direction_correct,
            "unable_reason": row.unable_reason,
            "anchor_date": row.anchor_date.isoformat() if row.anchor_date else None,
            "eval_window_days": row.eval_window_days,
            "start_price": row.start_price,
            "end_close": row.end_close,
            "max_high": row.max_high,
            "min_low": row.min_low,
            "stock_return_pct": row.stock_return_pct,
            "action": row.action,
            "market": row.market,
            "market_phase": row.market_phase,
            "source_type": row.source_type,
            "source_agent": row.source_agent,
            "plan_quality": row.plan_quality,
            "data_quality_level": row.data_quality_level,
            "holding_state": row.holding_state,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _serialize_feedback(row: DecisionSignalFeedbackRecord) -> Dict[str, Any]:
        return {
            "signal_id": row.signal_id,
            "feedback_value": row.feedback_value,
            "reason_code": row.reason_code,
            "note": row.note,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def _profile_calibration(self, stats_rows: List[OutcomeStatsRow]) -> Dict[str, Any]:
        samples: List[Dict[str, Any]] = []
        for stats_row in stats_rows:
            outcome = stats_row.outcome
            samples.append({
                "outcome": outcome,
                "decision_profile": self._profile_dimension(stats_row.decision_profile),
                "action": str(outcome.action or "unknown"),
                "horizon": str(outcome.horizon or "unknown"),
                "market_phase": str(outcome.market_phase or "unknown"),
                "data_quality_level": str(outcome.data_quality_level or "unknown"),
                "profile_source": self._profile_source(stats_row.metadata_json),
            })
        breakdowns = {
            name: self._profile_calibration_breakdown(samples, dimensions)
            for name, dimensions in PROFILE_CALIBRATION_BREAKDOWN_DIMENSIONS
        }
        return {
            "minimum_completed_sample_size": MIN_PROFILE_CALIBRATION_SAMPLE_SIZE,
            "breakdowns": breakdowns,
        }

    def _profile_calibration_breakdown(
        self,
        samples: List[Dict[str, Any]],
        dimensions: Tuple[str, ...],
    ) -> List[Dict[str, Any]]:
        grouped: Dict[Tuple[str, ...], List[DecisionSignalOutcomeRecord]] = defaultdict(list)
        for sample in samples:
            key = tuple(str(sample.get(dimension) or "unknown") for dimension in dimensions)
            grouped[key].append(sample["outcome"])

        buckets = [
            {
                "dimensions": dict(zip(dimensions, values)),
                **self._profile_calibration_aggregate(rows),
            }
            for values, rows in grouped.items()
        ]
        return sorted(
            buckets,
            key=lambda item: (
                -int(item["total"]),
                tuple(str(item["dimensions"][dimension]) for dimension in dimensions),
            ),
        )

    def _profile_calibration_aggregate(
        self,
        rows: List[DecisionSignalOutcomeRecord],
    ) -> Dict[str, Any]:
        aggregate = self._aggregate(rows)
        sample_sufficient = int(aggregate["completed"]) >= MIN_PROFILE_CALIBRATION_SAMPLE_SIZE
        direction_denominator = int(aggregate["hit"]) + int(aggregate["miss"])
        adverse_excursions = [
            value
            for row in rows
            if (value := self._row_max_adverse_excursion_pct(row)) is not None
        ]
        return {
            "total": aggregate["total"],
            "completed": aggregate["completed"],
            "unable": aggregate["unable"],
            "hit": aggregate["hit"],
            "miss": aggregate["miss"],
            "neutral": aggregate["neutral"],
            "sample_sufficient": sample_sufficient,
            "hit_rate_pct": aggregate["hit_rate_pct"] if sample_sufficient else None,
            "avg_stock_return_pct": aggregate["avg_stock_return_pct"] if sample_sufficient else None,
            "miss_rate_pct": (
                round(int(aggregate["miss"]) / direction_denominator * 100, 2)
                if sample_sufficient and direction_denominator
                else None
            ),
            "unable_rate_pct": (
                round(int(aggregate["unable"]) / int(aggregate["total"]) * 100, 2)
                if sample_sufficient and int(aggregate["total"])
                else None
            ),
            "max_adverse_excursion_pct": (
                round(max(adverse_excursions), 4)
                if sample_sufficient and adverse_excursions
                else None
            ),
        }

    @classmethod
    def _row_max_adverse_excursion_pct(
        cls,
        row: DecisionSignalOutcomeRecord,
    ) -> Optional[float]:
        if not cls._is_positive_finite(row.start_price):
            return None
        start_price = float(row.start_price)
        if row.action in LONG_SIDE_EXCURSION_ACTIONS:
            if not cls._is_positive_finite(row.min_low):
                return None
            return max(0.0, (start_price - float(row.min_low)) / start_price * 100)
        if row.action in SHORT_SIDE_EXCURSION_ACTIONS:
            if not cls._is_positive_finite(row.max_high):
                return None
            return max(0.0, (float(row.max_high) - start_price) / start_price * 100)
        return None

    @staticmethod
    def _profile_dimension(value: Any) -> str:
        profile = str(value or "").strip().lower()
        return profile if profile in VALID_DECISION_PROFILES else "unknown"

    def _profile_source(self, metadata_json: Optional[str]) -> str:
        metadata = self._json_loads(metadata_json)
        if not isinstance(metadata, dict):
            return "unknown"
        profile_source = str(metadata.get("profile_source") or "").strip().lower()
        return profile_source if profile_source in PROFILE_SOURCES else "unknown"

    def _breakdown(self, rows: List[DecisionSignalOutcomeRecord], dimension: str) -> List[Dict[str, Any]]:
        grouped: Dict[str, List[DecisionSignalOutcomeRecord]] = defaultdict(list)
        for row in rows:
            value = getattr(row, dimension, None)
            key = str(value or "unknown")
            grouped[key].append(row)
        buckets = [
            {
                "dimension": dimension,
                "value": value,
                **self._aggregate(bucket_rows),
            }
            for value, bucket_rows in grouped.items()
        ]
        return sorted(buckets, key=lambda item: (-int(item["total"]), str(item["value"])))

    @staticmethod
    def _aggregate(rows: List[DecisionSignalOutcomeRecord]) -> Dict[str, Any]:
        total = len(rows)
        completed = [row for row in rows if row.eval_status == "completed"]
        unable = [row for row in rows if row.eval_status == "unable"]
        hit = sum(1 for row in completed if row.outcome == "hit")
        miss = sum(1 for row in completed if row.outcome == "miss")
        neutral = sum(1 for row in completed if row.outcome == "neutral")
        denominator = hit + miss
        returns = [
            float(row.stock_return_pct)
            for row in completed
            if row.stock_return_pct is not None
        ]
        unable_reasons = Counter(row.unable_reason or "unknown" for row in unable)
        return {
            "total": total,
            "completed": len(completed),
            "unable": len(unable),
            "hit": hit,
            "miss": miss,
            "neutral": neutral,
            "hit_rate_pct": round(hit / denominator * 100, 2) if denominator else None,
            "avg_stock_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
            "unable_reasons": dict(sorted(unable_reasons.items())),
        }
