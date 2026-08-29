#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a score -> outcome calibration curve from decision_signal_outcomes.

Phase 01 of the Quant Desk Roadmap: measure whether the LLM's 0-100 decision
score is actually calibrated (a stated 80 should win ~80% of the time) before
trusting it for sizing or "high conviction" language.

Buckets are kept separate PER HORIZON — a score calibrated for "intraday"
day-trade signals does not transfer to a 10-day swing call, and pooling them
would hide that. Buckets below MIN_SAMPLE_SIZE report insufficient_sample
rather than a misleading rate (there is no existing convention to reuse here:
memory_service.py's RAG retrieval has no equivalent sample-size gate despite
the vault's own audit doc specifying one — this script is the first place in
the codebase that actually implements it).

Usage:
    python scripts/build_calibration_curve.py
    python scripts/build_calibration_curve.py --horizon intraday
    python scripts/build_calibration_curve.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src.storage import DatabaseManager, DecisionSignalOutcomeRecord, DecisionSignalRecord  # noqa: E402

MIN_SAMPLE_SIZE = 5
SCORE_BUCKET_WIDTH = 10
SCORE_BUCKETS = [(lo, lo + SCORE_BUCKET_WIDTH) for lo in range(0, 100, SCORE_BUCKET_WIDTH)]


def _bucket_for_score(score: Optional[int]) -> Optional[str]:
    if score is None:
        return None
    score = max(0, min(100, int(score)))
    for lo, hi in SCORE_BUCKETS:
        if lo <= score < hi or (hi == 100 and score == 100):
            return f"{lo}-{hi}"
    return None


def _fetch_rows(db: DatabaseManager, horizon: Optional[str]) -> List[Dict[str, Any]]:
    conditions = [DecisionSignalOutcomeRecord.eval_status == "completed"]
    if horizon:
        conditions.append(DecisionSignalOutcomeRecord.horizon == horizon)
    with db.get_session() as session:
        rows = session.execute(
            select(
                DecisionSignalRecord.score,
                DecisionSignalOutcomeRecord.horizon,
                DecisionSignalOutcomeRecord.outcome,
                DecisionSignalOutcomeRecord.stock_return_pct,
            )
            .join(DecisionSignalRecord, DecisionSignalRecord.id == DecisionSignalOutcomeRecord.signal_id)
            .where(*conditions)
        ).all()
    return [
        {"score": score, "horizon": h, "outcome": outcome, "stock_return_pct": ret}
        for score, h, outcome, ret in rows
    ]


def build_curve(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Group rows by horizon -> score bucket, compute win rate / avg return / Brier score.

    win rate = hit / (hit + miss), matching BacktestEngine._compute_advice_breakdown's
    existing win/loss/neutral convention elsewhere in this codebase — "neutral"
    outcomes are excluded from the win-rate denominator, not counted as losses.
    """
    grouped: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        bucket = _bucket_for_score(row["score"])
        if bucket is None or row["horizon"] is None:
            continue
        grouped[row["horizon"]][bucket].append(row)

    curve: Dict[str, Dict[str, Any]] = {}
    for horizon, buckets in grouped.items():
        curve[horizon] = {}
        for lo, hi in SCORE_BUCKETS:
            bucket_key = f"{lo}-{hi}"
            bucket_rows = buckets.get(bucket_key, [])
            n = len(bucket_rows)
            if n < MIN_SAMPLE_SIZE:
                curve[horizon][bucket_key] = {"n": n, "status": "insufficient_sample"}
                continue

            hits = sum(1 for r in bucket_rows if r["outcome"] == "hit")
            misses = sum(1 for r in bucket_rows if r["outcome"] == "miss")
            decided = hits + misses
            win_rate_pct = round(hits / decided * 100, 1) if decided else None

            returns = [r["stock_return_pct"] for r in bucket_rows if r["stock_return_pct"] is not None]
            avg_return_pct = round(sum(returns) / len(returns), 2) if returns else None

            # Brier score: predicted probability = bucket midpoint / 100, actual = 1 if hit
            # else 0 (neutral outcomes excluded — there is no "did it happen" answer for them).
            midpoint_prob = (lo + hi) / 2 / 100
            brier_terms = [
                (midpoint_prob - (1.0 if r["outcome"] == "hit" else 0.0)) ** 2
                for r in bucket_rows
                if r["outcome"] in ("hit", "miss")
            ]
            brier_score = round(sum(brier_terms) / len(brier_terms), 4) if brier_terms else None

            curve[horizon][bucket_key] = {
                "n": n,
                "status": "ok",
                "win_rate_pct": win_rate_pct,
                "avg_return_pct": avg_return_pct,
                "brier_score": brier_score,
            }
    return curve


def _print_table(curve: Dict[str, Dict[str, Any]]) -> None:
    if not curve:
        print("No completed decision-signal outcomes found yet. Nothing to calibrate.")
        return
    for horizon in sorted(curve.keys()):
        print(f"\n=== horizon: {horizon} ===")
        print(f"{'score':<10}{'n':<6}{'status':<20}{'win_rate%':<12}{'avg_return%':<14}{'brier':<8}")
        for lo, hi in SCORE_BUCKETS:
            bucket_key = f"{lo}-{hi}"
            entry = curve[horizon].get(bucket_key)
            if entry is None:
                continue
            if entry["status"] == "insufficient_sample":
                print(f"{bucket_key:<10}{entry['n']:<6}{'insufficient_sample':<20}{'-':<12}{'-':<14}{'-':<8}")
            else:
                print(
                    f"{bucket_key:<10}{entry['n']:<6}{'ok':<20}"
                    f"{entry['win_rate_pct']:<12}{entry['avg_return_pct']:<14}{entry['brier_score']:<8}"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build score -> outcome calibration curve per horizon.")
    parser.add_argument("--horizon", default=None, help="Restrict to one horizon (e.g. intraday, 3d).")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a table.")
    args = parser.parse_args()

    db = DatabaseManager.get_instance()
    rows = _fetch_rows(db, args.horizon)
    curve = build_curve(rows)

    if args.json:
        print(json.dumps(curve, indent=2))
    else:
        _print_table(curve)


if __name__ == "__main__":
    main()
