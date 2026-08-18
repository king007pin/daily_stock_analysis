# -*- coding: utf-8 -*-
"""
====================================================================
Post-Market End-Of-Day (EOD) Pipeline Service
====================================================================

Implements:
1. EOD Closing price aggregation across watchlists.
2. FII / DII net institutional cash & derivative metrics synthesis.
3. Automated Obsidian Markdown EOD Report generation in 02-Reports/Daily-EOD/.

Zero-Hallucination Invariant (AGENTS.md Sec 1.3): every field on
EODReportData must be populated from a real source (see
src/services/eod_market_data.py) or left in an explicit "unavailable"
state. The template renders exactly what's in the dataclass - it no
longer contains any hardcoded price/regime/basket text of its own.
"""

import os
import logging
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class EODReportData:
    date_str: str

    # Benchmark indices - None means the real fetch failed, render as unavailable.
    nifty_close: Optional[float] = None
    nifty_chg_pct: Optional[float] = None
    nifty_regime: Optional[str] = None
    sensex_close: Optional[float] = None
    sensex_chg_pct: Optional[float] = None
    sensex_regime: Optional[str] = None

    # FII/DII - real data source not yet available (see remediation vault
    # note); status defaults to UNAVAILABLE rather than a fabricated number.
    fii_dii_status: str = "UNAVAILABLE"
    fii_dii_unavailable_reason: Optional[str] = None
    fii_net_cr: Optional[float] = None
    dii_net_cr: Optional[float] = None
    fii_long_pct: Optional[float] = None
    market_bias: Optional[str] = None

    # Watchlist-scoped movers - NOT NSE-market-wide top gainers/losers.
    watchlist_top_gainers: List[Dict[str, Any]] = field(default_factory=list)
    watchlist_top_losers: List[Dict[str, Any]] = field(default_factory=list)

    # Real signal-outcome-backed BTST summary (see eod_market_data.compute_btst_summary).
    btst_performance_summary: Dict[str, Any] = field(default_factory=lambda: {"status": "NO_SIGNALS_YET"})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EODPipelineService:
    """Post-Market EOD Report Generator and Vault Bridge."""

    def __init__(self, vault_path: Optional[str] = None):
        self.vault_path = vault_path or "/Users/shubhammac/SSD/Obsidian/Daily Stock Analysis/Daily Stock Analysis"

    @staticmethod
    def _render_index_row(label: str, close: Optional[float], chg_pct: Optional[float], regime: Optional[str]) -> str:
        if close is None:
            return f"| **{label}** | *data unavailable* | - | - |"
        chg_str = f"{chg_pct:+.2f}%" if chg_pct is not None else "-"
        regime_str = regime or "-"
        return f"| **{label}** | **{close:,.2f}** | {chg_str} | {regime_str} |"

    @staticmethod
    def _render_movers_section(title: str, movers: List[Dict[str, Any]]) -> str:
        if not movers:
            return f"**{title}:** none today.\n"
        lines = [f"**{title}:**"]
        for m in movers:
            lines.append(f"* `{m.get('symbol', '?')}` {m.get('change', '?')}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_btst_section(summary: Dict[str, Any]) -> str:
        status = summary.get("status", "UNAVAILABLE")
        horizon = summary.get("horizon", "1d")
        if status == "TRACKED":
            hit_rate = summary.get("hit_rate_pct")
            hit_rate_str = f"{hit_rate:.1f}%" if hit_rate is not None else "n/a (no completed signals yet)"
            avg_ret = summary.get("avg_stock_return_pct")
            avg_ret_str = f"{avg_ret:+.2f}%" if avg_ret is not None else "n/a"
            return (
                f"* **Tracked signals (horizon={horizon}):** {summary.get('total', 0)} total, "
                f"{summary.get('completed', 0)} completed ({summary.get('hit', 0)} hit / {summary.get('miss', 0)} miss)\n"
                f"* **Hit rate:** {hit_rate_str} | **Avg return:** {avg_ret_str}\n"
            )
        if status == "NO_SIGNALS_YET":
            return f"* No tracked BTST-horizon ({horizon}) signals recorded yet for this market - not enough data for a real track record.\n"
        return f"* BTST stats unavailable: {summary.get('reason', 'unknown error')}\n"

    def generate_eod_markdown_report(self, data: EODReportData) -> str:
        """Constructs rich Markdown report adhering to Obsidian vault format.

        Every value here comes directly from `data` - no hardcoded price,
        percentage, regime, or basket text. Missing real data renders as an
        explicit unavailable state, never a filled-in guess.
        """
        date_formatted = data.date_str

        if data.fii_dii_status == "TRACKED":
            fii_dii_section = (
                f"$$\\text{{FII Net Cash: }} {data.fii_net_cr:+,.2f} \\text{{ Cr}} \\quad | \\quad "
                f"\\text{{DII Net Cash: }} {data.dii_net_cr:+,.2f} \\text{{ Cr}}$$\n"
                f"$$\\mathbf{{\\text{{Net Institutional Balance: }} {(data.fii_net_cr + data.dii_net_cr):+,.2f} \\text{{ Cr}}}}$$\n"
                f"$$\\mathbf{{\\text{{FII Index Futures Long Exposure: }} {data.fii_long_pct:.1f}\\% \\quad [\\text{{{data.market_bias}}}]}}$$\n"
            )
        else:
            reason = data.fii_dii_unavailable_reason or "no real data source wired yet"
            fii_dii_section = f"*FII/DII data unavailable this run ({reason}). Not shown as a guess.*\n"

        report_md = f"""---
title: 📊 Daily EOD Market & Institutional Flow Report ({date_formatted})
date: {date_formatted}
tags:
  - eod-report
  - market-review
  - fii-dii
  - closing-summary
---

# 📊 Daily EOD Market & Institutional Report ({date_formatted})

**Backlink:** [[00-Dashboard/Market-Command-Center|Command Center]] | [[10-Macro-Liquidity-Radar/01-FII-DII-Institutional-Flow-Tracker|FII/DII Radar]]

---

## 🏛️ 1. Benchmark Indices Closing Summary

| Benchmark Index | Final Closing Level | Daily Change (%) | Regime (index-sign derived) |
|---|:---:|:---:|:---:|
{self._render_index_row("Nifty 50", data.nifty_close, data.nifty_chg_pct, data.nifty_regime)}
{self._render_index_row("BSE Sensex", data.sensex_close, data.sensex_chg_pct, data.sensex_regime)}

---

## 🌊 2. Institutional FII / DII Flow Synthesis

{fii_dii_section}
---

## 📈 3. Watchlist Movers (not NSE-market-wide)

{self._render_movers_section("Top Gainers", data.watchlist_top_gainers)}
{self._render_movers_section("Top Losers", data.watchlist_top_losers)}
---

## ⚡ 4. BTST (horizon=1d) Signal Track Record

{self._render_btst_section(data.btst_performance_summary)}"""
        return report_md

    def save_eod_report_to_vault(self, data: EODReportData) -> str:
        """Saves generated markdown report to 02-Reports/Daily-EOD/ in Obsidian vault."""
        target_dir = os.path.join(self.vault_path, "02-Reports", "Daily-EOD")
        os.makedirs(target_dir, exist_ok=True)
        file_name = f"EOD-Report-{data.date_str.replace('-', '')}.md"
        file_path = os.path.join(target_dir, file_name)

        md_content = self.generate_eod_markdown_report(data)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        return file_path
