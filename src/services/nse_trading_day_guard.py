# -*- coding: utf-8 -*-
"""
====================================================================
NSE Trading Day Guard (fail-CLOSED market calendar gate)
====================================================================

Answers one question: "is the Indian equity market (NSE) actually open?"

Motivation: the ``com.dsa.intraday`` cron fired 4x/day across the weekend of
2026-08-22/23 and emitted live-looking BUY signals against Friday's stale
closing prices, because nothing consulted the calendar.

Design rules:
1. All "now"/"today" resolution goes through ``ZoneInfo("Asia/Kolkata")``.
   Never bare naive ``datetime.now()`` (that is only accidentally correct on an
   IST host, which is the bug in ``MarketSchedulerService.is_trading_day``).
2. A date is CLOSED if ANY source says closed (union of three checks):
   weekend, the curated ``NSE_TRADING_HOLIDAYS`` set below, or
   ``exchange_calendars`` XBOM. For 2026 the two curated sources agree
   exactly (16/16), so the union currently never disagrees with itself; it is
   kept as defence in depth for future years, where XBOM's bundled data may
   lag NSE's annual circular in either direction.

   Do NOT source holidays from ``market_scheduler_service.NSE_HOLIDAYS``. That
   set was checked against NSE's published 2026 calendar on 2026-08-24 and is
   wrong in both directions: 6 of its 14 entries are dates the market is
   actually OPEN (2026-02-18, 03-25, 06-17, 09-07, 10-21, 11-09 - several are
   the right festival on the wrong day, e.g. it places Holi on 03-25 when the
   real holiday is 03-03, and Bakri Id on 06-17 when the real date is 05-28),
   and it omits 9 genuine holidays. Using it here would have silently
   suppressed 6 real trading sessions a year.
3. FAIL-CLOSED. ``trading_calendar.is_market_open`` is deliberately fail-OPEN
   (returns True when the calendar library is unavailable); that polarity is
   wrong for a gate, so every exception here maps to CLOSED.
"""

import argparse
import logging
import sys
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# NSE equity trading holidays, verified 2026-08-24 against two independent
# published sources that agree exactly (cleartax.in/s/stock-market-holidays-2026
# and groww.in/p/nse-holidays), and cross-checked against exchange_calendars
# XBOM, which matches all 16.
#
# ANNUAL REFRESH REQUIRED. NSE publishes the next year's list by circular,
# typically in December. When this set has no entries for the current year the
# guard still holds - weekends and XBOM continue to apply - but a festival
# holiday XBOM has not picked up would be treated as a trading day. Re-verify
# each January against NSE's own circular, not a third-party summary.
NSE_TRADING_HOLIDAYS = frozenset({
    "2026-01-15",  # Municipal Corporation Election - Maharashtra
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Holi
    "2026-03-26",  # Shri Ram Navami
    "2026-03-31",  # Shri Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-28",  # Bakri Id
    "2026-06-26",  # Muharram
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali - Balipratipada
    "2026-11-24",  # Prakash Gurpurb Sri Guru Nanak Dev
    "2026-12-25",  # Christmas
})

# Muhurat trading: NSE holds a symbolic ~1-hour session on Sunday 2026-11-08
# (Diwali Laxmi Pujan). This guard deliberately reports that day CLOSED - the
# weekend check short-circuits first. That is correct for every caller today
# (the intraday scanner runs 09:45-14:15 IST; Muhurat is an evening session
# whose timings NSE announces separately), but it IS a real session this gate
# will refuse. Revisit if anything ever needs to trade it.
MUHURAT_SESSIONS = frozenset({"2026-11-08"})

# NSE regular equity session (IST wall clock).
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)

# How far back ``previous_nse_trading_day`` will walk before giving up. The longest
# closed stretch in the 2026 calendar is 4 days (Sat-Tue, Dussehra); 10 leaves room
# for a future year's longer block without ever returning a date from last month.
DEFAULT_TRADING_DAY_LOOKBACK_DAYS = 10


def _error_reason(exc: Exception) -> str:
    """Build a single-line ``calendar_error:<detail>`` reason string."""
    detail = " ".join(str(exc).split()) or type(exc).__name__
    return f"calendar_error:{detail}"


def _to_ist_datetime(value: Optional[datetime]) -> datetime:
    """Resolve a datetime in IST. Naive input is treated as IST wall clock."""
    if value is None:
        return datetime.now(IST)
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def _to_date(value: Optional[date]) -> date:
    """Resolve the date to check, defaulting to 'today' in IST."""
    if value is None:
        return datetime.now(IST).date()
    if isinstance(value, datetime):
        return _to_ist_datetime(value).date()
    return value


