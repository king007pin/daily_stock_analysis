# -*- coding: utf-8 -*-
"""把 ``stock_daily`` 里已存的 NSE 历史逐日与交易所 bhavcopy 对账。

为什么需要单独跑一次
--------------------
定时任务里的对账只看**上一个交易日**（``main._run_bhavcopy_reconciliation``）。也就是说
2026-08-25 之前的整个存量从未与交易所核对过——而这些 K 线正是每一次回测、每一个
outcome 评分、vault 里每一个基准率的输入。已知的坏数据不止一条：bhavcopy 客户端在
拟合容差时，单是 IDEA.NS 的成交量就找到 10 处 -57.7% ~ -94.8% 的错误。

本脚本只做一件事：把存量里每一个交易日补跑一遍对账，然后把整段历史的分歧数报出来。

刻意不做的事
------------
1. **不发告警。** 92 天的补跑会产生一连串通知，最快的效果是让人把告警渠道静音。
   告警属于每日定时那条路径，一天一条、有人会看。这里只打印汇总。
2. **不跳过"看起来已对过"的日期。** ``delivery_pct`` 只在 K 线与交易所一致时回填，
   所以它不能当"已检查"的标记：2026-08-31 就是 13 根里只有 11 根有值，剩下两根当时
   还不存在。按它跳过会静悄悄漏掉从未检查过的日期。重复对账本身是幂等的
   （隔离记录按交易日去重，已有 delivery 值不覆盖），补跑一遍比漏掉一天便宜。
3. **不修数据。** 分歧写进隔离表留证，K 线原样不动——和定时路径同一条规矩。

用法::

    python scripts/reconcile_bhavcopy_archive.py                    # 全量补跑
    python scripts/reconcile_bhavcopy_archive.py --start 2026-08-01 # 指定区间
    python scripts/reconcile_bhavcopy_archive.py --limit 5          # 先试跑 5 天
    python scripts/reconcile_bhavcopy_archive.py --json             # 机器可读汇总

退出码：完成即 0，**哪怕找到一堆分歧**——找到分歧说明脚本在工作。只有在连续抓取
失败被迫中止时才返回 1。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("reconcile_archive")

# NSE 的归档接口对连续请求并不友好，每次抓取本身还有 30 秒超时。默认给一点间隔。
DEFAULT_DELAY_SECONDS = 1.0

# 连续这么多天抓不到，就不是"这天没有数据"，而是对面已经不想理我们了：停下来，
# 而不是把剩下的日期也撞一遍。
CONSECUTIVE_FAILURE_LIMIT = 5

NSE_SUFFIX = ".NS"


def stored_nse_trading_days(db_manager: Any = None) -> List[date]:
    """``stock_daily`` 里所有含 NSE 标的的交易日，从旧到新。

    从存量日期出发而不是从日历出发：周末、节假日本来就不会出现在这里，也就不会
    白白发起抓取；同时也不会为一个我们根本没有 K 线的日子"声称"做过对账。
    """
    from sqlalchemy import select

    from src.storage import DatabaseManager, StockDaily

    manager = db_manager or DatabaseManager.get_instance()
    with manager.get_session() as session:
        rows = session.execute(
            select(StockDaily.date)
            .where(StockDaily.code.like(f"%{NSE_SUFFIX}"))
            .group_by(StockDaily.date)
            .order_by(StockDaily.date.asc())
        ).all()
    return [row[0] for row in rows if row[0] is not None]


def _blank_totals() -> Dict[str, int]:
    return {
        "days_attempted": 0,
        "days_reconciled": 0,
        "days_unavailable": 0,
        "days_failed": 0,
        "compared": 0,
        "agreed": 0,
        "quarantined": 0,
        "quarantine_records_written": 0,
        "delivery_backfilled": 0,
    }


def sweep(
    days: Sequence[date],
    *,
    reconcile: Callable[[date], Dict[str, Any]],
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    on_day: Optional[Callable[[date, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """逐日对账并汇总。抓取异常按天记账，不会中断整趟补跑。"""

    totals = _blank_totals()
    disagreements: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    consecutive_failures = 0
    aborted_after: Optional[str] = None

    for index, day in enumerate(days):
        if index and delay_seconds > 0:
            sleep(delay_seconds)

        totals["days_attempted"] += 1
        try:
            summary = reconcile(day)
        except Exception as exc:  # noqa: BLE001 - 单日失败不该毁掉整趟
            consecutive_failures += 1
            totals["days_failed"] += 1
            failures.append({"date": day.isoformat(), "error": f"{type(exc).__name__}: {exc}"})
            logger.warning("%s 对账失败: %s", day.isoformat(), exc)
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                aborted_after = day.isoformat()
                break
            continue

        consecutive_failures = 0
        status = str(summary.get("status") or "")
        if status == "ok":
            totals["days_reconciled"] += 1
        elif status == "unavailable":
            totals["days_unavailable"] += 1

        for key in ("compared", "agreed", "quarantined", "quarantine_records_written", "delivery_backfilled"):
            totals[key] += int(summary.get(key) or 0)

        for detail in summary.get("quarantine_details") or []:
            disagreements.append({"date": day.isoformat(), **detail})

        if on_day is not None:
            on_day(day, summary)

    return {
        "totals": totals,
        "disagreements": disagreements,
        "failures": failures,
        "aborted_after": aborted_after,
    }


def _print_day(day: date, summary: Dict[str, Any]) -> None:
    status = summary.get("status")
    if status == "ok":
        marker = "!" if summary.get("quarantined") else "."
        print(
            f"{marker} {day.isoformat()}  compared={summary.get('compared'):>3}  "
            f"agreed={summary.get('agreed'):>3}  quarantined={summary.get('quarantined')}  "
            f"delivery={summary.get('delivery_backfilled')}"
        )
    else:
        print(f"- {day.isoformat()}  {status}: {summary.get('reason')}")


def _print_report(result: Dict[str, Any]) -> None:
    totals = result["totals"]
    print()
    print("=" * 62)
    print("Archive reconciliation")
    print("=" * 62)
    for label, key in (
        ("days attempted", "days_attempted"),
        ("  reconciled", "days_reconciled"),
        ("  bhavcopy unavailable", "days_unavailable"),
        ("  failed", "days_failed"),
        ("bars compared", "compared"),
        ("bars agreeing", "agreed"),
        ("bars quarantined", "quarantined"),
        ("quarantine rows written", "quarantine_records_written"),
        ("delivery values backfilled", "delivery_backfilled"),
    ):
        print(f"{label:<28}{totals[key]:>8}")

    compared = totals["compared"]
    if compared:
        rate = totals["quarantined"] / compared * 100
        print(f"{'disagreement rate':<28}{rate:>7.2f}%")

    if result["disagreements"]:
        print()
        print("Disagreeing bars:")
        for item in result["disagreements"]:
            print(
                f"  {item['date']}  {item.get('code')}  {','.join(item.get('reasons') or [])}  "
                f"stored_close={item.get('stored_close')} published_close={item.get('published_close')} "
                f"stored_volume={item.get('stored_volume')} published_volume={item.get('published_volume')}"
            )

    if result["failures"]:
        print()
        print(f"Fetch failures ({len(result['failures'])}):")
        for failure in result["failures"][:10]:
            print(f"  {failure['date']}  {failure['error']}")

    if result["aborted_after"]:
        print()
        print(
            f"ABORTED after {result['aborted_after']}: {CONSECUTIVE_FAILURE_LIMIT} consecutive "
            "fetch failures. Re-run to resume - the pass is idempotent."
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reconcile_bhavcopy_archive",
        description="Reconcile every stored NSE trading day against the exchange bhavcopy.",
    )
    parser.add_argument("--start", help="First trade date to reconcile (YYYY-MM-DD).")
    parser.add_argument("--end", help="Last trade date to reconcile (YYYY-MM-DD).")
    parser.add_argument("--limit", type=int, help="Reconcile at most this many days.")
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Seconds to wait between fetches (default {DEFAULT_DELAY_SECONDS}).",
    )
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    days = stored_nse_trading_days()
    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        days = [day for day in days if day >= start]
    if args.end:
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
        days = [day for day in days if day <= end]
    if args.limit is not None:
        days = days[: max(0, args.limit)]

    if not days:
        print("No stored NSE trading days in that range - nothing to reconcile.")
        return 0

    from src.services.bhavcopy_reconciliation_service import BhavcopyReconciliationService

    service = BhavcopyReconciliationService()
    print(f"Reconciling {len(days)} stored trading days: {days[0]} -> {days[-1]}")

    result = sweep(
        days,
        reconcile=service.reconcile,
        delay_seconds=args.delay,
        on_day=None if args.json else _print_day,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(result)

    return 1 if result["aborted_after"] else 0


if __name__ == "__main__":
    sys.exit(main())
