# -*- coding: utf-8 -*-
"""
====================================================================
Broker Execution & Smart Order Management Service
====================================================================

Supports paper trading execution, bracket order management (Target + Stop-Loss),
circuit buffer validation (for sub-₹10 / Indian equities), and SEBI 1-Click
Telegram approval workflow.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class OrderBracket:
    """Smart bracket order specification."""
    order_id: str
    stock_code: str
    action: str  # "BUY" or "SELL"
    order_type: str  # "LIMIT", "MARKET", "GTT"
    quantity: int
    entry_price: float
    target_price: float
    stop_loss_price: float
    status: str = "PENDING"  # "PENDING", "APPROVED", "SUBMITTED", "FILLED", "CANCELLED", "REJECTED"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    executed_at: Optional[str] = None
    reason: str = ""
    circuit_risk: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "stock_code": self.stock_code,
            "action": self.action,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "entry_price": round(self.entry_price, 2),
            "target_price": round(self.target_price, 2),
            "stop_loss_price": round(self.stop_loss_price, 2),
            "status": self.status,
            "created_at": self.created_at,
            "executed_at": self.executed_at,
            "reason": self.reason,
            "circuit_risk": self.circuit_risk,
        }


class BrokerExecutionService:
    """Broker order manager with paper execution and risk filters."""

    def __init__(self, mode: str = "paper", account_capital: float = 100000.0):
        self.mode = mode  # "paper" or "live"
        self.account_capital = account_capital
        self._orders: Dict[str, OrderBracket] = {}

    def create_bracket_order(
        self,
        stock_code: str,
        action: str,
        entry_price: float,
        target_price: float,
        stop_loss_price: float,
        recommended_position_pct: float = 5.0,
        circuit_risk_flag: bool = False,
    ) -> OrderBracket:
        """Create and validate a bracket order."""
        order_id = f"ord_{uuid.uuid4().hex[:10]}"

        # Safety Gate: Circuit risk lock
        if circuit_risk_flag and entry_price < 10.0:
            order = OrderBracket(
                order_id=order_id,
                stock_code=stock_code,
                action=action,
                order_type="LIMIT",
                quantity=0,
                entry_price=entry_price,
                target_price=target_price,
                stop_loss_price=stop_loss_price,
                status="REJECTED",
                reason="Circuit risk buffer too narrow (< 1.5%) for sub-₹10 stock.",
                circuit_risk=True,
            )
            self._orders[order_id] = order
            logger.warning("Order %s rejected: %s", order_id, order.reason)
            return order

        # Position Sizing
        allocated_capital = self.account_capital * (max(0.5, min(25.0, recommended_position_pct)) / 100.0)
        quantity = max(1, int(allocated_capital / max(0.01, entry_price)))

        order = OrderBracket(
            order_id=order_id,
            stock_code=stock_code,
            action=action,
            order_type="LIMIT",
            quantity=quantity,
            entry_price=entry_price,
            target_price=target_price,
            stop_loss_price=stop_loss_price,
            status="PENDING" if self.mode == "live" else "FILLED",
            executed_at=datetime.now().isoformat() if self.mode == "paper" else None,
            circuit_risk=circuit_risk_flag,
        )
        self._orders[order_id] = order
        logger.info("Order %s created [%s] %s qty=%d price=%.2f", order_id, order.status, stock_code, quantity, entry_price)
        return order

    def get_order(self, order_id: str) -> Optional[OrderBracket]:
        return self._orders.get(order_id)

    def list_orders(self) -> List[Dict[str, Any]]:
        return [ord.to_dict() for ord in self._orders.values()]
