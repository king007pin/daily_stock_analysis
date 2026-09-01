# -*- coding: utf-8 -*-
"""NSE 官方 bhavcopy 抓取与解析，用于对账 ``stock_daily``。

为什么需要它
------------
在 2026-08-31 之前，本仓库所有的数据质量检查都是把 ``stock_daily`` 和 yfinance 比对 ——
而其中 943 行本来就是 ``YfinanceFetcher`` 写进去的。那是一次往返一致性检查：它能证明
存储与转换没有损坏数据，却**结构上不可能发现供应商本身就是错的**。

改成与交易所自己发布的记录比对之后，立刻发现了一个真实错误：
IDEA.NS 2026-08-25，本地存 460,728,374 股，NSE 公布 1,534,470,198 股，少了 70%。
``volume_ratio`` 是落库字段，放量又是盘中信号的主要触发条件，因此这种低估会静默地
压掉真实信号，日志里不留任何痕迹。

比对口径 —— 用实测决定，不靠假设
--------------------------------
2026-08-01 起 265 组（13 只标的 × 21 个交易日）实测：

**成交量：可靠。** 265 组里只有 1 组偏差超过 2%（即上述 IDEA.NS 的 70%），其余最大
偏差 0.5042%。信号与噪声之间有约 140 倍的间隔，因此 2% 阈值有充分余量。
成交量不受除权除息调整影响，是首选比对字段。

**收盘价：需要容差，且容差有边界。** ``stock_daily`` 存的是**除权除息调整后**的价格，
bhavcopy 公布的是**原始成交价**。实测 13 只标的中 12 只比值恒为 1.00000，只有 HAL.NS
出现 0.202% 的分红台阶（比值在除息日前后各自恒定，呈阶梯状）。

所以：
  - 默认价格容差取 1.0%，约为实测最大台阶的 5 倍；
  - **但这是有边界的**：一次超过标的价格 1% 的分红，会在除息日当天产生一次误报。
    这类误报是信息，不是错误 —— 它标记的是"这一天发生了公司行动"，应当据此核对，
    而不是当作数据损坏。
  - 因此价格比对是**辅助信号**，成交量才是主判据。

上游若改为存储原始价，或引入按标的的调整因子，这里的容差应当随之重新实测，不要沿用。
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

logger = logging.getLogger(__name__)

BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
)

# NSE 会拒绝没有浏览器 UA / Referer 的请求。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com/",
}

_TIMEOUT_SECONDS = 30

#: 成交量相对偏差阈值。实测：真实错误 70%，噪声上限 0.5042%。
VOLUME_TOLERANCE_PCT = 2.0

#: 收盘价相对偏差阈值。实测最大分红台阶 0.202%（HAL.NS），取约 5 倍余量。
#: 超过标的价格 1% 的分红会在除息日误报一次 —— 见模块 docstring。
PRICE_TOLERANCE_PCT = 1.0

#: bhavcopy 用 '-' 表示交割数据缺失。
_ABSENT = {"", "-", "NA", "N/A"}


class BhavcopyUnavailable(Exception):
    """当日 bhavcopy 取不到：周末、节假日、NSE 故障，或网络不可达。

    这是**正常结果**，不是错误。调用方应据此跳过对账，而不是补数据。
    """


@dataclass(frozen=True)
class BhavcopyRow:
    """交易所公布的一只标的的当日记录。

    ``close`` 是**原始成交价**，未经除权除息调整 —— 与 ``stock_daily`` 的口径不同，
    比对时必须走 :func:`prices_match`。
    """

    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    delivery_qty: Optional[float]
    delivery_pct: Optional[float]


def _to_float(raw: str) -> Optional[float]:
    value = (raw or "").strip()
    if value in _ABSENT:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def parse_bhavcopy(text: str) -> Dict[str, BhavcopyRow]:
    """解析 bhavcopy CSV 文本，按 SYMBOL 返回 EQ 系列的记录。

    与网络解耦，便于用固定样本离线测试。
    """
    rows: Dict[str, BhavcopyRow] = {}
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return rows
    # NSE 的表头带前导空格，逐列 strip 后再取值。
    field_map = {name.strip(): name for name in reader.fieldnames}

    def get(record, column: str) -> str:
        source = field_map.get(column)
        return record.get(source, "") if source else ""

    for record in reader:
        if get(record, "SERIES").strip() != "EQ":
            continue
        symbol = get(record, "SYMBOL").strip()
        if not symbol:
            continue
        close = _to_float(get(record, "CLOSE_PRICE"))
        volume = _to_float(get(record, "TTL_TRD_QNTY"))
        if close is None or volume is None:
            # 缺少关键字段的行不做猜测，直接跳过。
            continue
        rows[symbol] = BhavcopyRow(
            symbol=symbol,
            open=_to_float(get(record, "OPEN_PRICE")) or close,
            high=_to_float(get(record, "HIGH_PRICE")) or close,
            low=_to_float(get(record, "LOW_PRICE")) or close,
            close=close,
            volume=volume,
            delivery_qty=_to_float(get(record, "DELIV_QTY")),
            delivery_pct=_to_float(get(record, "DELIV_PER")),
        )
    return rows


def fetch_bhavcopy(trade_date: date) -> Dict[str, BhavcopyRow]:
    """抓取并解析某个交易日的 bhavcopy。

    Raises:
        BhavcopyUnavailable: 当日文件取不到或内容不是 bhavcopy。
    """
    url = BHAVCOPY_URL.format(ddmmyyyy=trade_date.strftime("%d%m%Y"))
    try:
        request = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - 任何取数失败都是"当日不可用"
        raise BhavcopyUnavailable(f"{trade_date}: {exc}") from exc

    if not payload.lstrip().upper().startswith("SYMBOL"):
        # 非交易日 NSE 常返回一个 HTML 错误页而不是 404。
        raise BhavcopyUnavailable(f"{trade_date}: response is not a bhavcopy")

    rows = parse_bhavcopy(payload)
    if not rows:
        raise BhavcopyUnavailable(f"{trade_date}: bhavcopy contained no EQ rows")
    logger.info("[Bhavcopy] %s: %d EQ symbols", trade_date, len(rows))
    return rows


def _relative_diff_pct(left: float, right: float) -> Optional[float]:
    if right in (None, 0) or left is None:
        return None
    return abs(float(left) - float(right)) / abs(float(right)) * 100.0


def volumes_match(
    stored_volume: Optional[float],
    published_volume: Optional[float],
    tolerance_pct: float = VOLUME_TOLERANCE_PCT,
) -> bool:
    """成交量是否一致。主判据 —— 不受除权除息影响。"""
    diff = _relative_diff_pct(stored_volume, published_volume)
    return diff is not None and diff <= tolerance_pct


def prices_match(
    stored_close: Optional[float],
    published_close: Optional[float],
    tolerance_pct: float = PRICE_TOLERANCE_PCT,
) -> bool:
    """收盘价是否一致，容忍除权除息造成的系统性偏移。

    ``stored_close`` 是调整后价格，``published_close`` 是原始成交价，两者本就不同。
    容差按实测的分红台阶取值；超过该幅度的分红会在除息日误报一次，那是需要核对的
    公司行动信号，不是数据损坏 —— 详见模块 docstring。
    """
    diff = _relative_diff_pct(stored_close, published_close)
    return diff is not None and diff <= tolerance_pct


def volume_diff_pct(
    stored_volume: Optional[float], published_volume: Optional[float]
) -> Optional[float]:
    """成交量相对偏差百分比，供隔离记录留证。"""
    return _relative_diff_pct(stored_volume, published_volume)


def price_diff_pct(
    stored_close: Optional[float], published_close: Optional[float]
) -> Optional[float]:
    """收盘价相对偏差百分比，供隔离记录留证。"""
    return _relative_diff_pct(stored_close, published_close)