def is_nse_trading_day(check_date: Optional[date] = None) -> Tuple[bool, str]:
    """Return ``(is_open, reason)`` for a calendar date.

    reason is a short machine-ish string, e.g. ``"open"``, ``"weekend:Saturday"``,
    ``"holiday:2026-10-21"``, ``"xcals:closed"``, ``"calendar_error:<detail>"``.
    FAIL-CLOSED: any exception -> ``(False, "calendar_error:...")``.
    """
    try:
        target = _to_date(check_date)

        if target.weekday() >= 5:
            return False, f"weekend:{target.strftime('%A')}"

        iso = target.isoformat()
        if iso in NSE_TRADING_HOLIDAYS:
            return False, f"holiday:{iso}"

        # Imported lazily so that a broken/missing calendar dependency raises
        # here, inside the fail-closed guard, instead of at module import time.
        from src.core.trading_calendar import is_market_open

        if not is_market_open("in", target):
            return False, "xcals:closed"

        return True, "open"
    except Exception as exc:  # noqa: BLE001 - fail-closed by design
        reason = _error_reason(exc)
        logger.warning("nse_trading_day_guard.is_nse_trading_day fail-closed: %s", exc)
        return False, reason


def is_nse_session_now(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Return ``(in_session, reason)`` for an instant.

    True only if :func:`is_nse_trading_day` passes AND the IST wall-clock time is
    inside the regular session ``09:15 <= t < 15:30``. reasons e.g.
    ``"in_session"``, ``"before_open:08:14"``, ``"after_close:16:02"``, or the
    trading-day reason when the day itself is closed. FAIL-CLOSED.
    """
    try:
        current = _to_ist_datetime(now)

        is_open, reason = is_nse_trading_day(current.date())
        if not is_open:
            return False, reason

        clock = current.time()
        if clock < SESSION_OPEN:
            return False, f"before_open:{current.strftime('%H:%M')}"
        if clock >= SESSION_CLOSE:
            return False, f"after_close:{current.strftime('%H:%M')}"

        return True, "in_session"
    except Exception as exc:  # noqa: BLE001 - fail-closed by design
        reason = _error_reason(exc)
        logger.warning("nse_trading_day_guard.is_nse_session_now fail-closed: %s", exc)
        return False, reason


def previous_nse_trading_day(
    before: Optional[date] = None,
    *,
    max_lookback_days: int = DEFAULT_TRADING_DAY_LOOKBACK_DAYS,
) -> Optional[date]:
    """Return the most recent NSE trading day strictly before ``before``.

    ``before`` defaults to today in IST, so the answer is the latest session
    whose end-of-day data the exchange has already published - never today,
    whose bhavcopy does not exist until after the close.

    FAIL-CLOSED, like the rest of this module: returns ``None`` when no open
    day is found within ``max_lookback_days``. A holiday block longer than the
    window, or a calendar that cannot be consulted at all, must produce "I do
    not know" rather than a guessed date, because callers use this to decide
    which day's official data to fetch.
    """
    try:
        anchor = _to_date(before)
        for offset in range(1, max(1, int(max_lookback_days)) + 1):
            candidate = anchor - timedelta(days=offset)
            is_open, _reason = is_nse_trading_day(candidate)
            if is_open:
                return candidate
        logger.warning(
            "nse_trading_day_guard.previous_nse_trading_day found no open session in "
            "the %s days before %s",
            max_lookback_days,
            anchor.isoformat(),
        )
        return None
    except Exception as exc:  # noqa: BLE001 - fail-closed by design
        logger.warning(
            "nse_trading_day_guard.previous_nse_trading_day fail-closed: %s", exc
        )
        return None


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint. Exit code 0 when open, 1 when closed."""
    parser = argparse.ArgumentParser(
        prog="nse_trading_day_guard",
        description="Fail-closed gate: is the NSE actually open?",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Check the trading day only (default).",
    )
    mode.add_argument(
        "--check-session",
        action="store_true",
        help="Check the trading day AND the 09:15-15:30 IST session window.",
    )
    args = parser.parse_args(argv)

    if args.check_session:
        now_ist = datetime.now(IST)
        is_open, reason = is_nse_session_now(now_ist)
        stamp = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
        label = "NSE SESSION OPEN" if is_open else "NSE SESSION CLOSED"
    else:
        today = datetime.now(IST).date()
        is_open, reason = is_nse_trading_day(today)
        stamp = today.isoformat()
        label = "NSE TRADING DAY" if is_open else "NSE CLOSED"

    print(f"{label} | {stamp} | reason={reason}")
    return 0 if is_open else 1


if __name__ == "__main__":
    sys.exit(main())
