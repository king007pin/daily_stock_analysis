# -*- coding: utf-8 -*-
"""NSE 官方 bhavcopy 与本地 ``stock_daily`` 的逐日对账服务。

职责边界：
1. 只做“对照 + 记录”，绝不改写已存储的 OHLCV。交易所公布值与本地存量不一致时，
   写入隔离表（quarantine）留证，由人工或后续流程决定如何处理。
2. 只回填 ``delivery_qty`` / ``delivery_pct`` 这两个本地原本缺失的字段，且仅在该
   K 线与 bhavcopy 一致时回填；不一致的 K 线不回填，避免把错误数据坐实。
3. 缺失 bhavcopy（周末、节假日、NSE 故障）是正常结果，不是异常：返回状态说明，
   不抛错，也不臆造任何数据（AGENTS.md 1.3）。

网络访问默认关闭：`BHAVCOPY_RECONCILIATION_ENABLED` 未开启时 ``reconcile()`` 直接
返回 ``disabled``，不触发抓取，也不访问数据库，从而保证离线测试始终离线
（与 ``decision_outcome_daily_refill_enabled`` 的处理方式一致）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from src.services.market_symbol_utils import split_suffix_symbol

logger = logging.getLogger(__name__)

# bhavcopy 只覆盖 NSE，本地代码形如 ``RELIANCE.NS``，bhavcopy 键形如 ``RELIANCE``。
NSE_SUFFIX = "NS"

# 成交量是唯一无需复权即可直接比对的字段，但不同数据源在极少数标的上仍有
# 舍入/口径差异，这里给一个很小的相对容差；需要更严格时由调用方显式传入。
DEFAULT_VOLUME_TOLERANCE_PCT = 0.5

STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_UNAVAILABLE = "unavailable"

# 价格比对必须复用 bhavcopy 客户端（agent A）定下的容差口径，不在本模块重复实现。
# 客户端尚未提供该 helper 时，价格比对整体标记为不可用，只比成交量，绝不用自造
# 的容差假装比过——那等于凭空发明一个“价格一致”的结论。
PRICE_CHECK_ENABLED = "enabled"
PRICE_CHECK_UNAVAILABLE = "unavailable"

_PRICE_MATCH_CANDIDATES: Tuple[str, ...] = (
    "prices_match",
    "price_matches",
    "close_matches",
    "is_price_match",
    "price_within_tolerance",
)

REASON_VOLUME_MISMATCH = "volume_mismatch"
REASON_CLOSE_MISMATCH = "close_mismatch"
REASON_STORED_VOLUME_MISSING = "stored_volume_missing"
REASON_STORED_CLOSE_MISSING = "stored_close_missing"

QUARANTINE_SOURCE = "nse_bhavcopy"

try:  # pragma: no cover - 取决于 agent A 的模块是否已落地
    from src.services.nse_bhavcopy_client import BhavcopyUnavailable
except ImportError:  # pragma: no cover - 客户端落地后本分支不再执行
    class BhavcopyUnavailable(Exception):  # type: ignore[no-redef]
        """占位定义：``src/services/nse_bhavcopy_client.py`` 尚未落地时使用。

        契约中该异常由 bhavcopy 客户端定义。客户端一旦落地，上面的 import 成功，
        此处不再生效；调用方应始终从本模块或客户端模块导入同一个类。
        """


@dataclass(frozen=True)
class StoredBar:
    """``stock_daily`` 中一条已存 K 线的对账视图（只读）。"""

    code: str
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    delivery_qty: Optional[float] = None
    delivery_pct: Optional[float] = None


@dataclass(frozen=True)
class QuarantineDraft:
    """一条待写入隔离表的分歧记录。"""

    code: str
    symbol: str
    trade_date: date
    reasons: Tuple[str, ...]
    stored_close: Optional[float] = None
    published_close: Optional[float] = None
    stored_volume: Optional[float] = None
    published_volume: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)


# 隔离表由 agent B 设计，本模块只依赖契约中的类名，不假设具体列名，
# 因此按候选名取第一个真实存在的列写入。
_CODE_COLUMN_CANDIDATES: Tuple[str, ...] = ("code", "stock_code")
_SYMBOL_COLUMN_CANDIDATES: Tuple[str, ...] = ("symbol", "nse_symbol")
_DATE_COLUMN_CANDIDATES: Tuple[str, ...] = ("trade_date", "date", "bar_date")


def _model_column_names(model: Any) -> Set[str]:
    """返回 ORM 模型上可赋值的列属性名集合。"""

    return {attr.key for attr in sa_inspect(model).mapper.column_attrs}


def _pick_column(columns: Set[str], candidates: Sequence[str]) -> Optional[str]:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


class BhavcopyReconciliationStore:
    """对账所需的全部数据库读写，集中在这里，便于测试整体替换。"""

    def __init__(self, db_manager: Any = None):
        self._db_manager = db_manager

    @property
    def db(self) -> Any:
        if self._db_manager is None:
            from src.storage import DatabaseManager

            self._db_manager = DatabaseManager.get_instance()
        return self._db_manager

    @staticmethod
    def _stock_daily_model() -> Any:
        from src.storage import StockDaily

        return StockDaily

    @staticmethod
    def _record_model() -> Any:
        from src import storage

        model = getattr(storage, "BarReconciliationRecord", None)
        if model is None:
            raise RuntimeError(
                "src.storage 未定义 BarReconciliationRecord，隔离记录无法落库；"
                "请先落地存储层改动，再开启 bhavcopy 对账。"
            )
        return model

    def load_bars(
        self,
        trade_date: date,
        codes: Optional[Iterable[str]] = None,
    ) -> Dict[str, StoredBar]:
        """读取指定交易日的已存 K 线，按 ``code``（形如 ``RELIANCE.NS``）索引。"""

        model = self._stock_daily_model()
        columns = _model_column_names(model)
        stmt = select(model).where(model.date == trade_date)
        code_list = [str(code).upper() for code in codes] if codes else None
        if code_list:
            stmt = stmt.where(model.code.in_(code_list))

        bars: Dict[str, StoredBar] = {}
        with self.db.session_scope() as session:
            for row in session.execute(stmt).scalars().all():
                bars[str(row.code).upper()] = StoredBar(
                    code=str(row.code).upper(),
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                    delivery_qty=getattr(row, "delivery_qty", None) if "delivery_qty" in columns else None,
                    delivery_pct=getattr(row, "delivery_pct", None) if "delivery_pct" in columns else None,
                )
        return bars

    def quarantined_codes(self, trade_date: date) -> Set[str]:
        """返回该交易日已写过隔离记录的代码集合，用于保证幂等。"""

        model = self._record_model()
        columns = _model_column_names(model)
        code_column = _pick_column(columns, _CODE_COLUMN_CANDIDATES)
        date_column = _pick_column(columns, _DATE_COLUMN_CANDIDATES)
        if not code_column or not date_column:
            raise RuntimeError(
                "BarReconciliationRecord 缺少可识别的代码列或日期列，无法保证对账幂等；"
                f"当前列：{sorted(columns)}"
            )

        stmt = select(model).where(getattr(model, date_column) == trade_date)
        with self.db.session_scope() as session:
            rows = session.execute(stmt).scalars().all()
            return {str(getattr(row, code_column)).upper() for row in rows}

    def write_quarantine(self, drafts: Sequence[QuarantineDraft]) -> int:
        """写入隔离记录，返回实际写入条数。

        走 ``DatabaseManager.upsert_bar_reconciliation_records`` —— 这是隔离表自带的
        写入 API，按 ``(code, trade_date, field_name, source)`` 幂等。本方法早期版本
        自己拼 session 与列名候选，那既是重复实现，也写不出 ``field_name``（该列
        NOT NULL），集成时直接触发 IntegrityError。

        **一条分歧对应一条记录，粒度是字段而不是 K 线**：同一根 K 线的收盘价与成交量
        各自分歧时会写两条，各自留下本地值与官方值，便于回头统计"供应商在哪个字段上
        错得更多"。
        """
        if not drafts:
            return 0

        records: List[Dict[str, Any]] = []
        for draft in drafts:
            reasons = set(draft.reasons)
            note = ",".join(sorted(reasons)) or None
            if draft.stored_volume is not None or draft.published_volume is not None:
                if any("volume" in reason for reason in reasons):
                    records.append(
                        self._quarantine_record(
                            draft, "volume", draft.stored_volume, draft.published_volume, note
                        )
                    )
            if draft.stored_close is not None or draft.published_close is not None:
                if any("close" in reason or "price" in reason for reason in reasons):
                    records.append(
                        self._quarantine_record(
                            draft, "close", draft.stored_close, draft.published_close, note
                        )
                    )
            if not records or records[-1]["code"] != draft.code:
                # 分歧原因未落到已知字段时仍要留证，不能静默丢弃。
                records.append(
                    self._quarantine_record(draft, "unspecified", None, None, note)
                )

        result = self.db.upsert_bar_reconciliation_records(records)
        return int(result.get("inserted", 0)) + int(result.get("updated", 0))

    @staticmethod
    def _quarantine_record(
        draft: QuarantineDraft,
        field_name: str,
        stored_value: Optional[float],
        official_value: Optional[float],
        note: Optional[str],
    ) -> Dict[str, Any]:
        abs_diff = diff_pct = None
        if stored_value is not None and official_value:
            abs_diff = abs(float(stored_value) - float(official_value))
            diff_pct = abs_diff / abs(float(official_value)) * 100.0
        return {
            "code": draft.code,
            "source_symbol": draft.symbol,
            "trade_date": draft.trade_date,
            "field_name": field_name,
            "stored_value": stored_value,
            "official_value": official_value,
            "abs_diff": abs_diff,
            "diff_pct": diff_pct,
            "source": QUARANTINE_SOURCE,
            "note": note,
        }

    def backfill_delivery(
        self,
        trade_date: date,
        updates: Dict[str, Tuple[Optional[float], Optional[float]]],
    ) -> int:
        """为一致的 K 线补齐交割量字段，返回实际更新的行数。

        ``updates`` 形如 ``{code: (delivery_qty, delivery_pct)}``；只填补本地为空的
        字段，已有值一律不覆盖。
        """

        if not updates:
            return 0

        model = self._stock_daily_model()
        columns = _model_column_names(model)
        missing = {"delivery_qty", "delivery_pct"} - columns
        if missing:
            raise RuntimeError(
                f"StockDaily 缺少列 {sorted(missing)}，交割量无法回填；请先落地存储层改动。"
            )

        updated = 0
        with self.db.session_scope() as session:
            for code, (delivery_qty, delivery_pct) in updates.items():
                stmt = select(model).where(model.code == code, model.date == trade_date)
                row = session.execute(stmt).scalars().first()
                if row is None:
                    continue
                changed = False
                if delivery_qty is not None and row.delivery_qty is None:
                    row.delivery_qty = float(delivery_qty)
                    changed = True
                if delivery_pct is not None and row.delivery_pct is None:
                    row.delivery_pct = float(delivery_pct)
                    changed = True
                if changed:
                    updated += 1
        return updated


class BhavcopyReconciliationService:
    """把 NSE 公布的 bhavcopy 与本地 ``stock_daily`` 对账。"""

    def __init__(
        self,
        *,
        store: Optional[BhavcopyReconciliationStore] = None,
        fetch_bhavcopy: Optional[Callable[[date], Dict[str, Any]]] = None,
        price_matches: Optional[Callable[[float, float], bool]] = None,
        volume_tolerance_pct: Optional[float] = None,
        db_manager: Any = None,
        enabled: Optional[bool] = None,
    ):
        self.store = store or BhavcopyReconciliationStore(db_manager)
        # 抓取入口延迟解析：客户端模块尚未落地时，只要不真正执行对账就不会报错。
        self._fetch_bhavcopy = fetch_bhavcopy
        self._price_matches = price_matches
        self._volume_tolerance_pct = (
            float(volume_tolerance_pct)
            if volume_tolerance_pct is not None
            else DEFAULT_VOLUME_TOLERANCE_PCT
        )
        # 对账要走网络，因此默认关闭：不配置即保持旧行为，离线测试也不会联网。
        self._enabled = bool(enabled) if enabled is not None else self._enabled_from_config()

    # ------------------------------------------------------------------
    # 依赖解析
    # ------------------------------------------------------------------
    @staticmethod
    def _enabled_from_config() -> bool:
        try:
            from src.config import get_config

            return bool(getattr(get_config(), "bhavcopy_reconciliation_enabled", False))
        except Exception:  # noqa: BLE001 - 配置异常不应把对账变成崩溃
            logger.warning("读取 bhavcopy_reconciliation_enabled 失败，按关闭处理", exc_info=True)
            return False

    def _fetch(self, trade_date: date) -> Dict[str, Any]:
        if self._fetch_bhavcopy is not None:
            return self._fetch_bhavcopy(trade_date)
        from src.services.nse_bhavcopy_client import fetch_bhavcopy

        return fetch_bhavcopy(trade_date)

    def _resolve_price_matcher(self) -> Optional[Callable[[float, float], bool]]:
        """取 bhavcopy 客户端定下的价格容差 helper；取不到则返回 None。"""

        if self._price_matches is not None:
            return self._price_matches
        try:
            from src.services import nse_bhavcopy_client
        except ImportError:
            return None
        for name in _PRICE_MATCH_CANDIDATES:
            candidate = getattr(nse_bhavcopy_client, name, None)
            if callable(candidate):
                return candidate
        return None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def reconcile(self, trade_date: date, *, codes: Optional[List[str]] = None) -> dict:
        summary = self._empty_summary(trade_date)

        if not self._enabled:
            summary["status"] = STATUS_DISABLED
            summary["reason"] = "BHAVCOPY_RECONCILIATION_ENABLED 未开启，跳过对账"
            return summary

        requested = self._normalize_requested_codes(codes)
        if codes is not None and not requested:
            # 指定了代码却一个 NSE 标的都没有：直接返回，绝不悄悄扩大成全量对账。
            summary["reason"] = "入参代码中没有 NSE 标的，无可对账范围"
            summary["skipped_non_nse"] = sorted({str(code).strip().upper() for code in codes if str(code).strip()})
            return summary

        try:
            published_raw = self._fetch(trade_date)
        except BhavcopyUnavailable as exc:
            # 周末、节假日、NSE 故障都会走到这里：属正常结果，不抛错也不补数据。
            summary["status"] = STATUS_UNAVAILABLE
            summary["reason"] = str(exc) or "bhavcopy 不可用"
            return summary

        published = {str(symbol).upper(): row for symbol, row in (published_raw or {}).items()}
        if not published:
            summary["status"] = STATUS_UNAVAILABLE
            summary["reason"] = "bhavcopy 为空，无可对账数据"
            return summary
        summary["published_symbol_count"] = len(published)

        stored = self.store.load_bars(
            trade_date,
            codes=[code for code, _ in requested] if requested else None,
        )

        price_matcher = self._resolve_price_matcher()
        summary["price_check"] = PRICE_CHECK_ENABLED if price_matcher else PRICE_CHECK_UNAVAILABLE
        if price_matcher is None:
            logger.warning(
                "bhavcopy 客户端未提供价格容差 helper，本次仅比对成交量（trade_date=%s）",
                trade_date,
            )

        pairs = self._build_pairs(requested, stored, published, summary)

        drafts: List[QuarantineDraft] = []
        delivery_updates: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        for code, symbol, bar, published_row in pairs:
            summary["compared"] += 1
            reasons, detail = self._compare(bar, published_row, price_matcher)
            if reasons:
                drafts.append(
                    QuarantineDraft(
                        code=code,
                        symbol=symbol,
                        trade_date=trade_date,
                        reasons=tuple(reasons),
                        stored_close=bar.close,
                        published_close=self._as_float(getattr(published_row, "close", None)),
                        stored_volume=bar.volume,
                        published_volume=self._as_float(getattr(published_row, "volume", None)),
                        detail=detail,
                    )
                )
                continue

            summary["agreed"] += 1
            pending = self._delivery_backfill_values(bar, published_row)
            if pending is not None:
                delivery_updates[code] = pending

        summary["quarantined"] = len(drafts)
        if drafts:
            # 幂等：同一交易日重复运行不会重复写隔离记录。
            already = self.store.quarantined_codes(trade_date)
            fresh = [draft for draft in drafts if draft.code not in already]
            summary["quarantine_records_written"] = self.store.write_quarantine(fresh)
            summary["quarantine_records_skipped"] = len(drafts) - len(fresh)

        if delivery_updates:
            summary["delivery_backfilled"] = self.store.backfill_delivery(trade_date, delivery_updates)

        summary["missing_in_bhavcopy"] = sorted(summary["missing_in_bhavcopy"])
        summary["missing_in_stock_daily"] = sorted(summary["missing_in_stock_daily"])
        summary["skipped_non_nse"] = sorted(summary["skipped_non_nse"])
        return summary

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _empty_summary(trade_date: date) -> Dict[str, Any]:
        return {
            "status": STATUS_OK,
            "trade_date": trade_date.isoformat(),
            "reason": None,
            "compared": 0,
            "agreed": 0,
            "quarantined": 0,
            "quarantine_records_written": 0,
            "quarantine_records_skipped": 0,
            "delivery_backfilled": 0,
            "missing_in_bhavcopy": [],
            "missing_in_stock_daily": [],
            "skipped_non_nse": [],
            "published_symbol_count": 0,
            "price_check": PRICE_CHECK_UNAVAILABLE,
        }

    @staticmethod
    def _normalize_requested_codes(codes: Optional[List[str]]) -> List[Tuple[str, str]]:
        """把入参代码统一成 ``(RELIANCE.NS, RELIANCE)`` 对，非 NSE 代码直接丢弃。"""

        normalized: List[Tuple[str, str]] = []
        seen: Set[str] = set()
        for raw in codes or []:
            text = str(raw or "").strip().upper()
            if not text:
                continue
            symbol = BhavcopyReconciliationService._nse_symbol(text)
            if symbol is None and "." not in text:
                # 允许传裸 SYMBOL：补上 .NS 后再走同一套映射。
                symbol = text
            if symbol is None:
                continue
            code = f"{symbol}.{NSE_SUFFIX}"
            if code in seen:
                continue
            seen.add(code)
            normalized.append((code, symbol))
        return normalized

    @staticmethod
    def _nse_symbol(code: str) -> Optional[str]:
        """``RELIANCE.NS`` -> ``RELIANCE``；非 NSE 代码返回 None。"""

        parts = split_suffix_symbol(code)
        if not parts:
            return None
        base, suffix = parts
        if suffix != NSE_SUFFIX:
            return None
        return base

    def _build_pairs(
        self,
        requested: List[Tuple[str, str]],
        stored: Dict[str, StoredBar],
        published: Dict[str, Any],
        summary: Dict[str, Any],
    ) -> List[Tuple[str, str, StoredBar, Any]]:
        """配对待比对的 ``(code, symbol, 本地 K 线, bhavcopy 行)``，并登记单边缺失。"""

        pairs: List[Tuple[str, str, StoredBar, Any]] = []

        if requested:
            for code, symbol in requested:
                bar = stored.get(code)
                published_row = published.get(symbol)
                if bar is None:
                    # 请求了但本地没有：若 bhavcopy 有，就是本地缺；两边都没有则两边都记。
                    summary["missing_in_stock_daily"].append(symbol)
                    if published_row is None:
                        summary["missing_in_bhavcopy"].append(code)
                    continue
                if published_row is None:
                    summary["missing_in_bhavcopy"].append(code)
                    continue
                pairs.append((code, symbol, bar, published_row))
            return pairs

        # 未指定代码时，比对范围就是当日本地已有的 NSE K 线。bhavcopy 覆盖全市场，
        # 其“多出来”的代码不属于本地关注范围，不作为缺失上报，只在
        # published_symbol_count 中体现。
        for code, bar in stored.items():
            symbol = self._nse_symbol(code)
            if symbol is None:
                summary["skipped_non_nse"].append(code)
                continue
            published_row = published.get(symbol)
            if published_row is None:
                summary["missing_in_bhavcopy"].append(code)
                continue
            pairs.append((code, symbol, bar, published_row))
        return pairs

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _compare(
        self,
        bar: StoredBar,
        published_row: Any,
        price_matcher: Optional[Callable[[float, float], bool]],
    ) -> Tuple[List[str], Dict[str, Any]]:
        """返回 ``(分歧原因列表, 明细)``；列表为空表示一致。"""

        reasons: List[str] = []
        detail: Dict[str, Any] = {}

        published_volume = self._as_float(getattr(published_row, "volume", None))
        stored_volume = self._as_float(bar.volume)
        if stored_volume is None:
            reasons.append(REASON_STORED_VOLUME_MISSING)
        elif published_volume is not None:
            diff_pct = self._relative_diff_pct(stored_volume, published_volume)
            detail["volume_diff_pct"] = diff_pct
            if diff_pct > self._volume_tolerance_pct:
                reasons.append(REASON_VOLUME_MISMATCH)
        detail["stored_volume"] = stored_volume
        detail["published_volume"] = published_volume

        published_close = self._as_float(getattr(published_row, "close", None))
        stored_close = self._as_float(bar.close)
        detail["stored_close"] = stored_close
        detail["published_close"] = published_close
        if price_matcher is not None and published_close is not None:
            if stored_close is None:
                reasons.append(REASON_STORED_CLOSE_MISSING)
            elif not price_matcher(stored_close, published_close):
                reasons.append(REASON_CLOSE_MISMATCH)
        detail["price_check"] = PRICE_CHECK_ENABLED if price_matcher else PRICE_CHECK_UNAVAILABLE
        detail["volume_tolerance_pct"] = self._volume_tolerance_pct
        return reasons, detail

    @staticmethod
    def _relative_diff_pct(left: float, right: float) -> float:
        base = max(abs(left), abs(right))
        if base == 0:
            return 0.0
        return abs(left - right) / base * 100.0

    def _delivery_backfill_values(
        self,
        bar: StoredBar,
        published_row: Any,
    ) -> Optional[Tuple[Optional[float], Optional[float]]]:
        """只在本地为空、且 bhavcopy 有值时回填，绝不覆盖已有值。"""

        published_qty = self._as_float(getattr(published_row, "delivery_qty", None))
        published_pct = self._as_float(getattr(published_row, "delivery_pct", None))
        qty = published_qty if (published_qty is not None and bar.delivery_qty is None) else None
        pct = published_pct if (published_pct is not None and bar.delivery_pct is None) else None
        if qty is None and pct is None:
            return None
        return qty, pct
