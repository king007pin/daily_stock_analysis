#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Widen the EOD daily run's universe with CN screening candidates (Phase 02
of the Quant Desk Roadmap).

Screening (src/services/screening/) is CN-only in practice, despite its own
pipeline validator technically accepting market="us": all 10 shipped strategy
YAMLs declare market_scope: [cn], none declare "us", and "in" (India) is not
in the validator's allow-list at all. This script only ever widens the CN
side of STOCK_LIST — it cannot discover new US or NSE/BSE candidates. Day-
trading (NSE) universe stays watchlist-curated via
scripts/run_daily_analysis.sh's premarket mode.

Runs the L1 statistical screen with use_llm=False (no LLM cost, filter/rank
only) — the actual qualitative LLM analysis still happens once per stock in
main.py's normal per-stock pipeline. Using use_llm=True here as well would
double-spend: once in screening's own L2 ranking, again in the per-stock
analysis that follows.

Usage:
    python scripts/screen_and_analyze_eod.py
    python scripts/screen_and_analyze_eod.py --strategy volume_breakout --max-output 15
    python scripts/screen_and_analyze_eod.py --dry-run
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.services.screening.pipeline import screen  # noqa: E402
from src.services.stock_list_parser import serialize_stock_list  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def discover_cn_candidates(strategy: str, max_output: int) -> list[str]:
    result = screen(strategy=strategy, market="cn", use_llm=False, max_output=max_output)
    return [pick.code for pick in result.picks]


def main() -> None:
    parser = argparse.ArgumentParser(description="Widen the EOD STOCK_LIST with CN screening candidates.")
    parser.add_argument("--strategy", default="momentum_quality", help="Strategy YAML name (cn-only).")
    parser.add_argument("--max-output", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", help="Print the merged ticker list without running main.py.")
    args = parser.parse_args()

    config = get_config()
    base_list = list(config.stock_list)

    try:
        candidates = discover_cn_candidates(args.strategy, args.max_output)
    except Exception as exc:
        print(
            f"[screen_and_analyze_eod] screening failed, falling back to STOCK_LIST only: {exc}",
            file=sys.stderr,
        )
        candidates = []

    merged = list(dict.fromkeys(base_list + candidates))  # dedupe, preserve order
    print(
        f"[screen_and_analyze_eod] STOCK_LIST({len(base_list)}) + "
        f"screened({len(candidates)}) = {len(merged)} unique tickers"
    )

    if args.dry_run:
        print(serialize_stock_list(",".join(merged)))
        return

    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "main.py"),
            "--stocks", ",".join(merged),
            "--force-run",
            "--no-notify",
        ],
        cwd=str(REPO_ROOT),
        check=True,
    )


if __name__ == "__main__":
    main()
