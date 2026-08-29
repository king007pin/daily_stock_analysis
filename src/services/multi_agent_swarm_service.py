# -*- coding: utf-8 -*-
"""
====================================================================
Multi-Agent Trading Swarm Consensus Engine
====================================================================

Implements:
1. 5 Specialized Agent Personas (Graham, Ackman, Cathie, Risk Manager, Orchestrator).
2. Hardened Absolute Risk Manager Veto Power (Circuit buffer & ATR bounds).
3. 4/5 Supermajority Voting Resolution.
"""

import logging
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentVote:
    agent_name: str
    vote: str  # 'BUY', 'NEUTRAL', 'VETO_REJECT'
    confidence: float
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SwarmConsensusResult:
    symbol: str
    decision: str  # 'APPROVED_BUY', 'REJECTED_VETO', 'REJECTED_INSUFFICIENT_VOTES'
    buy_votes: int
    veto_triggered: bool
    veto_reason: Optional[str]
    votes: Dict[str, AgentVote]
    consensus_score: float

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["votes"] = {k: v.to_dict() for k, v in self.votes.items()}
        return d


class MultiAgentSwarmService:
    """Multi-Agent Trading Swarm Consensus Engine with Absolute Risk Veto."""

    def __init__(self, required_approval_votes: int = 4):
        self.required_votes = required_approval_votes

    def evaluate_swarm_consensus(
        self,
        symbol: str,
        pe_ratio: Optional[float],
        volume_surge_ratio: float,
        price_vs_vwap_pct: float,
        rsi: float,
        atr_pct: float,
        circuit_buffer_pct: float,
        has_news_catalyst: bool = False,
    ) -> SwarmConsensusResult:
        """
        Executes 5-agent voting swarm with strict Risk Manager veto.
        """
        votes: Dict[str, AgentVote] = {}

        # Safely parse pe_ratio
        clean_pe: Optional[float] = None
        if pe_ratio is not None:
            try:
                clean_pe = float(str(pe_ratio).strip().replace(",", ""))
            except (ValueError, TypeError):
                clean_pe = None

        # 1. Ben Graham Agent (Value / Balance Sheet Safety)
        if clean_pe and 0 < clean_pe <= 30.0:
            votes["graham"] = AgentVote("Graham", "BUY", 0.85, f"Fair/Low P/E valuation of {clean_pe:.1f}")
        elif clean_pe is None or clean_pe > 50.0:
            votes["graham"] = AgentVote("Graham", "NEUTRAL", 0.40, "P/E high, negative, or N/A; fundamental safety moderate")
        else:
            votes["graham"] = AgentVote("Graham", "BUY", 0.70, f"Acceptable valuation at P/E {clean_pe:.1f}")

        # 2. Bill Ackman Agent (Volume & Catalyst Surge)
        if volume_surge_ratio >= 1.5 or has_news_catalyst:
            votes["ackman"] = AgentVote("Ackman", "BUY", 0.90, f"Volume expansion {volume_surge_ratio:.1f}x with catalyst")
        else:
            votes["ackman"] = AgentVote("Ackman", "NEUTRAL", 0.50, "Average volume turnover; lacking institutional catalyst")

        # 3. Cathie Wood Agent (Momentum & Relative Strength)
        if price_vs_vwap_pct >= 0.5 and 50.0 <= rsi <= 75.0:
            votes["cathie"] = AgentVote("Cathie", "BUY", 0.90, f"Strong momentum above VWAP (+{price_vs_vwap_pct:.1f}%), RSI={rsi:.1f}")
        else:
            votes["cathie"] = AgentVote("Cathie", "NEUTRAL", 0.45, f"Subdued momentum; RSI={rsi:.1f}")

        # 4. Master Orchestrator Agent (Cross-Factor Alignment)
        if price_vs_vwap_pct >= 0.0 and volume_surge_ratio >= 1.1:
            votes["orchestrator"] = AgentVote("Orchestrator", "BUY", 0.80, "Technical and factor alignment positive")
        else:
            votes["orchestrator"] = AgentVote("Orchestrator", "NEUTRAL", 0.50, "Factor divergence detected")

        # 5. Risk Manager Agent (Capital Preservation & ABSOLUTE VETO)
        veto_triggered = False
        veto_reason = None

        if circuit_buffer_pct <= 2.0:
            veto_triggered = True
            veto_reason = f"Circuit limit breach risk! Buffer is only {circuit_buffer_pct:.1f}% (Min required: 2.0%)"
            votes["risk_manager"] = AgentVote("RiskManager", "VETO_REJECT", 1.0, veto_reason)
        elif atr_pct > 6.0:
            veto_triggered = True
            veto_reason = f"Excessive intraday volatility! ATR is {atr_pct:.1f}% (Max allowed: 6.0%)"
            votes["risk_manager"] = AgentVote("RiskManager", "VETO_REJECT", 1.0, veto_reason)
        else:
            votes["risk_manager"] = AgentVote("RiskManager", "BUY", 0.85, f"Risk parameters safe: ATR={atr_pct:.1f}%, Circuit buffer={circuit_buffer_pct:.1f}%")

        # Tabulate Votes
        buy_count = sum(1 for v in votes.values() if v.vote == "BUY")
        avg_confidence = sum(v.confidence for v in votes.values()) / 5.0

        if veto_triggered:
            decision = "REJECTED_VETO"
        elif buy_count >= self.required_votes:
            decision = "APPROVED_BUY"
        else:
            decision = "REJECTED_INSUFFICIENT_VOTES"

        return SwarmConsensusResult(
            symbol=symbol,
            decision=decision,
            buy_votes=buy_count,
            veto_triggered=veto_triggered,
            veto_reason=veto_reason,
            votes=votes,
            consensus_score=round(avg_confidence * 100.0, 1),
        )
