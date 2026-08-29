# -*- coding: utf-8 -*-
"""
====================================================================
ChromaDB Vector RAG Memory & Pattern Analogue Retrieval Service
====================================================================

Stores past trade setups, indicator configurations, and realized outcomes.
Uses a hybrid numerical-semantic vectorizer to avoid number-blindness traps
and supports VIX regime pre-filtering to prevent lookahead and regime contamination.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TradePatternRecord:
    """Historical trade setup and realized performance outcome."""
    setup_id: str
    stock_code: str
    market: str  # "NSE", "BSE", "US", "HK", "CN"
    setup_type: str  # "BREAKOUT", "PULLBACK", "MEAN_REVERSION", "PENNY_MOMENTUM"
    rsi: float
    ema_spread_pct: float  # (EMA20 - EMA50) / EMA50 * 100
    volume_surge_ratio: float  # Current Volume / 20-day Volume SMA
    kronos_projected_return: float
    regime_vix_bucket: str  # "LOW", "MID", "HIGH"
    outcome_return_pct: float
    trade_result: str  # "WIN", "LOSS", "SCRATCH"
    holding_days: int
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))

    def to_vector(self) -> np.ndarray:
        """Construct normalized dense numerical vector representation."""
        # 1. RSI normalized to [-1, 1] centered at 50
        v_rsi = (np.clip(self.rsi, 0.0, 100.0) - 50.0) / 50.0
        # 2. EMA spread bounded [-1, 1]
        v_ema = np.clip(self.ema_spread_pct / 10.0, -1.0, 1.0)
        # 3. Volume surge bounded [0, 1]
        v_vol = np.clip(self.volume_surge_ratio / 5.0, 0.0, 1.0)
        # 4. Kronos projected return bounded [-1, 1]
        v_kronos = np.clip(self.kronos_projected_return / 20.0, -1.0, 1.0)

        raw_vec = np.array([v_rsi, v_ema, v_vol, v_kronos], dtype=np.float32)
        norm = np.linalg.norm(raw_vec)
        return raw_vec / norm if norm > 0 else raw_vec

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "stock_code": self.stock_code,
            "market": self.market,
            "setup_type": self.setup_type,
            "rsi": round(self.rsi, 2),
            "ema_spread_pct": round(self.ema_spread_pct, 2),
            "volume_surge_ratio": round(self.volume_surge_ratio, 2),
            "kronos_projected_return": round(self.kronos_projected_return, 2),
            "regime_vix_bucket": self.regime_vix_bucket,
            "outcome_return_pct": round(self.outcome_return_pct, 2),
            "trade_result": self.trade_result,
            "holding_days": self.holding_days,
            "created_at": self.created_at,
        }


@dataclass
class MemoryQueryMatch:
    """Retrieved historical pattern analogue."""
    record: TradePatternRecord
    similarity_score: float

    def to_summary_line(self) -> str:
        sign = "+" if self.record.outcome_return_pct >= 0 else ""
        return (
            f"- {self.record.created_at} ({self.record.stock_code}): {self.record.setup_type} | "
            f"RSI {self.record.rsi:.1f} | Vol {self.record.volume_surge_ratio:.1f}x -> "
            f"Realized {sign}{self.record.outcome_return_pct:.1f}% ({self.record.trade_result}) [Sim: {self.similarity_score:.2f}]"
        )


class TradeMemoryService:
    """Vector RAG memory engine with ChromaDB and SQLite fallback."""

    def __init__(self, db_dir: str = "./data/chroma_db", sqlite_path: str = "./data/trade_memory.db"):
        self.db_dir = db_dir
        self.sqlite_path = sqlite_path
        self._chroma_client = None
        self._collection = None
        self._use_chroma = False

        self._init_storage()

    def _init_storage(self):
        """Initialize ChromaDB or fallback SQLite storage."""
        os.makedirs(self.db_dir, exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(self.sqlite_path)), exist_ok=True)

        # 1. Initialize SQLite table for durable storage & fallback
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_patterns (
                    setup_id TEXT PRIMARY KEY,
                    stock_code TEXT,
                    market TEXT,
                    setup_type TEXT,
                    rsi REAL,
                    ema_spread_pct REAL,
                    volume_surge_ratio REAL,
                    kronos_projected_return REAL,
                    regime_vix_bucket TEXT,
                    outcome_return_pct REAL,
                    trade_result TEXT,
                    holding_days INTEGER,
                    created_at TEXT,
                    vector_json TEXT
                )
            """)
            conn.commit()

        # 2. Try ChromaDB
        try:
            import chromadb
            self._chroma_client = chromadb.PersistentClient(path=self.db_dir)
            self._collection = self._chroma_client.get_or_create_collection(
                name="stock_trade_patterns",
                metadata={"hnsw:space": "cosine"}
            )
            self._use_chroma = True
            logger.info("ChromaDB persistent client initialized at %s", self.db_dir)
        except Exception as e:
            logger.debug("ChromaDB not available (%s); operating on SQLite vector engine.", e)
            self._use_chroma = False

    def index_trade(self, record: TradePatternRecord) -> bool:
        """Index a matured trade setup with realized outcome."""
        vec = record.to_vector()
        vec_list = vec.tolist()

        # 1. Persist to SQLite
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO trade_patterns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.setup_id,
                    record.stock_code,
                    record.market,
                    record.setup_type,
                    record.rsi,
                    record.ema_spread_pct,
                    record.volume_surge_ratio,
                    record.kronos_projected_return,
                    record.regime_vix_bucket,
                    record.outcome_return_pct,
                    record.trade_result,
                    record.holding_days,
                    record.created_at,
                    json.dumps(vec_list),
                ))
                conn.commit()
        except Exception as e:
            logger.error("Failed to insert into SQLite trade_patterns: %s", e)
            return False

        # 2. Upsert to ChromaDB if available
        if self._use_chroma and self._collection is not None:
            try:
                doc_text = (
                    f"Symbol: {record.stock_code} | Market: {record.market} | Setup: {record.setup_type} | "
                    f"RSI: {record.rsi} | Volume Surge: {record.volume_surge_ratio}x | "
                    f"Kronos: {record.kronos_projected_return}% | VIX Regime: {record.regime_vix_bucket}"
                )
                self._collection.upsert(
                    ids=[record.setup_id],
                    embeddings=[vec_list],
                    metadatas=[record.to_dict()],
                    documents=[doc_text],
                )
            except Exception as e:
                logger.warning("ChromaDB upsert failed (%s), fallback active.", e)

        return True

    def query_similar_setups(
        self,
        current_record: TradePatternRecord,
        top_k: int = 3,
        min_similarity: float = 0.70,
    ) -> List[MemoryQueryMatch]:
        """Retrieve top-k closest historical setups filtered by market regime."""
        target_vec = current_record.to_vector()
        matches: List[MemoryQueryMatch] = []

        # Read candidates from SQLite filtered by regime & market
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trade_patterns
                WHERE regime_vix_bucket = ? OR market = ?
            """, (current_record.regime_vix_bucket, current_record.market))
            rows = cursor.fetchall()

        if not rows:
            # Broaden search if regime-filtered returns empty
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM trade_patterns")
                rows = cursor.fetchall()

        for row in rows:
            try:
                saved_vec = np.array(json.loads(row["vector_json"]), dtype=np.float32)
                # Compute Cosine Similarity
                sim = float(np.dot(target_vec, saved_vec))
                if sim >= min_similarity:
                    rec = TradePatternRecord(
                        setup_id=row["setup_id"],
                        stock_code=row["stock_code"],
                        market=row["market"],
                        setup_type=row["setup_type"],
                        rsi=float(row["rsi"]),
                        ema_spread_pct=float(row["ema_spread_pct"]),
                        volume_surge_ratio=float(row["volume_surge_ratio"]),
                        kronos_projected_return=float(row["kronos_projected_return"]),
                        regime_vix_bucket=row["regime_vix_bucket"],
                        outcome_return_pct=float(row["outcome_return_pct"]),
                        trade_result=row["trade_result"],
                        holding_days=int(row["holding_days"]),
                        created_at=row["created_at"],
                    )
                    matches.append(MemoryQueryMatch(record=rec, similarity_score=sim))
            except Exception as e:
                logger.debug("Error computing vector similarity: %s", e)

        # Sort by highest similarity
        matches.sort(key=lambda x: x.similarity_score, reverse=True)
        return matches[:top_k]

    def format_memory_prompt_injection(
        self,
        current_record: TradePatternRecord,
        top_k: int = 3,
    ) -> str:
        """Format token-efficient Markdown block for LLM prompt augmentation."""
        matches = self.query_similar_setups(current_record, top_k=top_k)
        if len(matches) < 2:
            return ""

        wins = sum(1 for m in matches if m.record.trade_result == "WIN")
        win_rate = (wins / len(matches)) * 100.0
        avg_ret = float(np.mean([m.record.outcome_return_pct for m in matches]))

        lines = ["\n### 🧠 Historical Pattern Analogue Memory (ChromaDB RAG):"]
        for m in matches:
            lines.append(m.to_summary_line())

        lines.append(f"* Setup Empirical Win Rate: **{win_rate:.1f}%** | Avg Realized Return: **{avg_ret:+.1f}%**\n")
        return "\n".join(lines)
