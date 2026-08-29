# -*- coding: utf-8 -*-
"""Tests for scripts/screen_and_analyze_eod.py's candidate-merge logic."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from screen_and_analyze_eod import discover_cn_candidates  # noqa: E402


def _fake_result(codes):
    return SimpleNamespace(picks=[SimpleNamespace(code=c) for c in codes])


@patch("screen_and_analyze_eod.screen")
def test_discover_cn_candidates_uses_llm_free_screen(mock_screen):
    mock_screen.return_value = _fake_result(["601166", "601318"])

    codes = discover_cn_candidates("momentum_quality", 10)

    assert codes == ["601166", "601318"]
    mock_screen.assert_called_once_with(
        strategy="momentum_quality", market="cn", use_llm=False, max_output=10
    )


def test_merge_dedupes_preserving_stock_list_order():
    base_list = ["RELIANCE.NS", "TCS.NS", "AAPL", "600519"]
    candidates = ["601166", "600519", "601318"]  # 600519 already in STOCK_LIST

    merged = list(dict.fromkeys(base_list + candidates))

    assert merged == ["RELIANCE.NS", "TCS.NS", "AAPL", "600519", "601166", "601318"]


def test_merge_with_no_candidates_falls_back_to_base_list():
    base_list = ["RELIANCE.NS", "TCS.NS", "AAPL", "600519"]
    candidates = []

    merged = list(dict.fromkeys(base_list + candidates))

    assert merged == base_list
