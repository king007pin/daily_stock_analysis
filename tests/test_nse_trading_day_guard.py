# -*- coding: utf-8 -*-
"""
Tests for src/services/nse_trading_day_guard.py - the fail-CLOSED NSE market-calendar gate.

Background (the incident these tests exist to prevent regressing):
a cron job fired on Saturday 2026-08-22 and Sunday 2026-08-23 and emitted intraday BUY
signals with share quantities priced off Friday's stale close. The guard must refuse to
report "open" unless it can positively prove the market is open.

Contract under test:
    is_nse_trading_day(check_date: date | None = None) -> tuple[bool, str]
    is_nse_session_now(now: datetime | None = None) -> tuple[bool, str]
    main(argv: list[str] | None = None) -> int      # --check / --check-session; 0 open, 1 closed

Fully offline and deterministic: no network, no live calendar downloads, and no test reads
the real clock - every time-dependent assertion injects an explicit date/datetime.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.core import trading_calendar
from src.services import nse_trading_day_guard as guard

IST = ZoneInfo("Asia/Kolkata")

# Monday 2026-08-24: a weekday, absent from the holiday set, and a real XBOM session.
# Used as the "known open" backdrop so session-window tests isolate the time-of-day logic.
KNOWN_OPEN_DAY = date(2026, 8, 24)


def _assert_closed(result, label: str) -> None:
    """A guard result must be a (bool, str) pair; assert it says CLOSED with a reason."""
    assert isinstance(result, tuple) and len(result) == 2, f"{label}: expected a 2-tuple, got {result!r}"
    is_open, reason = result
    assert is_open is False, f"{label}: expected CLOSED, got open (reason={reason!r})"
    assert isinstance(reason, str) and reason.strip(), f"{label}: expected a non-empty reason string"


def _assert_open(result, label: str) -> None:
    assert isinstance(result, tuple) and len(result) == 2, f"{label}: expected a 2-tuple, got {result!r}"
    is_open, reason = result
    assert is_open is True, f"{label}: expected OPEN, got closed (reason={reason!r})"
    assert isinstance(reason, str), f"{label}: expected a reason string"


class TestIncidentRegression:
    """The exact dates that produced bogus weekend BUY signals. Permanent test cases."""

    def test_saturday_2026_08_22_is_closed(self):
        _assert_closed(guard.is_nse_trading_day(date(2026, 8, 22)), "Saturday 2026-08-22")

    def test_sunday_2026_08_23_is_closed(self):
        _assert_closed(guard.is_nse_trading_day(date(2026, 8, 23)), "Sunday 2026-08-23")

    def test_monday_2026_08_24_is_open(self):
        _assert_open(guard.is_nse_trading_day(date(2026, 8, 24)), "Monday 2026-08-24")


class TestFabricatedHolidaysAreTradingDays:
    """
    Dates that ``market_scheduler_service.NSE_HOLIDAYS`` claims are holidays but which
    the market is actually OPEN on.

    That set was checked against NSE's published 2026 calendar on 2026-08-24 (two
    independent sources agreeing, plus exchange_calendars XBOM matching both) and is
    wrong in both directions: 6 of its 14 entries are invented dates - several the right
    festival on the wrong day, e.g. Holi placed on 03-25 when the real holiday is 03-03,
    and Bakri Id on 06-17 when the real date is 05-28 - while 9 genuine holidays are
    missing from it entirely.

    An earlier revision of this guard ORed that set into the decision, which would have
    silently suppressed all six of these real trading sessions every year. These cases
    pin the corrected behaviour and would fail immediately if anyone reintroduced the
    fabricated list as a holiday source.
    """

    FABRICATED_HOLIDAYS_THAT_ARE_REALLY_TRADING_DAYS = [
        date(2026, 2, 18),   # set claims Mahashivratri; real Mahashivratri is Sun 2026-02-15
        date(2026, 3, 25),   # set claims Holi;          real Holi is Tue 2026-03-03
        date(2026, 6, 17),   # set claims Bakri Id;      real Bakri Id is Thu 2026-05-28
        date(2026, 9, 7),    # set claims Janmashtami;   not an NSE holiday at all
        date(2026, 10, 21),  # set claims Dussehra;      real Dussehra is Tue 2026-10-20
        date(2026, 11, 9),   # set claims Diwali;        real Muhurat is Sun 2026-11-08
    ]

    @pytest.mark.parametrize(
        "trading_day", FABRICATED_HOLIDAYS_THAT_ARE_REALLY_TRADING_DAYS, ids=lambda d: d.isoformat()
    )
    def test_fabricated_holiday_is_actually_open(self, trading_day):
        assert trading_day.weekday() < 5, "precondition: this date must be a weekday for the test to mean anything"
        _assert_open(guard.is_nse_trading_day(trading_day), f"real trading day {trading_day.isoformat()}")

    def test_guard_does_not_import_the_fabricated_set(self):
        """The guard must own its verified list, not borrow the known-bad one."""
        assert not hasattr(guard, "NSE_HOLIDAYS"), (
            "guard re-imported market_scheduler_service.NSE_HOLIDAYS - that set is "
            "verified-wrong and would close 6 real trading days a year"
        )


# Parametrized over the guard's own verified set so the holiday list is never retyped
# here: correcting the annual list automatically corrects the test cases.
@pytest.mark.parametrize("holiday_str", sorted(guard.NSE_TRADING_HOLIDAYS))
def test_every_nse_holiday_is_closed(holiday_str):
    _assert_closed(
        guard.is_nse_trading_day(date.fromisoformat(holiday_str)),
        f"NSE_TRADING_HOLIDAYS entry {holiday_str}",
    )


def test_nse_holidays_set_is_non_empty():
    """Guards the parametrization above from silently degenerating into zero test cases."""
    assert guard.NSE_TRADING_HOLIDAYS, "NSE_TRADING_HOLIDAYS is empty - the parametrization would cover nothing"


def test_verified_holiday_set_matches_exchange_calendars():
    """
    Cross-check the curated list against XBOM. For 2026 they agree 16/16.

    A divergence here is not automatically a bug - it means NSE's circular and XBOM's
    bundled data disagree, which is exactly why the guard ORs both. But it should never
    happen silently: if this fails, re-verify against NSE's own circular before editing
    either source.
    """
    from src.core.trading_calendar import is_market_open

    disagreements = [
        holiday_str
        for holiday_str in sorted(guard.NSE_TRADING_HOLIDAYS)
        if is_market_open("in", date.fromisoformat(holiday_str))
    ]
    assert not disagreements, f"XBOM reports these curated NSE holidays as open sessions: {disagreements}"


class TestFailClosed:
    """
    The most important behaviour in this module.

    src.core.trading_calendar.is_market_open is deliberately fail-OPEN: its own docstring says
    "Fail-open: returns True if exchange-calendars unavailable or date out of range", and it
    swallows every exception into `return True`. That polarity is correct for its callers and
    catastrophic for this one - a fail-open market gate is what lets a cron job trade a stale
    Friday close on a Sunday.

    So nse_trading_day_guard deliberately INVERTS that polarity by wrapping the call and
    treating any exception as CLOSED. These tests pin the inversion. If someone "simplifies"
    the try/except away, or returns True on error to match the callee's convention, these fail.
    """

    @staticmethod
    def _explode(*args, **kwargs):
        raise RuntimeError("exchange_calendars blew up (simulated)")

    @pytest.fixture
    def calendar_raises(self, monkeypatch):
        """Make the underlying calendar lookup raise, however the guard imported it."""
        monkeypatch.setattr(trading_calendar, "is_market_open", self._explode)
        # Covers `from src.core.trading_calendar import is_market_open` style imports too.
        if hasattr(guard, "is_market_open"):
            monkeypatch.setattr(guard, "is_market_open", self._explode, raising=False)
        return self._explode

    def test_trading_day_is_closed_when_calendar_raises(self, calendar_raises):
        # KNOWN_OPEN_DAY is a genuine trading day: without the exception this returns open.
        # With the exception it MUST flip to closed, not fall through to the callee's fail-open True.
        _assert_closed(guard.is_nse_trading_day(KNOWN_OPEN_DAY), "calendar raising on a real trading day")

    def test_session_is_closed_when_calendar_raises(self, calendar_raises):
        mid_session = datetime(2026, 8, 24, 11, 0, tzinfo=IST)
        _assert_closed(guard.is_nse_session_now(mid_session), "calendar raising mid-session")

    def test_weekend_still_closed_when_calendar_raises(self, calendar_raises):
        _assert_closed(guard.is_nse_trading_day(date(2026, 8, 23)), "calendar raising on a Sunday")

    def test_calendar_returning_garbage_does_not_open_the_gate(self, monkeypatch):
        """A non-bool truthy return from the calendar must not be laundered into 'open' on a weekend."""
        monkeypatch.setattr(trading_calendar, "is_market_open", lambda *a, **k: "yes")
        if hasattr(guard, "is_market_open"):
            monkeypatch.setattr(guard, "is_market_open", lambda *a, **k: "yes", raising=False)
        _assert_closed(guard.is_nse_trading_day(date(2026, 8, 22)), "garbage calendar return on a Saturday")


class TestSessionWindowBoundaries:
    """
    Session window is 09:15 <= t < 15:30 IST. All datetimes are injected and timezone-aware,
    so these assertions are identical at every wall-clock moment and on any host timezone.
    """

    @pytest.mark.parametrize(
        "hour, minute, expect_open",
        [
            (9, 14, False),   # one minute before the open - closed
            (9, 15, True),    # inclusive lower bound - open
            (15, 29, True),   # last minute inside the window - open
            (15, 30, False),  # exclusive upper bound - closed
        ],
        ids=["09:14-closed", "09:15-open", "15:29-open", "15:30-closed"],
    )
    def test_boundaries_on_a_known_open_day(self, hour, minute, expect_open):
        now = datetime(KNOWN_OPEN_DAY.year, KNOWN_OPEN_DAY.month, KNOWN_OPEN_DAY.day, hour, minute, tzinfo=IST)
        label = f"{KNOWN_OPEN_DAY.isoformat()} {hour:02d}:{minute:02d} IST"
        if expect_open:
            _assert_open(guard.is_nse_session_now(now), label)
        else:
            _assert_closed(guard.is_nse_session_now(now), label)

    @pytest.mark.parametrize(
        "hour, minute",
        [(0, 0), (9, 15), (12, 0), (15, 29), (23, 59)],
        ids=["00:00", "09:15", "12:00", "15:29", "23:59"],
    )
    def test_saturday_is_closed_at_every_time_of_day(self, hour, minute):
        now = datetime(2026, 8, 22, hour, minute, tzinfo=IST)
        _assert_closed(guard.is_nse_session_now(now), f"Saturday 2026-08-22 {hour:02d}:{minute:02d} IST")

    @pytest.mark.parametrize(
        "hour, minute",
        [(0, 0), (9, 15), (12, 0), (15, 29), (23, 59)],
        ids=["00:00", "09:15", "12:00", "15:29", "23:59"],
    )
    def test_sunday_is_closed_at_every_time_of_day(self, hour, minute):
        now = datetime(2026, 8, 23, hour, minute, tzinfo=IST)
        _assert_closed(guard.is_nse_session_now(now), f"Sunday 2026-08-23 {hour:02d}:{minute:02d} IST")

    def test_holiday_is_closed_inside_the_session_window(self):
        # Independence Day 2026-08-15 falls on a Saturday; Ganesh Chaturthi 2026-09-14 is a
        # Monday, so it exercises the holiday branch rather than the weekday branch.
        now = datetime(2026, 9, 14, 11, 0, tzinfo=IST)
        _assert_closed(guard.is_nse_session_now(now), "Ganesh Chaturthi 2026-09-14 11:00 IST")


class TestCli:
    """
    Exit codes are what cron reads: 0 = open, 1 = closed.

    The loose assertions below hold regardless of what day the suite happens to run on,
    so this file never breaks on a weekend.
    """

    @pytest.mark.parametrize("flag", ["--check", "--check-session"])
    def test_cli_returns_a_valid_exit_code_on_any_day(self, flag):
        rc = guard.main([flag])
        assert isinstance(rc, int) and not isinstance(rc, bool), f"{flag}: expected an int exit code, got {rc!r}"
        assert rc in (0, 1), f"{flag}: expected exit code 0 or 1, got {rc}"

    def test_check_maps_open_to_zero_and_closed_to_one(self, monkeypatch):
        """Pins the polarity: a non-zero exit must mean 'do not trade', never the reverse."""
        monkeypatch.setattr(guard, "is_nse_trading_day", lambda *a, **k: (True, "open (stubbed)"))
        assert guard.main(["--check"]) == 0

        monkeypatch.setattr(guard, "is_nse_trading_day", lambda *a, **k: (False, "closed (stubbed)"))
        assert guard.main(["--check"]) == 1

    def test_check_session_maps_open_to_zero_and_closed_to_one(self, monkeypatch):
        monkeypatch.setattr(guard, "is_nse_session_now", lambda *a, **k: (True, "in session (stubbed)"))
        assert guard.main(["--check-session"]) == 0

        monkeypatch.setattr(guard, "is_nse_session_now", lambda *a, **k: (False, "out of session (stubbed)"))
        assert guard.main(["--check-session"]) == 1


class TestPreviousTradingDay:
    """
    ``previous_nse_trading_day`` names the last session whose end-of-day data the
    exchange has already published. The bhavcopy reconciliation scheduled in
    ``main._run_bhavcopy_reconciliation`` uses it to decide which day's official
    file to fetch, so "today" and "a guessed date" are both wrong answers.

    Deterministic: every case injects an explicit ``before`` date.
    """

    def test_monday_looks_back_to_the_friday_session(self):
        assert guard.previous_nse_trading_day(KNOWN_OPEN_DAY) == date(2026, 8, 21)

    def test_it_never_returns_the_anchor_itself(self):
        """Strictly before: today's bhavcopy does not exist until after the close."""
        result = guard.previous_nse_trading_day(KNOWN_OPEN_DAY)
        assert result is not None and result < KNOWN_OPEN_DAY

    def test_it_skips_a_holiday_that_falls_on_a_weekday(self):
        """2026-10-20 is Dussehra, so the Wednesday after it looks back to Monday."""
        assert guard.previous_nse_trading_day(date(2026, 10, 21)) == date(2026, 10, 19)

    def test_it_skips_a_holiday_and_the_weekend_behind_it(self):
        """2026-01-26 is Republic Day (Monday), so Tuesday looks back to Friday 01-23."""
        assert guard.previous_nse_trading_day(date(2026, 1, 27)) == date(2026, 1, 23)

    def test_a_saturday_anchor_still_finds_the_friday_session(self):
        """The anchor itself need not be a trading day - the cron may fire on a weekend."""
        assert guard.previous_nse_trading_day(date(2026, 8, 22)) == date(2026, 8, 21)

    def test_every_result_is_itself_an_open_session(self):
        for anchor in (KNOWN_OPEN_DAY, date(2026, 10, 21), date(2026, 1, 27), date(2026, 8, 22)):
            result = guard.previous_nse_trading_day(anchor)
            assert result is not None, f"no session found before {anchor.isoformat()}"
            _assert_open(guard.is_nse_trading_day(result), f"result for anchor {anchor.isoformat()}")

    def test_no_open_day_in_the_window_returns_none(self, monkeypatch):
        """Fail-CLOSED: an unbroken closed stretch must produce 'I do not know', not a date."""
        monkeypatch.setattr(guard, "is_nse_trading_day", lambda *a, **k: (False, "closed (stubbed)"))
        assert guard.previous_nse_trading_day(KNOWN_OPEN_DAY) is None

    def test_a_broken_calendar_returns_none(self, monkeypatch):
        """``is_nse_trading_day`` is already fail-closed; the walk must not paper over it."""
        def explode(*_args, **_kwargs):
            raise RuntimeError("calendar data unavailable")

        monkeypatch.setattr(trading_calendar, "is_market_open", explode)
        assert guard.previous_nse_trading_day(KNOWN_OPEN_DAY) is None

    def test_lookback_window_is_respected(self, monkeypatch):
        """A window too short to reach the Friday session returns None rather than reaching past it."""
        assert guard.previous_nse_trading_day(KNOWN_OPEN_DAY, max_lookback_days=2) is None
        assert guard.previous_nse_trading_day(KNOWN_OPEN_DAY, max_lookback_days=3) == date(2026, 8, 21)

    def test_default_anchor_is_today_in_ist(self, monkeypatch):
        """``before=None`` must mean 'today in IST', not a bare naive ``date.today()``.

        The clock is frozen by injection rather than read: 2026-08-24 18:00 IST is
        22:30 on 2026-08-23 UTC, so a host running in UTC would answer Thursday
        2026-08-20 here if the helper ever resolved "today" outside IST.
        """
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 24, 18, 0, tzinfo=IST)

        monkeypatch.setattr(guard, "datetime", FrozenDatetime)
        assert guard.previous_nse_trading_day() == date(2026, 8, 21)

    def test_the_default_lookback_covers_the_longest_closed_stretch_of_the_year(self):
        """Guards the constant against being trimmed below what the calendar actually needs."""
        assert guard.DEFAULT_TRADING_DAY_LOOKBACK_DAYS >= 5
