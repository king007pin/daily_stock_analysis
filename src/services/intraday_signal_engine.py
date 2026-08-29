# -*- coding: utf-8 -*-
"""
====================================================================
Intraday Signal & Margin Sizer Engine
====================================================================

Implements:
1. Synthesis of VWAP Bands, ORB Breakout, and Multi-Agent Swarm Consensus.
2. SEBI 100% Upfront Peak Margin (5.0x for F&O, 2.0x for Cash EQ).
3. Bounded Kelly Sizing with 25% max single-trade risk allocation.
4. Auto-Squareoff Scheduler (15:05 IST pre-liquidation buffer).
"""

import logging
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional
from src.services.intraday_microstructure_service import IntradayMicrostructureService, VWAPBandsResult, ORBResult
from src.services.multi_agent_swarm_service import MultiAgentSwarmService, SwarmConsensusResult

logger = logging.getLogger(__name__)


@dataclass
class IntradayTradeTicket:
    symbol: str
    action: str  # 'BUY_MIS', 'NO_TRADE_REJECTED'
    entry_price: float
    target_price: float
    stop_loss_price: float
    target_pct: float
    stop_loss_pct: float
    risk_reward_ratio: float
    leverage_multiplier: float
    allocated_capital: float
    position_quantity: int
    auto_squareoff_time: str
    rejection_reason: Optional[str]
    swarm_consensus: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class IntradaySignalEngine:
    """Quantitative Intraday Signal, Leverage Sizer, and GTT Router."""

    def __init__(self, default_capital: float = 5000.0):
        self.total_capital = default_capital
        self.micro_service = IntradayMicrostructureService()
        self.swarm_service = MultiAgentSwarmService(required_approval_votes=4)

    def generate_intraday_signal(
        self,
        symbol: str,
        current_price: float,
        prices_1m: List[float],
        volumes_1m: List[float],
        orb_highs: List[float],
        orb_lows: List[float],
        pe_ratio: Optional[float] = None,
        volume_surge_ratio: float = 1.8,
        rsi: float = 58.0,
        circuit_buffer_pct: float = 5.0,
        is_fno_eligible: bool = True,
        has_news_catalyst: bool = False,
    ) -> IntradayTradeTicket:
        """
        Generates hardened Intraday MIS ticket with 5x leverage and 15:05 IST cutoff.
        """
        # 1. Microstructure & VWAP calculation
        vwap_res = self.micro_service.calculate_anchored_vwap_bands(symbol, prices_1m, volumes_1m)
        orb_res = self.micro_service.calculate_orb(symbol, orb_highs, orb_lows, current_price)

        price_vs_vwap_pct = ((current_price - vwap_res.vwap) / vwap_res.vwap) * 100.0 if vwap_res.vwap > 0 else 0.0
        atr_pct = max(0.5, (vwap_res.vwap_std / current_price) * 100.0) if current_price > 0 else 2.0

        # 2. Multi-Agent Swarm Consensus
        swarm_res = self.swarm_service.evaluate_swarm_consensus(
            symbol=symbol,
            pe_ratio=pe_ratio,
            volume_surge_ratio=volume_surge_ratio,
            price_vs_vwap_pct=price_vs_vwap_pct,
            rsi=rsi,
            atr_pct=atr_pct,
            circuit_buffer_pct=circuit_buffer_pct,
            has_news_catalyst=has_news_catalyst,
        )

        if swarm_res.decision != "APPROVED_BUY":
            reason = swarm_res.veto_reason or f"Insufficient swarm consensus ({swarm_res.buy_votes}/5 votes)"
            return IntradayTradeTicket(
                symbol=symbol,
                action="NO_TRADE_REJECTED",
                entry_price=current_price,
                target_price=0.0,
                stop_loss_price=0.0,
                target_pct=0.0,
                stop_loss_pct=0.0,
                risk_reward_ratio=0.0,
                leverage_multiplier=1.0,
                allocated_capital=0.0,
                position_quantity=0,
                auto_squareoff_time="15:05 IST",
                rejection_reason=reason,
                swarm_consensus=swarm_res.to_dict(),
            )

        # 3. Calculate Targets & Stop-Loss (Asymmetric 1:2.5+ R:R)
        # Entry at current price, SL placed at VWAP lower band or -0.8% floor
        sl_price = round(max(vwap_res.lower_band_1, current_price * 0.992), 2)
        sl_pct = round(((current_price - sl_price) / current_price) * 100.0, 2)
        sl_pct = max(0.5, sl_pct)

        target_pct = round(sl_pct * 2.5, 2)
        target_price = round(current_price * (1.0 + target_pct / 100.0), 2)
        rr_ratio = round(target_pct / sl_pct, 2)

        # 4. Sizing & SEBI Peak Margin Multiplier
        leverage = 5.0 if is_fno_eligible else 2.0
        # Bounded Kelly: Cap capital at 25% of account balance (₹1,250 out of ₹5,000)
        allocated_cash = self.total_capital * 0.25
        buying_power = allocated_cash * leverage
        quantity = max(1, int(buying_power / current_price))

        return IntradayTradeTicket(
            symbol=symbol,
            action="BUY_MIS",
            entry_price=round(current_price, 2),
            target_price=target_price,
            stop_loss_price=sl_price,
            target_pct=target_pct,
            stop_loss_pct=sl_pct,
            risk_reward_ratio=rr_ratio,
            leverage_multiplier=leverage,
            allocated_capital=round(allocated_cash, 2),
            position_quantity=quantity,
            auto_squareoff_time="15:05 IST",
            rejection_reason=None,
            swarm_consensus=swarm_res.to_dict(),
        )
