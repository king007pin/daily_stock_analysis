# -*- coding: utf-8 -*-
"""The archive sweep must cover the history, and must not behave like the daily run.

The scheduled reconciliation only ever looks at the previous trading day, so everything
before 2026-08-25 had never been compared with the exchange - and that history is the
input to every backtest, every scored outcome and every base rate in the vault.

Two properties matter more than the arithmetic:

* it must not alert. Ninety days of notifications is the fastest way to make someone
  mute the channel that the daily run depends on;
* a bad day must not end the sweep, but an unresponsive exchange must.

Fully offline: the reconciliation callable is a stub and sleeping is injected.
"""

from datetime import date

import pytest

from scripts.reconcile_bhavcopy_archive import (
    CONSECUTIVE_FAILURE_LIMIT,
    sweep,
)

DAYS = [date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27)]


def _summary(**overrides):
    summary = {
        "status": "ok",
        "compared": 13,
        "agreed": 13,
        "quarantined": 0,
        "quarantine_records_written": 0,
        "delivery_backfilled": 13,
        "quarantine_details": [],
    }
    summary.update(overrides)
    return summary


def _no_sleep(_seconds):
    return None


class TestCoverage:
    def test_every_day_is_reconciled_once(self):
        seen = []

        def reconcile(day):
            seen.append(day)
            return _summary()

        result = sweep(DAYS, reconcile=reconcile, sleep=_no_sleep)

        assert seen == DAYS
        assert result["totals"]["days_attempted"] == 3
        assert result["totals"]["days_reconciled"] == 3

    def test_totals_add_up_across_days(self):
        result = sweep(
            DAYS,
            reconcile=lambda _day: _summary(compared=10, agreed=9, quarantined=1, quarantine_records_written=2),
            sleep=_no_sleep,
        )

        totals = result["totals"]
        assert totals["compared"] == 30
        assert totals["agreed"] == 27
        assert totals["quarantined"] == 3
        assert totals["quarantine_records_written"] == 6

    def test_disagreements_carry_the_date_they_belong_to(self):
        def reconcile(day):
            if day != date(2026, 8, 26):
                return _summary()
            return _summary(
                quarantined=1,
                quarantine_records_written=1,
                quarantine_details=[{"code": "IDEA.NS", "reasons": ["volume_mismatch"]}],
            )

        result = sweep(DAYS, reconcile=reconcile, sleep=_no_sleep)

        assert len(result["disagreements"]) == 1
        assert result["disagreements"][0]["date"] == "2026-08-26"
        assert result["disagreements"][0]["code"] == "IDEA.NS"

    def test_an_unavailable_day_is_counted_not_treated_as_success(self):
        """A holiday or an NSE outage is a normal result, but it is not a reconciliation."""
        result = sweep(
            [date(2026, 8, 25)],
            reconcile=lambda _day: _summary(status="unavailable", compared=0, agreed=0, delivery_backfilled=0),
            sleep=_no_sleep,
        )

        assert result["totals"]["days_unavailable"] == 1
        assert result["totals"]["days_reconciled"] == 0


class TestFailureHandling:
    def test_one_bad_day_does_not_end_the_sweep(self):
        def reconcile(day):
            if day == DAYS[0]:
                raise RuntimeError("connection reset")
            return _summary()

        result = sweep(DAYS, reconcile=reconcile, sleep=_no_sleep)

        assert result["totals"]["days_failed"] == 1
        assert result["totals"]["days_reconciled"] == 2
        assert result["aborted_after"] is None
        assert "connection reset" in result["failures"][0]["error"]

    def test_a_run_of_failures_stops_the_sweep(self):
        """Repeated failures mean the exchange is refusing us, not that those days are odd."""
        days = [date(2026, 8, d) for d in range(3, 3 + CONSECUTIVE_FAILURE_LIMIT + 4)]
        attempted = []

        def reconcile(day):
            attempted.append(day)
            raise RuntimeError("403")

        result = sweep(days, reconcile=reconcile, sleep=_no_sleep)

        assert len(attempted) == CONSECUTIVE_FAILURE_LIMIT
        assert result["aborted_after"] == days[CONSECUTIVE_FAILURE_LIMIT - 1].isoformat()

    def test_the_failure_counter_resets_after_a_good_day(self):
        """Scattered failures across a long archive must not trip the abort."""
        days = [date(2026, 8, d) for d in range(3, 20)]

        def reconcile(day):
            if day.day % 2:
                raise RuntimeError("transient")
            return _summary()

        result = sweep(days, reconcile=reconcile, sleep=_no_sleep)

        assert result["aborted_after"] is None
        assert result["totals"]["days_attempted"] == len(days)


class TestItIsNotTheDailyRun:
    def test_the_sweep_never_sends_an_alert(self, monkeypatch):
        """Alerting belongs to the scheduled single-day path, which someone reads."""
        import main

        def explode(*_args, **_kwargs):
            raise AssertionError("the archive sweep must not alert")

        monkeypatch.setattr(main, "_send_quarantine_alert", explode)

        sweep(
            DAYS,
            reconcile=lambda _day: _summary(
                quarantined=1,
                quarantine_records_written=1,
                quarantine_details=[{"code": "IDEA.NS", "reasons": ["volume_mismatch"]}],
            ),
            sleep=_no_sleep,
        )

    def test_it_waits_between_fetches_but_not_before_the_first(self):
        waits = []

        sweep(DAYS, reconcile=lambda _day: _summary(), delay_seconds=1.5, sleep=waits.append)

        assert waits == [1.5, 1.5]

    def test_no_days_means_no_work(self):
        result = sweep([], reconcile=lambda _day: _summary(), sleep=_no_sleep)

        assert result["totals"]["days_attempted"] == 0
        assert result["disagreements"] == []


class TestDayEnumeration:
    def test_stored_days_come_back_oldest_first(self, tmp_path, monkeypatch):
        import os

        from src.config import Config
        from src.storage import DatabaseManager, StockDaily

        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "archive.db"))
        Config.reset_instance()
        DatabaseManager.reset_instance()
        db = DatabaseManager.get_instance()
        try:
            with db.get_session() as session:
                for code, day in (
                    ("IDEA.NS", date(2026, 8, 27)),
                    ("IDEA.NS", date(2026, 8, 25)),
                    ("BCG.NS", date(2026, 8, 25)),
                    ("600519", date(2026, 8, 26)),  # not NSE - must not appear
                ):
                    session.add(
                        StockDaily(code=code, date=day, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)
                    )
                session.commit()

            from scripts.reconcile_bhavcopy_archive import stored_nse_trading_days

            assert stored_nse_trading_days(db) == [date(2026, 8, 25), date(2026, 8, 27)]
        finally:
            DatabaseManager.reset_instance()
            Config.reset_instance()
            os.environ.pop("DATABASE_PATH", None)
