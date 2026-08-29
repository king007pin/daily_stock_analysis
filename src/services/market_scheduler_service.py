# -*- coding: utf-8 -*-
"""
====================================================================
Automated Market Schedule Daemon & Holiday Guard Service
====================================================================

Implements:
1. Statutory 2026-2027 Indian NSE Market Holiday Calendar.
2. Stateful Market Session State Machine (Pre-Market, Open, BTST, CAS, EOD).
3. Automated Execution Scheduler.
"""

import logging
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Set, Optional
from datetime import datetime, time

logger = logging.getLogger(__name__)

# Statutory Indian NSE Market Holidays (2026-2027)
NSE_HOLIDAYS: Set[str] = {
    "2026-01-26",  # Republic Day
    "2026-02-18",  # Mahashivratri
    "2026-03-25",  # Holi
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-06-17",  # Bakri Id / Eid ul-Adha
    "2026-08-15",  # Independence Day
    "2026-09-07",  # Janmashtami
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-21",  # Dussehra
    "2026-11-09",  # Diwali Laxmi Pujan (Muhurat session evening only)
    "2026-11-10",  # Diwali Balipratipada
    "2026-12-25",  # Christmas
}


@dataclass
class MarketSessionState:
    current_time_str: str
    is_trading_day: bool
    holiday_name: Optional[str]
    session_phase: str  # 'PRE_MARKET', 'MARKET_OPEN', 'MIDDAY_BTST', 'CLOSING_AUCTION_CAS', 'POST_MARKET_EOD', 'MARKET_CLOSED'
    next_action_due: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MarketSchedulerService:
    """Stateful Market Scheduler with Indian Market Holiday Calendar."""

    def __init__(self):
        self.holidays = NSE_HOLIDAYS

    def is_trading_day(self, dt: Optional[datetime] = None) -> bool:
        """Returns True if the date is a weekday and not a statutory NSE holiday."""
        check_dt = dt or datetime.now()
        date_str = check_dt.strftime("%Y-%m-%d")
        if check_dt.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        if date_str in self.holidays:
            return False
        return True

    def get_current_session_phase(self, dt: Optional[datetime] = None) -> MarketSessionState:
        """Evaluates current market session phase based on IST trading hours."""
        now_dt = dt or datetime.now()
        date_str = now_dt.strftime("%Y-%m-%d")
        t = now_dt.time()

        if not self.is_trading_day(now_dt):
            holiday_str = "Statutory Holiday" if date_str in self.holidays else "Weekend"
            return MarketSessionState(
                current_time_str=now_dt.strftime("%Y-%m-%d %H:%M:%S IST"),
                is_trading_day=False,
                holiday_name=holiday_str,
                session_phase="MARKET_CLOSED",
                next_action_due="Wait for next trading session at 08:45 IST",
            )

        # Indian Market Trading Session Hours
        if time(8, 45) <= t < time(9, 7):
            phase = "PRE_MARKET"
            next_action = "Execute Pre-Market Gap Scan and Volatility Filter at 09:00 IST"
        elif time(9, 15) <= t < time(13, 30):
            phase = "MARKET_OPEN"
            next_action = "Monitor live momentum breakouts & intraday stop-losses"
        elif time(13, 30) <= t < time(15, 15):
            phase = "MIDDAY_BTST"
            next_action = "Scan and place BTST (Buy Today, Sell Tomorrow) 24h CNC swing orders"
        elif time(15, 15) <= t < time(15, 30):
            phase = "CLOSING_AUCTION_CAS"
            next_action = "Monitor discrete closing auction matching prices"
        elif time(15, 45) <= t < time(17, 0):
            phase = "POST_MARKET_EOD"
            next_action = "Trigger EOD Pipeline: FII/DII flow scraping & Vault Sync"
        else:
            phase = "MARKET_CLOSED"
            next_action = "Offline analysis & overnight factor calibration"

        return MarketSessionState(
            current_time_str=now_dt.strftime("%Y-%m-%d %H:%M:%S IST"),
            is_trading_day=True,
            holiday_name=None,
            session_phase=phase,
            next_action_due=next_action,
        )
