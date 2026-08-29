# -*- coding: utf-8 -*-
"""Unit tests for TradeMemoryService and Vector RAG pattern matching."""

import os
import shutil
import tempfile
import unittest
import numpy as np

from src.services.memory_service import TradeMemoryService, TradePatternRecord, MemoryQueryMatch


class TestTradeMemoryService(unittest.TestCase):
    """Verify vector indexing, hybrid embeddings, and regime-filtered RAG."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_dir = os.path.join(self.temp_dir, "chroma")
        self.sqlite_path = os.path.join(self.temp_dir, "memory.db")
        self.service = TradeMemoryService(db_dir=self.db_dir, sqlite_path=self.sqlite_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_record_vectorization_hybrid(self):
        rec = TradePatternRecord(
            setup_id="s1",
            stock_code="HAL.NS",
            market="NSE",
            setup_type="BREAKOUT",
            rsi=65.0,
            ema_spread_pct=3.5,
            volume_surge_ratio=2.0,
            kronos_projected_return=12.0,
            regime_vix_bucket="LOW",
            outcome_return_pct=14.5,
            trade_result="WIN",
            holding_days=5,
        )
        vec = rec.to_vector()
        self.assertIsInstance(vec, np.ndarray)
        self.assertEqual(len(vec), 4)
        # Unit norm verification
        self.assertAlmostEqual(float(np.linalg.norm(vec)), 1.0, places=5)

    def test_trade_indexing_and_similarity_retrieval(self):
        # 1. Index 3 historical records
        r1 = TradePatternRecord(
            setup_id="r1",
            stock_code="BEL.NS",
            market="NSE",
            setup_type="BREAKOUT",
            rsi=64.0,
            ema_spread_pct=3.2,
            volume_surge_ratio=1.9,
            kronos_projected_return=11.5,
            regime_vix_bucket="LOW",
            outcome_return_pct=12.0,
            trade_result="WIN",
            holding_days=5,
            created_at="2026-03-14",
        )
        r2 = TradePatternRecord(
            setup_id="r2",
            stock_code="COCHINSHIP.NS",
            market="NSE",
            setup_type="BREAKOUT",
            rsi=66.0,
            ema_spread_pct=3.8,
            volume_surge_ratio=2.1,
            kronos_projected_return=13.0,
            regime_vix_bucket="LOW",
            outcome_return_pct=15.2,
            trade_result="WIN",
            holding_days=7,
            created_at="2026-04-10",
        )
        r3_bearish = TradePatternRecord(
            setup_id="r3",
            stock_code="IDEA.NS",
            market="NSE",
            setup_type="BREAKOUT",
            rsi=25.0,
            ema_spread_pct=-8.0,
            volume_surge_ratio=0.5,
            kronos_projected_return=-10.0,
            regime_vix_bucket="HIGH",
            outcome_return_pct=-6.0,
            trade_result="LOSS",
            holding_days=3,
            created_at="2026-01-20",
        )

        self.assertTrue(self.service.index_trade(r1))
        self.assertTrue(self.service.index_trade(r2))
        self.assertTrue(self.service.index_trade(r3_bearish))

        # Query with a current candidate (HAL.NS breakout)
        query_candidate = TradePatternRecord(
            setup_id="query",
            stock_code="HAL.NS",
            market="NSE",
            setup_type="BREAKOUT",
            rsi=65.0,
            ema_spread_pct=3.5,
            volume_surge_ratio=2.0,
            kronos_projected_return=12.0,
            regime_vix_bucket="LOW",
            outcome_return_pct=0.0,
            trade_result="PENDING",
            holding_days=5,
        )

        matches = self.service.query_similar_setups(query_candidate, top_k=2, min_similarity=0.80)
        self.assertGreaterEqual(len(matches), 1)
        # Closest match should be bullish breakouts
        self.assertIn(matches[0].record.stock_code, ("BEL.NS", "COCHINSHIP.NS"))
        self.assertGreater(matches[0].similarity_score, 0.90)

    def test_prompt_injection_formatting(self):
        r1 = TradePatternRecord(
            setup_id="r1",
            stock_code="BEL.NS",
            market="NSE",
            setup_type="BREAKOUT",
            rsi=64.0,
            ema_spread_pct=3.2,
            volume_surge_ratio=1.9,
            kronos_projected_return=11.5,
            regime_vix_bucket="LOW",
            outcome_return_pct=12.0,
            trade_result="WIN",
            holding_days=5,
            created_at="2026-03-14",
        )
        r2 = TradePatternRecord(
            setup_id="r2",
            stock_code="COCHINSHIP.NS",
            market="NSE",
            setup_type="BREAKOUT",
            rsi=66.0,
            ema_spread_pct=3.8,
            volume_surge_ratio=2.1,
            kronos_projected_return=13.0,
            regime_vix_bucket="LOW",
            outcome_return_pct=15.2,
            trade_result="WIN",
            holding_days=7,
            created_at="2026-04-10",
        )
        self.service.index_trade(r1)
        self.service.index_trade(r2)

        prompt_str = self.service.format_memory_prompt_injection(r1, top_k=2)
        self.assertIn("Historical Pattern Analogue Memory", prompt_str)
        self.assertIn("100.0%", prompt_str)
        self.assertIn("BEL.NS", prompt_str)


if __name__ == "__main__":
    unittest.main()
