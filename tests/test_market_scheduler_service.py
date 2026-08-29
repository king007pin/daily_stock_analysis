# -*- coding: utf-8 -*-
"""
Tests for Automated Market Schedule Daemon & Holiday Guard Service
"""

import unittest
from datetime import datetime
from src.services.market_scheduler_service import MarketSchedulerService, MarketSessionState


class TestMarketSchedulerService(unittest.TestCase):
    """Test suite for MarketSchedulerService."""

    def setUp(self):
        self.scheduler = MarketSchedulerService()

    def test_holiday_and_weekend_filtering(self):
        """Verify weekends and Indian statutory holidays return is_trading_day=False."""
        # Weekend: Sunday, Aug 16, 2026
        sunday_dt = datetime(2026, 8, 16, 10, 0, 0)
        self.assertFalse(self.scheduler.is_trading_day(sunday_dt))

        # Statutory Holiday: Independence Day, Aug 15, 2026
        holiday_dt = datetime(2026, 8, 15, 10, 0, 0)
        self.assertFalse(self.scheduler.is_trading_day(holiday_dt))

        # Regular Trading Day: Monday, Aug 17, 2026
        trading_dt = datetime(2026, 8, 17, 10, 0, 0)
        self.assertTrue(self.scheduler.is_trading_day(trading_dt))

    def test_session_phase_transitions(self):
        """Verify correct phase classification across IST trading hours."""
        # Pre-Market at 08:50 IST
        pre_mkt = self.scheduler.get_current_session_phase(datetime(2026, 8, 17, 8, 50, 0))
        self.assertEqual(pre_mkt.session_phase, "PRE_MARKET")

        # Market Open at 10:30 IST
        mkt_open = self.scheduler.get_current_session_phase(datetime(2026, 8, 17, 10, 30, 0))
        self.assertEqual(mkt_open.session_phase, "MARKET_OPEN")

        # BTST Window at 14:00 IST
        btst_win = self.scheduler.get_current_session_phase(datetime(2026, 8, 17, 14, 0, 0))
        self.assertEqual(btst_win.session_phase, "MIDDAY_BTST")

        # Closing Auction CAS at 15:20 IST
        cas_win = self.scheduler.get_current_session_phase(datetime(2026, 8, 17, 15, 20, 0))
        self.assertEqual(cas_win.session_phase, "CLOSING_AUCTION_CAS")

        # Post-Market EOD at 16:00 IST
        eod_win = self.scheduler.get_current_session_phase(datetime(2026, 8, 17, 16, 0, 0))
        self.assertEqual(eod_win.session_phase, "POST_MARKET_EOD")


if __name__ == "__main__":
    unittest.main()
