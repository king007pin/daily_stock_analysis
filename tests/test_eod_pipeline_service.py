# -*- coding: utf-8 -*-
"""
Tests for Post-Market EOD Pipeline Service
"""

import os
import unittest
import tempfile
from src.services.eod_pipeline_service import EODPipelineService, EODReportData


class TestEODPipelineService(unittest.TestCase):
    """Test suite for EODPipelineService."""

    def test_markdown_generation_and_file_save(self):
        """Verify EOD markdown report generation and proper vault serialization."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = EODPipelineService(vault_path=tmp_dir)

            data = EODReportData(
                date_str="2026-08-17",
                nifty_close=24312.50,
                nifty_chg_pct=0.42,
                nifty_regime="Risk-On",
                sensex_close=79850.20,
                sensex_chg_pct=0.38,
                sensex_regime="Risk-On",
                fii_dii_status="TRACKED",
                fii_net_cr=1200.0,
                dii_net_cr=1800.0,
                fii_long_pct=66.5,
                market_bias="BULLISH",
                watchlist_top_gainers=[{"symbol": "RTNPOWER.NS", "change": "+5.00%"}],
                watchlist_top_losers=[],
                btst_performance_summary={"status": "NO_SIGNALS_YET", "horizon": "1d"},
            )

            report_md = service.generate_eod_markdown_report(data)
            self.assertIn("Nifty 50", report_md)
            self.assertIn("24,312.50", report_md)
            self.assertIn("66.5\\%", report_md)
            self.assertIn("RTNPOWER.NS", report_md)

            saved_file = service.save_eod_report_to_vault(data)
            self.assertTrue(os.path.exists(saved_file))
            with open(saved_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertEqual(content, report_md)

    def test_no_hardcoded_placeholder_values_survive(self):
        """Regression guard: the old hardcoded literals must never reappear
        unless a real value happens to legitimately match them (astronomically
        unlikely for the exact old constants used here)."""
        data = EODReportData(date_str="2026-08-18")  # everything defaults to unavailable/empty
        report_md = EODPipelineService().generate_eod_markdown_report(data)

        self.assertNotIn("+0.42%", report_md)
        self.assertNotIn("+0.38%", report_md)
        self.assertNotIn("RTNPOWER.NS`, `EASEMYTRIP.NS`, `IDEA.NS`, `JPPOWER.NS`, `SUZLON.NS`", report_md)
        self.assertNotIn("Exit target gate at +10%", report_md)
        self.assertIn("data unavailable", report_md)

    def test_fii_dii_unavailable_state_does_not_fabricate_numbers(self):
        data = EODReportData(date_str="2026-08-18", fii_dii_unavailable_reason="scrape target JS-rendered, no static fallback")
        report_md = EODPipelineService().generate_eod_markdown_report(data)
        self.assertIn("FII/DII data unavailable", report_md)
        self.assertIn("scrape target JS-rendered", report_md)

    def test_btst_no_signals_yet_is_rendered_honestly(self):
        data = EODReportData(date_str="2026-08-18", btst_performance_summary={"status": "NO_SIGNALS_YET", "horizon": "1d"})
        report_md = EODPipelineService().generate_eod_markdown_report(data)
        self.assertIn("No tracked BTST-horizon", report_md)

    def test_btst_tracked_renders_real_stats(self):
        data = EODReportData(
            date_str="2026-08-18",
            btst_performance_summary={
                "status": "TRACKED", "horizon": "1d", "total": 12, "completed": 5,
                "hit": 3, "miss": 2, "hit_rate_pct": 60.0, "avg_stock_return_pct": 1.25,
            },
        )
        report_md = EODPipelineService().generate_eod_markdown_report(data)
        self.assertIn("60.0%", report_md)
        self.assertIn("3 hit / 2 miss", report_md)


if __name__ == "__main__":
    unittest.main()
