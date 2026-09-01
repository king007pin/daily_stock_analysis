# -*- coding: utf-8 -*-
"""检查 ``stock_daily`` 里每只标的的最新 K 线有多旧，并把落后的标的报出来。

为什么需要这个服务（2026-09-01 实测）：

抓取失败不会中断分析，也不会留下任何痕迹。当天 08:57 的运行里，
``BCG.NS`` 与 ``EASEMYTRIP.NS`` 的日志写的是 ``数据保存成功（新增 0 条）`` ——
**保存成功，写入 0 条**。yfinance 当时还没发布 08-31 的日线，于是这两只标的停在
08-28；``588200`` 更落后 6 根，因为 Pytdx 连不上任何服务器，只有备份源能用。
与此同时，这三只标的当天照常产出了信号。

也就是说：**系统会拿几天前的价格发今天的信号，而没有任何东西会发现。**
这与"服务写完却没人调用"是同一类缺陷，只是发生在数据层。

本服务只读数据库，不联网、不抓取、不修数据：它只回答"哪些标的的价格过期了，
其中哪些还在被下信号"。修不修、怎么修，交给调用方和人。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# 一根 K 线落后几天算"过期"。默认 4 个自然日：足够跨过一个周末（五 -> 一 是 3 天）
# 再加一个节假日，又不至于让真正掉队一周的标的混过去。
DEFAULT_MAX_AGE_DAYS = 4


@dataclass(frozen=True)
class StaleCode:
    """一只价格已经过期的标的。"""

    code: str
    last_bar: date
    days_behind: int
    signals_since_last_bar: int

    @property
    def signalled_while_stale(self) -> bool:
        """是否在最后一根 K 线之后还继续发过信号 —— 这才是真正有害的那一类。"""

        return self.signals_since_last_bar > 0


class StockDataFreshnessService:
    """报告 ``stock_daily`` 的新鲜度。只读。"""

    def __init__(
        self,
        *,
        db_manager: Any = None,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    ):
        self._db_manager = db_manager
        self.max_age_days = max(1, int(max_age_days))

    @property
    def db(self) -> Any:
        if self._db_manager is None:
            from src.storage import DatabaseManager

            self._db_manager = DatabaseManager.get_instance()
        return self._db_manager

    def find_stale(
        self,
        *,
        as_of: date,
        codes: Optional[Sequence[str]] = None,
    ) -> List[StaleCode]:
        """返回落后超过 ``max_age_days`` 的标的，最旧的排在最前面。

        完全没有 K 线的标的不在此列：那是"从未抓到"，不是"过期"，两者的处理方式
        不同，混在一起报只会让告警失去意义。
        """

        from sqlalchemy import func, select

        from src.storage import DecisionSignalRecord, StockDaily

        wanted = {str(code).strip() for code in codes or [] if str(code).strip()}

        with self.db.get_session() as session:
            latest_stmt = select(StockDaily.code, func.max(StockDaily.date)).group_by(StockDaily.code)
            if wanted:
                latest_stmt = latest_stmt.where(StockDaily.code.in_(sorted(wanted)))
            latest_bars = {code: last for code, last in session.execute(latest_stmt).all() if last is not None}

            stale: List[StaleCode] = []
            for code, last_bar in latest_bars.items():
                days_behind = (as_of - last_bar).days
                if days_behind <= self.max_age_days:
                    continue

                signals_since = session.execute(
                    select(func.count())
                    .select_from(DecisionSignalRecord)
                    .where(
                        DecisionSignalRecord.stock_code == code,
                        func.date(DecisionSignalRecord.created_at) > last_bar,
                    )
                ).scalar_one()

                stale.append(
                    StaleCode(
                        code=code,
                        last_bar=last_bar,
                        days_behind=days_behind,
                        signals_since_last_bar=int(signals_since or 0),
                    )
                )

        stale.sort(key=lambda item: (-item.days_behind, item.code))
        return stale

    def summary(self, *, as_of: date, codes: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """给调用方一个可直接记录或告警的结果。"""

        stale = self.find_stale(as_of=as_of, codes=codes)
        signalled = [item for item in stale if item.signalled_while_stale]
        return {
            "as_of": as_of.isoformat(),
            "max_age_days": self.max_age_days,
            "stale_count": len(stale),
            "signalled_while_stale_count": len(signalled),
            "stale": stale,
        }
