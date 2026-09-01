# -*- coding: utf-8 -*-
"""Service layer for persisted DecisionSignal assets."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple, get_args

from data_provider.base import canonical_stock_code, normalize_stock_code
from src.core.trading_calendar import MarketPhase
from src.repositories.decision_signal_repo import (
    DecisionSignalCreateResult,
    DecisionSignalRepository,
)
from src.repositories.portfolio_repo import PortfolioRepository
from src.report_language import normalize_report_language
from src.schemas.decision_action import (
    DecisionAction,
    build_action_fields,
    localize_action_label,
    normalize_decision_action,
)
from src.schemas.decision_profile import (
    DecisionProfileFilter,
    VALID_DECISION_PROFILES,
    extract_legacy_decision_profile,
    normalize_decision_profile,
    normalize_decision_profile_filter,
)
from src.schemas.decision_scale import action_for_score, score_action_conflicts_without_guardrail
from src.services.decision_signal_level_validator import validate_levels
from src.services.decision_signal_level_validator import MIN_REWARD_RISK
from src.services.horizon_policy import (
    minimum_viable_horizon,
    noise_safe_stop_pct,
    reachable_target_pct,
)
from src.services.portfolio_service import VALID_MARKETS
from src.storage import (
    AnalysisHistory,
    DatabaseManager,
    DecisionSignalRecord,
    to_utc_naive_datetime,
    utc_naive_now,
)
from src.utils.data_processing import parse_json_field
from src.utils.sanitize import sanitize_decision_signal_payload, sanitize_decision_signal_text


SOURCE_TYPES = frozenset({"analysis", "agent", "alert", "market_review", "manual"})
SIGNAL_STATUSES = frozenset({"active", "expired", "invalidated", "closed", "archived"})
PLAN_QUALITIES = frozenset({"complete", "partial", "minimal", "unknown"})
HORIZONS = frozenset({"intraday", "1d", "3d", "5d", "10d", "swing", "long"})
MARKET_PHASES = frozenset(phase.value for phase in MarketPhase)
DECISION_ACTIONS = frozenset(get_args(DecisionAction))
REDACTION_MARKERS = ("[REDACTED]", "[REDACTED_URL]")
TERMINAL_STATUSES = frozenset({"expired", "invalidated", "closed", "archived"})
BULLISH_ACTIONS = frozenset({"buy", "add"})
DEFENSIVE_ACTIONS = frozenset({"reduce", "sell", "avoid"})
HORIZON_ORDER = {"intraday": 1, "1d": 1, "3d": 3, "5d": 5, "10d": 10}

INTRADAY_PHASES = frozenset({
    MarketPhase.PREMARKET.value,
    MarketPhase.INTRADAY.value,
    MarketPhase.LUNCH_BREAK.value,
    MarketPhase.CLOSING_AUCTION.value,
})
DEFAULT_INTRADAY_TTL_HOURS = {
    "cn": 4.0,
    "hk": 5.5,
    "us": 6.5,
}

logger = logging.getLogger(__name__)


class DecisionSignalNotFoundError(ValueError):
    """Raised when a requested decision signal does not exist."""


class DecisionSignalStorageError(RuntimeError):
    """Raised when persisted decision-signal data is internally inconsistent."""


DecisionSignalWriteDisposition = Literal["created", "existing", "refreshed"]


@dataclass(frozen=True)
class DecisionSignalWriteOutcome:
    """Typed internal result for the single DecisionSignal write path."""

    item: Dict[str, Any]
    created: bool
    refreshed: bool
    duplicate: bool

    def __post_init__(self) -> None:
        if sum((self.created, self.refreshed, self.duplicate)) != 1:
            raise DecisionSignalStorageError("invalid DecisionSignal write outcome")

    @property
    def disposition(self) -> DecisionSignalWriteDisposition:
        if self.created:
            return "created"
        if self.refreshed:
            return "refreshed"
        if self.duplicate:
            return "existing"
        raise DecisionSignalStorageError("DecisionSignal write outcome has no disposition")


class DecisionSignalService:
    """Business logic for DecisionSignal storage, querying, and serialization."""

    def __init__(
        self,
        repo: Optional[DecisionSignalRepository] = None,
        portfolio_repo: Optional[PortfolioRepository] = None,
        db_manager: Optional[DatabaseManager] = None,
    ):
        self.repo = repo or DecisionSignalRepository(db_manager)
        self.portfolio_repo = portfolio_repo or PortfolioRepository(db_manager)
        self.db = db_manager or getattr(self.repo, "db", None) or DatabaseManager.get_instance()

    def create_signal(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        outcome = self.create_signal_with_outcome(payload)
        return {"item": outcome.item, "created": outcome.created}

    def create_signal_with_outcome(self, payload: Dict[str, Any]) -> DecisionSignalWriteOutcome:
        """Create through the canonical path while preserving repository disposition."""

        result = self._store_signal(payload)
        # Active duplicates can be retries after a prior partial create; rerun invalidation to repair old opposing signals.
        if result.row.status == "active":
            self._invalidate_opposing_active_signals(
                result.row,
                reference_at=result.invalidation_reference_at,
            )
        return self._write_outcome(result)

    def create_history_bound_signal_with_outcome(
        self,
        payload: Dict[str, Any],
        *,
        history_created_at: Optional[datetime],
        market_phase_summary: Any = None,
    ) -> DecisionSignalWriteOutcome:
        """Persist a report-derived signal on the source report's timeline."""

        history_payload = dict(payload)
        self._apply_history_bound_lifecycle(
            history_payload,
            created_at=history_created_at,
            market_phase_summary=market_phase_summary,
        )
        result = self._store_signal(history_payload)
        if result.row.status == "active":
            if result.row.created_at is None:
                raise DecisionSignalStorageError(
                    "history-bound DecisionSignal has no created_at"
                )
            self._invalidate_opposing_active_signals(
                result.row,
                reference_at=result.row.created_at,
            )
            self._invalidate_history_bound_if_superseded(result.row.id)

        final_row = self.repo.get(result.row.id)
        if final_row is None:
            raise DecisionSignalStorageError(
                f"history-bound DecisionSignal disappeared after write: {result.row.id}"
            )
        return self._write_outcome(result, row=final_row)

    def _store_signal(self, payload: Dict[str, Any]) -> DecisionSignalCreateResult:
        fields, lifecycle = self._normalize_payload(payload)
        return self.repo.create_if_absent(
            fields,
            allow_relaxed_horizon_fill=lifecycle["horizon_defaulted"],
        )

    def _write_outcome(
        self,
        result: DecisionSignalCreateResult,
        *,
        row: Optional[DecisionSignalRecord] = None,
    ) -> DecisionSignalWriteOutcome:
        return DecisionSignalWriteOutcome(
            item=self._serialize(row if row is not None else result.row),
            created=result.created,
            refreshed=result.refreshed,
            duplicate=result.duplicate,
        )

    def get_signal(self, signal_id: int) -> Dict[str, Any]:
        row = self.repo.get(signal_id)
        if row is None:
            raise DecisionSignalNotFoundError(f"Decision signal not found: {signal_id}")
        return self._serialize(row)

    def list_signals(
        self,
        *,
        stock_code: Optional[str] = None,
        market: Optional[str] = None,
        action: Optional[str] = None,
        market_phase: Optional[str] = None,
        decision_profile: Optional[Any] = None,
        source_type: Optional[str] = None,
        source_report_id: Optional[Any] = None,
        trace_id: Optional[str] = None,
        trigger_source: Optional[str] = None,
        status: Optional[str] = None,
        created_from: Optional[Any] = None,
        created_to: Optional[Any] = None,
        expires_from: Optional[Any] = None,
        expires_to: Optional[Any] = None,
        holding_only: bool = False,
        account_id: Optional[int] = None,
        stock_identities: Optional[List[Tuple[str, str]]] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        safe_page = max(1, int(page))
        safe_page_size = max(1, min(int(page_size), 100))
        market_norm = self._normalize_optional_market(market)
        action_norm = self._normalize_optional_action(action)
        market_phase_norm = self._normalize_optional_enum(market_phase, MARKET_PHASES, "market_phase")
        decision_profile_filter = normalize_decision_profile_filter(decision_profile)
        source_type_norm = self._normalize_optional_enum(source_type, SOURCE_TYPES, "source_type")
        source_report_id_norm = self._optional_int(source_report_id, "source_report_id")
        trace_id_norm = self._optional_identity_text(trace_id, "trace_id", max_length=64)
        status_norm = self._normalize_optional_enum(status, SIGNAL_STATUSES, "status")
        trigger_source_norm = self._normalize_optional_trigger_source(trigger_source)
        created_from_dt = self._parse_datetime(created_from)
        created_to_dt = self._parse_datetime(created_to)
        expires_from_dt = self._parse_datetime(expires_from)
        expires_to_dt = self._parse_datetime(expires_to)
        stock_codes = self._stock_filter_codes(stock_code, market=market_norm)
        stock_identity_filters: Optional[List[Tuple[str, str]]] = None

        if stock_identities is not None:
            # Explicit identities come from a caller-owned snapshot; skip cached holdings entirely.
            requested_codes = set(stock_codes or [])
            normalized_identities: set[Tuple[str, str]] = set()
            for identity_market, identity_code in stock_identities:
                if not str(identity_code or "").strip():
                    continue
                identity_market_norm = self._normalize_market(identity_market)
                if market_norm and identity_market_norm != market_norm:
                    continue
                identity_code_norm = self._normalize_stock_code(identity_code, market=identity_market_norm)
                if requested_codes and identity_code_norm not in requested_codes:
                    continue
                normalized_identities.add((identity_market_norm, identity_code_norm))
            stock_identity_filters = sorted(normalized_identities)
            stock_codes = None
            if not stock_identity_filters:
                return {"items": [], "total": 0, "page": safe_page, "page_size": safe_page_size}
        elif holding_only:
            held_identities = self._cached_holding_identities(account_id=account_id)
            if market_norm:
                held_identities = {
                    identity for identity in held_identities if identity[0] == market_norm
                }
            if stock_codes:
                requested_codes = set(stock_codes)
                held_identities = {
                    identity for identity in held_identities if identity[1] in requested_codes
                }
            stock_identity_filters = sorted(held_identities)
            stock_codes = None
            if not stock_identity_filters:
                return {"items": [], "total": 0, "page": safe_page, "page_size": safe_page_size}

        rows, total = self.repo.list(
            stock_codes=stock_codes,
            stock_identities=stock_identity_filters,
            market=market_norm,
            action=action_norm,
            market_phase=market_phase_norm,
            decision_profile_filter=decision_profile_filter,
            source_type=source_type_norm,
            source_report_id=source_report_id_norm,
            trace_id=trace_id_norm,
            trigger_source=trigger_source_norm,
            status=status_norm,
            created_from=created_from_dt,
            created_to=created_to_dt,
            expires_from=expires_from_dt,
            expires_to=expires_to_dt,
            page=safe_page,
            page_size=safe_page_size,
        )
        if total == 0 and self._should_backfill_history_bound_analysis_signal(
            stock_code=stock_code,
            market=market_norm,
            action=action_norm,
            market_phase=market_phase_norm,
            decision_profile_filter=decision_profile_filter,
            source_type=source_type_norm,
            source_report_id=source_report_id_norm,
            trace_id=trace_id_norm,
            trigger_source=trigger_source_norm,
            status=status_norm,
            created_from=created_from_dt,
            created_to=created_to_dt,
            expires_from=expires_from_dt,
            expires_to=expires_to_dt,
            stock_identities=stock_identity_filters,
            holding_only=holding_only,
        ):
            self._backfill_analysis_signal_from_history(source_report_id_norm)
            rows, total = self.repo.list(
                stock_codes=stock_codes,
                stock_identities=stock_identity_filters,
                market=market_norm,
                action=action_norm,
                market_phase=market_phase_norm,
                decision_profile_filter=decision_profile_filter,
                source_type=source_type_norm,
                source_report_id=source_report_id_norm,
                trace_id=trace_id_norm,
                trigger_source=trigger_source_norm,
                status=status_norm,
                created_from=created_from_dt,
                created_to=created_to_dt,
                expires_from=expires_from_dt,
                expires_to=expires_to_dt,
                page=safe_page,
                page_size=safe_page_size,
            )
        return {
            "items": [self._serialize(row) for row in rows],
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
        }

    def get_latest_active(
        self,
        *,
        stock_code: str,
        market: Optional[str] = None,
        limit: int = 1,
    ) -> Dict[str, Any]:
        market_norm = self._normalize_optional_market(market)
        rows = self.repo.get_latest_active(
            stock_codes=self._stock_filter_codes(stock_code, market=market_norm) or [
                self._normalize_stock_code(stock_code)
            ],
            market=market_norm,
            limit=limit,
        )
        return {
            "items": [self._serialize(row) for row in rows],
            "total": len(rows),
            "page": 1,
            "page_size": max(1, min(int(limit), 100)),
        }

    def update_status(
        self,
        signal_id: int,
        *,
        status: str,
        metadata: Optional[Any] = None,
        replace_metadata: bool = False,
    ) -> Dict[str, Any]:
        status_norm = self._normalize_enum(status, SIGNAL_STATUSES, "status")
        existing = self.repo.get(signal_id)
        if existing is None:
            raise DecisionSignalNotFoundError(f"Decision signal not found: {signal_id}")
        if status_norm == "active" and (
            existing.status in TERMINAL_STATUSES or self._is_expired(existing.expires_at)
        ):
            raise ValueError("terminal decision signal cannot be reactivated through status update")
        metadata_json = None
        if replace_metadata:
            if isinstance(metadata, dict):
                normalized_metadata = dict(metadata)
                if existing.decision_profile is None:
                    normalized_metadata.pop("decision_profile", None)
                else:
                    normalized_metadata = self._synchronize_metadata_decision_profile(
                        normalized_metadata,
                        existing.decision_profile,
                    )
                metadata_json = self._json_dumps(normalized_metadata)
            else:
                metadata_json = self._json_dumps(metadata)
        row = self.repo.update_status(
            signal_id,
            status=status_norm,
            metadata_json=metadata_json,
            replace_metadata=replace_metadata,
        )
        if row is None:
            raise DecisionSignalNotFoundError(f"Decision signal not found: {signal_id}")
        return self._serialize(row)

    @staticmethod
    def _should_backfill_history_bound_analysis_signal(
        *,
        stock_code: Optional[Any],
        market: Optional[str],
        action: Optional[str],
        market_phase: Optional[str],
        decision_profile_filter: DecisionProfileFilter,
        source_type: Optional[str],
        source_report_id: Optional[int],
        trace_id: Optional[str],
        trigger_source: Optional[str],
        status: Optional[str],
        created_from: Optional[datetime],
        created_to: Optional[datetime],
        expires_from: Optional[datetime],
        expires_to: Optional[datetime],
        stock_identities: Optional[List[Tuple[str, str]]],
        holding_only: bool,
    ) -> bool:
        """Only lazy-backfill for the exact report section query used by Web."""

        if source_type != "analysis" or source_report_id is None:
            return False
        if decision_profile_filter.is_unknown:
            return False
        if (
            not decision_profile_filter.is_all
            and decision_profile_filter.profile != "balanced"
        ):
            return False
        return not any(
            value not in (None, "", False)
            for value in (
                stock_code,
                market,
                action,
                market_phase,
                trace_id,
                trigger_source,
                status,
                created_from,
                created_to,
                expires_from,
                expires_to,
                stock_identities,
                holding_only,
            )
        )

    def _backfill_analysis_signal_from_history(self, source_report_id: int) -> None:
        """Best-effort lazy extraction for reports saved before DecisionSignal existed."""

        try:
            record = self.db.get_analysis_history_by_id(source_report_id)
            if record is None or getattr(record, "report_type", None) == "market_review":
                return

            raw_result = parse_json_field(getattr(record, "raw_result", None))
            raw = raw_result if isinstance(raw_result, dict) else {}
            context_snapshot = parse_json_field(getattr(record, "context_snapshot", None))
            if not isinstance(context_snapshot, dict):
                context_snapshot = None
            history_action, history_action_label = self._history_action_fields(
                raw=raw,
                record=record,
            )
            if history_action is None:
                return

            from src.analyzer import AnalysisResult
            from src.services.decision_signal_extractor import build_decision_signal_payload_from_report

            result = AnalysisResult(
                code=getattr(record, "code", "") or "",
                name=getattr(record, "name", None) or raw.get("name") or "",
                sentiment_score=self._history_int(
                    raw.get("sentiment_score"),
                    getattr(record, "sentiment_score", None),
                    default=50,
                ),
                trend_prediction=raw.get("trend_prediction") or getattr(record, "trend_prediction", None) or "",
                operation_advice=raw.get("operation_advice") or getattr(record, "operation_advice", None) or "",
                decision_type=raw.get("decision_type") or "",
                confidence_level=raw.get("confidence_level") or "中",
                report_language=normalize_report_language(raw.get("report_language")),
                action=history_action,
                action_label=history_action_label,
                dashboard=raw.get("dashboard") if isinstance(raw.get("dashboard"), dict) else None,
                analysis_summary=raw.get("analysis_summary") or getattr(record, "analysis_summary", None) or "",
                key_points=raw.get("key_points") or "",
                risk_warning=raw.get("risk_warning") or "",
                buy_reason=raw.get("buy_reason") or "",
                raw_response=raw.get("raw_response"),
                search_performed=bool(raw.get("search_performed", False)),
                data_sources=raw.get("data_sources") or "",
                success=bool(raw.get("success", True)),
                error_message=raw.get("error_message"),
                current_price=self._history_float(raw.get("current_price")),
                change_pct=self._history_float(raw.get("change_pct")),
                model_used=raw.get("model_used"),
                query_id=getattr(record, "query_id", None),
                market_structure_context=(
                    raw.get("market_structure_context")
                    if isinstance(raw.get("market_structure_context"), dict)
                    else None
                ),
            )
            payload = build_decision_signal_payload_from_report(
                result,
                context_snapshot=context_snapshot,
                source_report_id=source_report_id,
                trace_id=str(getattr(record, "query_id", "") or source_report_id),
                query_source="history",
                report_type=str(getattr(record, "report_type", "") or "simple"),
                profile_source="backfill_defaulted",
            )
            if payload is None:
                return
            self.create_history_bound_signal_with_outcome(
                payload,
                history_created_at=getattr(record, "created_at", None),
            )
        except Exception as exc:
            logger.warning(
                "Decision signal lazy backfill failed: source_report_id=%s error=%s",
                source_report_id,
                exc,
                exc_info=True,
            )

    @staticmethod
    def _history_has_decision_source(*, raw: Dict[str, Any], record: AnalysisHistory) -> bool:
        action, _ = DecisionSignalService._history_action_fields(raw=raw, record=record)
        return action is not None

    @staticmethod
    def _history_action_fields(
        *,
        raw: Dict[str, Any],
        record: AnalysisHistory,
    ) -> tuple[Optional[str], Optional[str]]:
        raw_operation_advice = raw.get("operation_advice")
        normalized_operation_advice = str(raw_operation_advice).strip() if raw_operation_advice is not None else None
        if not normalized_operation_advice:
            normalized_operation_advice = getattr(record, "operation_advice", None)
        raw_action = raw.get("action")
        normalized_action = str(raw_action).strip() if raw_action is not None else None
        if not normalized_action:
            normalized_action = None
        score = DecisionSignalService._history_int(
            raw.get("sentiment_score"),
            getattr(record, "sentiment_score", None),
            default=None,
        )
        raw_action_value = normalize_decision_action(normalized_action) or normalize_decision_action(
            normalized_operation_advice
        )
        guardrail_reason = DecisionSignalService._history_guardrail_reason(
            raw=raw,
            operation_advice=normalized_operation_advice,
            score=score,
            raw_action=raw_action_value,
        )
        action_fields = build_action_fields(
            operation_advice=normalized_operation_advice,
            explicit_action=normalized_action,
            report_type=getattr(record, "report_type", ""),
            report_language=raw.get("report_language"),
            sentiment_score=score,
            guardrail_reason=guardrail_reason,
            align_with_score=True,
        )
        return action_fields["action"], action_fields["action_label"]

    @staticmethod
    def _history_guardrail_reason(
        *,
        raw: Dict[str, Any],
        operation_advice: Optional[str],
        score: Optional[int],
        raw_action: Optional[str],
    ) -> Optional[str]:
        dashboard = raw.get("dashboard") if isinstance(raw.get("dashboard"), dict) else {}
        calibration = (
            dashboard.get("decision_score_calibration")
            if isinstance(dashboard.get("decision_score_calibration"), dict)
            else {}
        )
        stability = (
            dashboard.get("decision_stability")
            if isinstance(dashboard.get("decision_stability"), dict)
            else {}
        )
        for candidate in (
            calibration.get("guardrail_reason"),
            stability.get("reason"),
            raw.get("guardrail_reason"),
        ):
            text = str(candidate or "").strip()
            if text:
                return text

        if score_action_conflicts_without_guardrail(score=score, action=raw_action):
            candidates = [operation_advice]
            if action_for_score(score) == "buy":
                candidates.extend(
                    [
                        raw.get("analysis_summary"),
                        raw.get("buy_reason"),
                        raw.get("risk_warning"),
                    ]
                )
            hints = (
                "等待",
                "待",
                "需要确认",
                "缺少确认",
                "未确认",
                "回踩",
                "支撑",
                "压力",
                "风险",
                "资金",
                "突破",
                "不追",
                "不宜",
            )
            for candidate in candidates:
                text = str(candidate or "").strip()
                if not text:
                    continue
                normalized = text.lower()
                if any(hint in normalized for hint in hints):
                    return text
        return None

    def _apply_history_bound_lifecycle(
        self,
        payload: Dict[str, Any],
        *,
        created_at: Optional[datetime],
        market_phase_summary: Any = None,
    ) -> None:
        """Anchor a history-derived signal to the source report time."""

        if not isinstance(created_at, datetime):
            raise ValueError("source report created_at is required for persistence")
        history_created_at = self._coerce_history_created_at_to_utc_naive(created_at)

        payload["_created_at_override"] = history_created_at
        payload["status"] = "active"
        payload.pop("expires_at", None)
        sanitized_phase_summary = self._sanitize_history_market_phase_summary(
            market_phase_summary
        )
        if sanitized_phase_summary:
            raw_metadata = payload.get("metadata")
            if raw_metadata is None:
                metadata: Dict[str, Any] = {}
            elif isinstance(raw_metadata, dict):
                metadata = dict(raw_metadata)
            else:
                raise ValueError("metadata must be an object")
            metadata["market_phase_summary"] = sanitized_phase_summary
            payload["metadata"] = metadata

        horizon = payload.get("horizon") or self._default_horizon(
            action=str(payload.get("action") or ""),
            market_phase=payload.get("market_phase"),
        )
        if horizon:
            payload["horizon"] = horizon

        expires_at = self._history_bound_expires_at(
            created_at=history_created_at,
            horizon=horizon,
            market=str(payload.get("market") or ""),
            metadata=payload.get("metadata"),
        )
        if expires_at is None:
            return
        payload["expires_at"] = expires_at
        if self._is_expired(expires_at):
            payload["status"] = "expired"

    @staticmethod
    def _sanitize_history_market_phase_summary(value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        allowed_fields = (
            "phase",
            "session_date",
            "minutes_to_open",
            "minutes_to_close",
        )
        return {
            field_name: value[field_name]
            for field_name in allowed_fields
            if value.get(field_name) not in (None, "")
        }

    @staticmethod
    def _coerce_history_created_at_to_utc_naive(value: datetime) -> datetime:
        if value.tzinfo is not None:
            return to_utc_naive_datetime(value)

        local_tz = datetime.now().astimezone().tzinfo
        if local_tz is None or local_tz.utcoffset(value) is None:
            return to_utc_naive_datetime(value)

        try:
            return value.replace(tzinfo=local_tz).astimezone(timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError):
            return to_utc_naive_datetime(value)

    def _invalidate_history_bound_if_superseded(self, signal_id: int) -> None:
        row = self.repo.get(signal_id)
        if row is None or row.status != "active":
            return

        opposing_actions = self._opposing_actions(row.action)
        if not opposing_actions:
            return
        newer_rows = self.repo.list_active_by_stock_actions(
            market=row.market,
            stock_code=row.stock_code,
            actions=sorted(opposing_actions),
            decision_profile=row.decision_profile,
            exclude_signal_id=row.id,
        )
        for newer_row in newer_rows:
            if not self._is_prior_signal(row, newer_row, reference_at=newer_row.created_at):
                continue
            metadata_json = self._invalidation_metadata_json(row, invalidated_by=newer_row)
            updated = self.repo.update_status(
                row.id,
                status="invalidated",
                metadata_json=metadata_json,
                replace_metadata=True,
            )
            if updated is None:
                logger.warning(
                    "Decision signal disappeared before history-bound invalidation: "
                    "signal_id=%s invalidated_by=%s",
                    row.id,
                    newer_row.id,
                )
            return

    @classmethod
    def _history_bound_expires_at(
        cls,
        *,
        created_at: datetime,
        horizon: Optional[str],
        market: str,
        metadata: Any,
    ) -> Optional[datetime]:
        base = to_utc_naive_datetime(created_at)
        return cls._expires_at_from_base(
            horizon=horizon,
            market=market,
            metadata=metadata,
            base=base,
        )

    @staticmethod
    def _history_int(*values: Any, default: int) -> int:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
        return default

    @staticmethod
    def _history_float(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def _normalize_payload(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        market = self._normalize_market(payload.get("market"))
        stock_code = self._normalize_stock_code(payload.get("stock_code"), market=market)
        action = self._normalize_action(payload.get("action"))
        report_language = self._resolve_report_language(payload.get("report_language"))
        action_label = self._optional_public_text(payload.get("action_label"), "action_label", max_length=32)
        if not action_label:
            action_label = localize_action_label(action, report_language)

        raw_metadata = payload.get("metadata")
        if raw_metadata is None:
            metadata: Dict[str, Any] = {}
        elif isinstance(raw_metadata, dict):
            metadata = dict(raw_metadata)
        else:
            raise ValueError("metadata must be an object")

        if "decision_profile" in payload:
            decision_profile = normalize_decision_profile(payload.get("decision_profile"))
            if decision_profile is None:
                allowed = ", ".join(VALID_DECISION_PROFILES)
                raise ValueError(f"decision_profile must be one of: {allowed}")
        else:
            decision_profile = extract_legacy_decision_profile(metadata) or "balanced"
        metadata = self._synchronize_metadata_decision_profile(metadata, decision_profile)

        confidence = self._optional_float(payload.get("confidence"), "confidence")
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        score = self._optional_int(payload.get("score"), "score")
        if score is not None and not 0 <= score <= 100:
            raise ValueError("score must be between 0 and 100")

        market_phase = self._normalize_optional_enum(payload.get("market_phase"), MARKET_PHASES, "market_phase")
        horizon_explicit = self._payload_has_value(payload, "horizon")
        horizon = self._normalize_optional_enum(payload.get("horizon"), HORIZONS, "horizon")
        horizon_defaulted = False
        if horizon is None:
            horizon = self._default_horizon(action=action, market_phase=market_phase)
            horizon_defaulted = horizon is not None and not horizon_explicit
        expires_explicit = self._payload_has_value(payload, "expires_at")
        expires_at = self._parse_datetime(payload.get("expires_at"))
        if expires_at is None and not expires_explicit:
            expires_at = self._default_expires_at(
                horizon=horizon,
                market=market,
                metadata=metadata,
            )
        created_at = self._parse_datetime(payload.get("_created_at_override"))

        fields: Dict[str, Any] = {
            "stock_code": stock_code,
            "stock_name": self._optional_public_text(payload.get("stock_name"), "stock_name", max_length=64),
            "market": market,
            "source_type": self._normalize_enum(payload.get("source_type"), SOURCE_TYPES, "source_type"),
            "source_agent": self._optional_public_text(payload.get("source_agent"), "source_agent", max_length=64),
            "source_report_id": self._optional_int(payload.get("source_report_id"), "source_report_id"),
            "trace_id": self._optional_identity_text(payload.get("trace_id"), "trace_id", max_length=64),
            "decision_profile": decision_profile,
            "market_phase": market_phase,
            "trigger_source": self._normalize_trigger_source(payload.get("trigger_source")),
            "action": action,
            "action_label": action_label,
            "confidence": confidence,
            "score": score,
            "horizon": horizon,
            "entry_low": self._optional_price_float(payload.get("entry_low"), "entry_low"),
            "entry_high": self._optional_price_float(payload.get("entry_high"), "entry_high"),
            "stop_loss": self._optional_price_float(payload.get("stop_loss"), "stop_loss"),
            "target_price": self._optional_price_float(payload.get("target_price"), "target_price"),
            "invalidation": self._optional_signal_text(payload.get("invalidation")),
            "watch_conditions": self._optional_signal_text(payload.get("watch_conditions")),
            "reason": self._optional_signal_text(payload.get("reason")),
            "risk_summary": self._optional_signal_text(payload.get("risk_summary")),
            "catalyst_summary": self._optional_signal_text(payload.get("catalyst_summary")),
            "evidence_json": self._json_dumps(payload.get("evidence")),
            "data_quality_summary_json": self._json_dumps(payload.get("data_quality_summary")),
            "status": self._normalize_optional_enum(payload.get("status"), SIGNAL_STATUSES, "status") or "active",
            "expires_at": expires_at,
            "metadata_json": self._json_dumps(metadata),
        }
        if created_at is not None:
            fields["created_at"] = created_at
        if fields["status"] == "active" and self._is_expired(fields["expires_at"]):
            fields["status"] = "expired"
        self._validate_entry_range(fields)
        # horizon 与目标价必须先按标的波动校正，plan_quality 才是对"校正后的
        # 计划"的判断，而不是对一份不可执行的原始计划的判断。
        policy_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        self._apply_horizon_policy(fields, policy_metadata)
        if policy_metadata:
            fields["metadata_json"] = self._json_dumps(policy_metadata)
        fields["plan_quality"] = self._normalize_plan_quality(
            payload.get("plan_quality"),
            fields=fields,
        )
        return fields, {"horizon_defaulted": horizon_defaulted}

    @staticmethod
    def _payload_has_value(payload: Dict[str, Any], field_name: str) -> bool:
        return payload.get(field_name) not in (None, "")

    @staticmethod
    def _default_horizon(*, action: str, market_phase: Optional[str]) -> str:
        if action == "alert" or market_phase in INTRADAY_PHASES:
            return "intraday"
        return "3d"

    def _instrument_stats(self, stock_code: Any) -> tuple[Optional[float], Optional[float]]:
        """Recent average daily range % and average close for a code."""
        code = str(stock_code or "").strip()
        if not code:
            return None, None
        cache = getattr(self, "_instrument_stats_cache", None)
        if cache is None:
            cache = self._instrument_stats_cache = {}
        if code in cache:
            return cache[code]
        adr = price = None
        try:
            from src.storage import StockDaily

            with self.db.get_session() as session:
                rows = (
                    session.query(
                        StockDaily.high, StockDaily.low, StockDaily.open, StockDaily.close
                    )
                    .filter(StockDaily.code == code)
                    .order_by(StockDaily.date.desc())
                    .limit(60)
                    .all()
                )
            ranges = [
                (float(h) - float(low)) / float(o) * 100
                for h, low, o, _ in rows
                if o and float(o) > 0 and h is not None and low is not None
            ]
            closes = [float(c) for *_, c in rows if c]
            if ranges:
                adr = sum(ranges) / len(ranges)
            if closes:
                price = sum(closes) / len(closes)
        except Exception:
            logger.debug("[DecisionSignal] instrument stats lookup failed for %s", code, exc_info=True)
        cache[code] = (adr, price)
        return adr, price

    def _apply_horizon_policy(self, fields: Dict[str, Any], metadata: Dict[str, Any]) -> None:
        """Set a viable horizon and keep the target inside what it can deliver.

        方向性信号的 horizon 此前只看 market_phase：盘中阶段一律 intraday，
        与标的本身的波动无关。目标价则直接来自 LLM 输出（sniper points），
        既不参考波动也不参考 horizon —— 于是出现了"日内 +11.32%"这类目标，
        实测超出可达幅度 14.7 倍。这样的信号永远不会触及止损或目标，也就
        永远无法作为交易被记分。

        这里做两件事，都不静默丢信息：
          1. 按标的实测波动把 horizon 提升到最短可行值；
          2. 把超出可达范围的目标夹到可达值，并把原值记入 metadata。
        原始 LLM 目标全部保留，日后可以回头衡量模型的目标是否强于公式。
        """
        action = str(fields.get("action") or "").strip().lower()
        if action in ("watch", "alert"):
            return

        adr, price = self._instrument_stats(fields.get("stock_code"))
        if not adr or not price:
            return

        viable = minimum_viable_horizon(adr, price)
        original_horizon = fields.get("horizon")
        if viable is None:
            metadata.setdefault("horizon_policy", {})["out_of_universe"] = True
            metadata["horizon_policy"]["reason"] = (
                "no horizon up to 10d supports the required net reward-to-risk"
            )
            return

        if HORIZON_ORDER.get(original_horizon, 0) < HORIZON_ORDER.get(viable, 0):
            fields["horizon"] = viable
            metadata.setdefault("horizon_policy", {})["original_horizon"] = original_horizon
            metadata["horizon_policy"]["horizon"] = viable
            logger.info(
                "[DecisionSignal] %s horizon %s -> %s (ADR %.2f%%)",
                fields.get("stock_code"), original_horizon, viable, adr,
            )

        entry_low = fields.get("entry_low")
        entry_high = fields.get("entry_high")
        entry = None
        if entry_low is not None and entry_high is not None:
            entry = (float(entry_low) + float(entry_high)) / 2
        elif entry_low is not None:
            entry = float(entry_low)
        target = fields.get("target_price")
        if entry is None or not target or entry <= 0:
            return

        cap_pct = reachable_target_pct(adr, fields.get("horizon"))
        if cap_pct is None:
            return

        distance_pct = abs(float(target) - entry) / entry * 100
        if distance_pct <= cap_pct:
            return

        bullish = action in ("buy", "add")
        capped = entry * (1 + cap_pct / 100) if bullish else entry * (1 - cap_pct / 100)
        policy = metadata.setdefault("horizon_policy", {})
        policy["original_target_price"] = float(target)
        policy["original_target_distance_pct"] = round(distance_pct, 4)
        policy["target_capped_to_pct"] = round(cap_pct, 4)
        fields["target_price"] = round(capped, 2)
        logger.info(
            "[DecisionSignal] %s target %.2f -> %.2f (%.2f%% exceeded reachable %.2f%% over %s)",
            fields.get("stock_code"), float(target), fields["target_price"],
            distance_pct, cap_pct, fields.get("horizon"),
        )

        # 只夹目标而不管止损会让计划更糟：目标被拉近之后，原本的宽止损
        # 会把 R:R 压到门槛之下，信号照样不可执行。夹了目标就必须重新
        # 推导止损，否则这一步没有让任何信号变得可交易。
        stop = fields.get("stop_loss")
        if stop is None:
            return
        stop_f = float(stop)
        risk_pct = abs(entry - stop_f) / entry * 100
        floor_pct = noise_safe_stop_pct(adr, price)
        # 留 5% 余量：止损要按分位取整，正好卡在门槛上会被四舍五入推到门槛之下。
        needed_risk_pct = cap_pct / (MIN_REWARD_RISK * 1.05)
        if risk_pct <= needed_risk_pct:
            return

        # 收紧到刚好满足 R:R，但绝不紧于噪声下限 —— 比下限更紧的止损
        # 是在收割噪声，不是风控。
        new_risk_pct = max(needed_risk_pct, floor_pct)
        if new_risk_pct >= risk_pct:
            policy["stop_left_as_is"] = (
                "noise floor prevents tightening enough to reach the required reward-to-risk"
            )
            return
        new_stop = (
            entry * (1 - new_risk_pct / 100) if bullish else entry * (1 + new_risk_pct / 100)
        )
        policy["original_stop_loss"] = stop_f
        policy["original_stop_distance_pct"] = round(risk_pct, 4)
        policy["stop_tightened_to_pct"] = round(new_risk_pct, 4)
        fields["stop_loss"] = round(new_stop, 2)
        logger.info(
            "[DecisionSignal] %s stop %.2f -> %.2f to preserve reward-to-risk after target cap",
            fields.get("stock_code"), stop_f, fields["stop_loss"],
        )

    @classmethod
    def _default_expires_at(
        cls,
        *,
        horizon: Optional[str],
        market: str,
        metadata: Any,
    ) -> Optional[datetime]:
        return cls._expires_at_from_base(
            horizon=horizon,
            market=market,
            metadata=metadata,
            base=utc_naive_now(),
        )

    @classmethod
    def _expires_at_from_base(
        cls,
        *,
        horizon: Optional[str],
        market: str,
        metadata: Any,
        base: datetime,
    ) -> Optional[datetime]:
        if horizon == "intraday":
            minutes_to_close = cls._metadata_minutes(metadata, "minutes_to_close")
            if minutes_to_close is not None:
                return base + timedelta(minutes=minutes_to_close)
            minutes_to_open = cls._metadata_minutes(metadata, "minutes_to_open")
            if minutes_to_open is not None:
                fallback_minutes = int(cls._intraday_fallback_hours(market) * 60)
                return base + timedelta(minutes=minutes_to_open + fallback_minutes)
            return base + timedelta(hours=cls._intraday_fallback_hours(market))

        days = cls._horizon_days(horizon)
        if days is None:
            return None
        return base + timedelta(days=days)

    @staticmethod
    def _intraday_fallback_hours(market: str) -> float:
        return DEFAULT_INTRADAY_TTL_HOURS.get(market, 4.0)

    @staticmethod
    def _horizon_days(horizon: Optional[str]) -> Optional[int]:
        if horizon in {"1d", "3d", "5d", "10d"}:
            return int(horizon[:-1])
        return None

    @classmethod
    def _metadata_minutes(cls, metadata: Any, field_name: str) -> Optional[int]:
        if not isinstance(metadata, dict):
            return None
        summary = metadata.get("market_phase_summary")
        if not isinstance(summary, dict):
            return None
        value = summary.get(field_name)
        if value in (None, ""):
            return None
        try:
            minutes = int(float(value))
        except (TypeError, ValueError):
            return None
        return minutes if minutes >= 0 else None

    def _invalidate_opposing_active_signals(
        self,
        row: DecisionSignalRecord,
        *,
        reference_at: Optional[datetime],
    ) -> None:
        opposing_actions = self._opposing_actions(row.action)
        if not opposing_actions:
            return
        old_rows = self.repo.list_active_by_stock_actions(
            market=row.market,
            stock_code=row.stock_code,
            actions=sorted(opposing_actions),
            decision_profile=row.decision_profile,
            exclude_signal_id=row.id,
        )
        for old_row in old_rows:
            if not self._is_prior_signal(old_row, row, reference_at=reference_at):
                continue
            metadata_json = self._invalidation_metadata_json(old_row, invalidated_by=row)
            updated = self.repo.update_status(
                old_row.id,
                status="invalidated",
                metadata_json=metadata_json,
                replace_metadata=True,
            )
            if updated is None:
                logger.warning(
                    "Decision signal disappeared before invalidation: signal_id=%s invalidated_by=%s",
                    old_row.id,
                    row.id,
                )

    @staticmethod
    def _is_prior_signal(
        candidate: DecisionSignalRecord,
        current: DecisionSignalRecord,
        *,
        reference_at: Optional[datetime],
    ) -> bool:
        candidate_created_at = candidate.created_at
        if candidate_created_at is not None and reference_at is not None:
            candidate_created_at = to_utc_naive_datetime(candidate_created_at)
            reference_at = to_utc_naive_datetime(reference_at)
            if candidate_created_at != reference_at:
                return candidate_created_at < reference_at

        if candidate.id is not None and current.id is not None:
            return candidate.id < current.id
        return False

    @staticmethod
    def _opposing_actions(action: str) -> frozenset[str]:
        if action in BULLISH_ACTIONS:
            return DEFENSIVE_ACTIONS
        if action in DEFENSIVE_ACTIONS:
            return BULLISH_ACTIONS
        return frozenset()

    def _invalidation_metadata_json(
        self,
        row: DecisionSignalRecord,
        *,
        invalidated_by: DecisionSignalRecord,
    ) -> Optional[str]:
        metadata = self._metadata_for_invalidation(row)
        metadata.update({
            "invalidated_by_signal_id": invalidated_by.id,
            "invalidated_reason": f"opposite_active_signal:{row.action}->{invalidated_by.action}",
            "invalidated_at": utc_naive_now().isoformat(),
            "previous_status": row.status,
        })
        if row.decision_profile is not None:
            metadata = self._synchronize_metadata_decision_profile(
                metadata,
                row.decision_profile,
            )
        return self._json_dumps(metadata)

    @staticmethod
    def _synchronize_metadata_decision_profile(
        metadata: Dict[str, Any],
        decision_profile: str,
    ) -> Dict[str, Any]:
        normalized = dict(metadata)
        normalized["decision_profile"] = decision_profile
        return normalized

    @staticmethod
    def _metadata_for_invalidation(row: DecisionSignalRecord) -> Dict[str, Any]:
        if not row.metadata_json:
            return {}
        try:
            value = json.loads(row.metadata_json)
        except (TypeError, ValueError, RecursionError) as exc:
            logger.warning(
                "Replacing invalid decision signal metadata during invalidation: "
                "id=%s error_type=%s",
                row.id,
                type(exc).__name__,
            )
            return {"metadata_replaced_due_to_invalid_json": True}
        if isinstance(value, dict):
            return dict(value)
        return {"metadata_replaced_due_to_non_object": True}

    @staticmethod
    def _resolve_report_language(value: Any) -> str:
        """Resolve the language a signal's display label is written in.

        ``normalize_report_language(None)`` answers ``zh``, which is the right default for
        a library function and the wrong one here: a payload that carries no language is
        not asking for Chinese, it simply did not say. Signals written by the scheduled
        path do not carry one, so on a deployment configured ``REPORT_LANGUAGE=en`` they
        were labelled ``买入`` while every CLI-written signal in the same run said ``Buy``.
        Two such rows are in the database, a fortnight apart (`id=1`, `id=74`), and both
        were read as a first-day artefact rather than a live defect.

        An unspecified language now means "whatever this deployment is configured to
        emit". A deployment that has not configured one still gets ``zh``, so this changes
        nothing for Chinese installations.
        """
        if str(value or "").strip():
            return normalize_report_language(value)
        try:
            from src.config import get_config

            configured = getattr(get_config(), "report_language", None)
        except Exception:  # noqa: BLE001 - a config problem must not fail the write
            configured = None
        return normalize_report_language(configured)

    def _normalize_plan_quality(self, value: Any, *, fields: Dict[str, Any]) -> str:
        claimed = (
            self._normalize_enum(value, PLAN_QUALITIES, "plan_quality")
            if value is not None
            else self._plan_quality_from_slots(fields)
        )
        # 声明是声明，能否成交是另一回事。此前本方法只数"填了几个槽位"，
        # 一条 entry/stop/target/invalidation 齐全但几何写反、或目标在自身
        # horizon 内根本不可达的信号，同样会被标成 complete —— 该字段因此
        # 长期在说谎（2026-08-31：42 条自称 complete，其中方向性信号 12 条里
        # 有 9 条实际不可执行）。调用方显式传入的值同样要被校验：主张不能
        # 凌驾于测量之上。
        return self._downgrade_plan_quality_if_unexecutable(claimed, fields=fields)

    @staticmethod
    def _plan_quality_from_slots(fields: Dict[str, Any]) -> str:
        has_action_or_reason = bool(fields.get("action") or fields.get("reason"))
        if not has_action_or_reason:
            return "unknown"
        slots = 0
        if fields.get("entry_low") is not None or fields.get("entry_high") is not None:
            slots += 1
        for key in ("stop_loss", "target_price", "invalidation", "watch_conditions"):
            if fields.get(key) not in (None, ""):
                slots += 1
        if slots >= 4:
            return "complete"
        if slots >= 2:
            return "partial"
        return "minimal"

    def _downgrade_plan_quality_if_unexecutable(
        self,
        claimed: str,
        *,
        fields: Dict[str, Any],
    ) -> str:
        """Never let a signal claim a quality its levels do not support."""
        if claimed in ("minimal", "unknown"):
            return claimed

        entry_low = fields.get("entry_low")
        entry_high = fields.get("entry_high")
        if entry_low is not None and entry_high is not None:
            entry = (float(entry_low) + float(entry_high)) / 2
        else:
            entry = entry_low if entry_low is not None else entry_high

        try:
            result = validate_levels(
                action=fields.get("action"),
                entry=entry,
                stop_loss=fields.get("stop_loss"),
                target_price=fields.get("target_price"),
                horizon=fields.get("horizon"),
                average_daily_range_pct=self._average_daily_range_pct(
                    fields.get("stock_code")
                ),
            )
        except Exception:  # 校验本身不得阻断写入
            logger.warning(
                "[DecisionSignal] level validation failed to run for %s",
                fields.get("stock_code"),
                exc_info=True,
            )
            return claimed

        if result.ok:
            return claimed

        logger.info(
            "[DecisionSignal] %s %s levels not executable (%s) - plan_quality %s -> partial",
            fields.get("stock_code"),
            fields.get("action"),
            ", ".join(result.issues),
            claimed,
        )
        return "partial"

    def _average_daily_range_pct(self, stock_code: Any) -> Optional[float]:
        """Recent average (high-low)/open for the code, or None when unavailable.

        Only used for the target-reachability check; returning None skips it and
        leaves the completeness and geometry checks in force.
        """
        code = str(stock_code or "").strip()
        if not code:
            return None
        cache = getattr(self, "_adr_cache", None)
        if cache is None:
            cache = self._adr_cache = {}
        if code in cache:
            return cache[code]
        value: Optional[float] = None
        try:
            from src.storage import StockDaily

            with self.db.get_session() as session:
                rows = (
                    session.query(StockDaily.high, StockDaily.low, StockDaily.open)
                    .filter(StockDaily.code == code)
                    .order_by(StockDaily.date.desc())
                    .limit(60)
                    .all()
                )
            ranges = [
                (float(h) - float(low)) / float(o) * 100
                for h, low, o in rows
                if o and float(o) > 0 and h is not None and low is not None
            ]
            if ranges:
                value = sum(ranges) / len(ranges)
        except Exception:
            logger.debug("[DecisionSignal] ADR lookup failed for %s", code, exc_info=True)
        cache[code] = value
        return value

    def _cached_holding_identities(self, *, account_id: Optional[int]) -> set[Tuple[str, str]]:
        identities = self.portfolio_repo.list_cached_position_identities(account_id=account_id)
        normalized: set[Tuple[str, str]] = set()
        for market, symbol in identities:
            if not str(symbol or "").strip():
                continue
            market_norm = self._normalize_market(market)
            normalized.add((market_norm, self._normalize_stock_code(symbol, market=market_norm)))
        return normalized

    @classmethod
    def _stock_filter_codes(
        cls,
        stock_code: Optional[str],
        *,
        market: Optional[str] = None,
    ) -> Optional[List[str]]:
        if not stock_code:
            return None
        normalized = cls._normalize_stock_code(stock_code, market=market)
        if market is not None:
            return [normalized]

        hk_normalized = cls._normalize_hk_stock_code(str(stock_code).strip())
        return list(dict.fromkeys([normalized, hk_normalized]))

    @classmethod
    def normalize_stock_code_for_signal(cls, value: Any, *, market: Optional[str] = None) -> str:
        """Normalize a stock code for DecisionSignal identity matching."""

        return cls._normalize_stock_code(value, market=market)

    @classmethod
    def _normalize_stock_code(cls, value: Any, *, market: Optional[str] = None) -> str:
        raw = str(value or "").strip()
        if market == "us":
            code = canonical_stock_code(raw)
        elif market == "hk":
            code = cls._normalize_hk_stock_code(raw)
        else:
            code = canonical_stock_code(normalize_stock_code(raw))
        if not code:
            raise ValueError("stock_code is required")
        return code

    @staticmethod
    def _normalize_hk_stock_code(value: str) -> str:
        normalized = canonical_stock_code(normalize_stock_code(value))
        digits = ""
        if normalized.startswith("HK"):
            digits = normalized[2:]
        elif normalized.isdigit():
            digits = normalized
        if digits.isdigit() and 1 <= len(digits) <= 5:
            return f"HK{digits.zfill(5)}"
        return normalized

    @staticmethod
    def _normalize_market(value: Any) -> str:
        market = str(value or "").strip().lower()
        if market not in VALID_MARKETS:
            raise ValueError("market must be one of cn, hk, us, jp, kr, tw, in")
        return market

    @classmethod
    def _normalize_optional_market(cls, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        return cls._normalize_market(value)

    @staticmethod
    def _normalize_action(value: Any) -> str:
        action = str(value or "").strip().lower()
        if not action or action not in DECISION_ACTIONS:
            raise ValueError("action must be one of buy/add/hold/reduce/sell/watch/avoid/alert")
        return action

    @classmethod
    def _normalize_optional_action(cls, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        return cls._normalize_action(value)

    @staticmethod
    def _normalize_enum(value: Any, allowed: frozenset[str], field_name: str) -> str:
        text = str(value or "").strip()
        if text not in allowed:
            allowed_text = ", ".join(sorted(allowed))
            raise ValueError(f"{field_name} must be one of {allowed_text}")
        return text

    @classmethod
    def _normalize_optional_enum(
        cls,
        value: Any,
        allowed: frozenset[str],
        field_name: str,
    ) -> Optional[str]:
        if value in (None, ""):
            return None
        return cls._normalize_enum(value, allowed, field_name)

    @staticmethod
    def _normalize_trigger_source(value: Any) -> str:
        text = DecisionSignalService._public_text(value, "trigger_source", max_length=64, required=True)
        if not text:
            raise ValueError("trigger_source is required")
        return text

    @classmethod
    def _normalize_optional_trigger_source(cls, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        return cls._normalize_trigger_source(value)

    @staticmethod
    def _optional_text(value: Any, field_name: str, *, max_length: int) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if len(text) > max_length:
            raise ValueError(f"{field_name} must be at most {max_length} characters")
        return text

    @classmethod
    def _optional_public_text(cls, value: Any, field_name: str, *, max_length: int) -> Optional[str]:
        return cls._public_text(value, field_name, max_length=max_length, required=False)

    @staticmethod
    def _public_text(value: Any, field_name: str, *, max_length: int, required: bool) -> Optional[str]:
        if value is None:
            if required:
                raise ValueError(f"{field_name} is required")
            return None
        text = sanitize_decision_signal_text(value)
        if not text:
            if required:
                raise ValueError(f"{field_name} is required")
            return None
        if len(text) > max_length:
            raise ValueError(f"{field_name} must be at most {max_length} characters")
        return text

    @classmethod
    def _optional_identity_text(cls, value: Any, field_name: str, *, max_length: int) -> Optional[str]:
        text = cls._optional_text(value, field_name, max_length=max_length)
        if text is None:
            return None
        sanitized = sanitize_decision_signal_text(text)
        if any(marker in sanitized for marker in REDACTION_MARKERS):
            raise ValueError(f"{field_name} must not contain sensitive credentials")
        return text

    @staticmethod
    def _optional_signal_text(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return json.dumps(sanitize_decision_signal_payload(value), ensure_ascii=False, sort_keys=True)
        text = sanitize_decision_signal_text(value)
        return text or None

    @staticmethod
    def _optional_float(value: Any, field_name: str) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a number") from exc

    @classmethod
    def _optional_price_float(cls, value: Any, field_name: str) -> Optional[float]:
        number = cls._optional_float(value, field_name)
        if number is None:
            return None
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{field_name} must be a finite positive number")
        return number

    @staticmethod
    def _validate_entry_range(fields: Dict[str, Any]) -> None:
        entry_low = fields.get("entry_low")
        entry_high = fields.get("entry_high")
        if entry_low is not None and entry_high is not None and entry_low > entry_high:
            raise ValueError("entry_low must be less than or equal to entry_high")

    @staticmethod
    def _optional_int(value: Any, field_name: str) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return to_utc_naive_datetime(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"invalid datetime value: {value}") from exc
            return to_utc_naive_datetime(parsed)
        raise ValueError(f"invalid datetime value: {value}")

    @classmethod
    def _is_expired(cls, expires_at: Optional[datetime]) -> bool:
        normalized_expires_at = cls._parse_datetime(expires_at)
        return normalized_expires_at is not None and normalized_expires_at <= utc_naive_now()

    @staticmethod
    def _json_dumps(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        sanitized = sanitize_decision_signal_payload(value)
        return json.dumps(sanitized, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _json_loads(value: Optional[str], *, signal_id: int, field_name: str) -> Any:
        if not value:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Invalid decision signal JSON: id=%s field=%s error=%s",
                signal_id,
                field_name,
                exc,
            )
            raise DecisionSignalStorageError(
                f"invalid persisted JSON for decision signal {signal_id} field {field_name}"
            ) from exc

    def _serialize(self, row: DecisionSignalRecord) -> Dict[str, Any]:
        return {
            "id": row.id,
            "stock_code": row.stock_code,
            "stock_name": row.stock_name,
            "market": row.market,
            "source_type": row.source_type,
            "source_agent": row.source_agent,
            "source_report_id": row.source_report_id,
            "trace_id": row.trace_id,
            "decision_profile": row.decision_profile,
            "market_phase": row.market_phase,
            "trigger_source": row.trigger_source,
            "action": row.action,
            "action_label": row.action_label,
            "confidence": row.confidence,
            "score": row.score,
            "horizon": row.horizon,
            "entry_low": row.entry_low,
            "entry_high": row.entry_high,
            "stop_loss": row.stop_loss,
            "target_price": row.target_price,
            "invalidation": row.invalidation,
            "watch_conditions": row.watch_conditions,
            "reason": row.reason,
            "risk_summary": row.risk_summary,
            "catalyst_summary": row.catalyst_summary,
            "evidence": self._json_loads(row.evidence_json, signal_id=row.id, field_name="evidence_json"),
            "data_quality_summary": self._json_loads(
                row.data_quality_summary_json,
                signal_id=row.id,
                field_name="data_quality_summary_json",
            ),
            "plan_quality": row.plan_quality,
            "status": row.status,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "metadata": self._json_loads(row.metadata_json, signal_id=row.id, field_name="metadata_json"),
        }
