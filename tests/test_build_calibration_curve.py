# -*- coding: utf-8 -*-
"""Tests for scripts/build_calibration_curve.py's pure aggregation logic."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from build_calibration_curve import MIN_SAMPLE_SIZE, _bucket_for_score, build_curve  # noqa: E402


def _row(score, horizon="intraday", outcome="hit", stock_return_pct=1.0):
    return {"score": score, "horizon": horizon, "outcome": outcome, "stock_return_pct": stock_return_pct}


def test_bucket_for_score_boundaries():
    assert _bucket_for_score(0) == "0-10"
    assert _bucket_for_score(9) == "0-10"
    assert _bucket_for_score(10) == "10-20"
    assert _bucket_for_score(80) == "80-90"
    assert _bucket_for_score(100) == "90-100"
    assert _bucket_for_score(None) is None


def test_below_min_sample_reports_insufficient():
    rows = [_row(85) for _ in range(MIN_SAMPLE_SIZE - 1)]
    curve = build_curve(rows)
    assert curve["intraday"]["80-90"]["status"] == "insufficient_sample"
    assert curve["intraday"]["80-90"]["n"] == MIN_SAMPLE_SIZE - 1
    assert "win_rate_pct" not in curve["intraday"]["80-90"]


def test_win_rate_excludes_neutral_from_denominator():
    rows = (
        [_row(85, outcome="hit") for _ in range(4)]
        + [_row(85, outcome="miss") for _ in range(1)]
        + [_row(85, outcome="neutral") for _ in range(3)]
    )
    curve = build_curve(rows)
    entry = curve["intraday"]["80-90"]
    assert entry["status"] == "ok"
    assert entry["n"] == 8
    assert entry["win_rate_pct"] == 80.0  # 4 hit / (4 hit + 1 miss), neutral excluded


def test_brier_score_perfect_calibration_is_near_zero():
    # Bucket midpoint for 90-100 is 0.95; 5 hits should score close to (0.05)^2.
    rows = [_row(95, outcome="hit") for _ in range(5)]
    curve = build_curve(rows)
    entry = curve["intraday"]["90-100"]
    assert entry["status"] == "ok"
    assert entry["brier_score"] == round((1 - 0.95) ** 2, 4)


def test_horizons_kept_separate_not_pooled():
    rows = [_row(70, horizon="intraday") for _ in range(5)] + [_row(70, horizon="10d") for _ in range(5)]
    curve = build_curve(rows)
    assert "intraday" in curve
    assert "10d" in curve
    assert curve["intraday"]["70-80"]["n"] == 5
    assert curve["10d"]["70-80"]["n"] == 5


def test_avg_return_pct_computed_from_stock_return_pct():
    rows = [_row(85, stock_return_pct=v) for v in (2.0, 4.0, 6.0, 8.0, 10.0)]
    curve = build_curve(rows)
    assert curve["intraday"]["80-90"]["avg_return_pct"] == 6.0
