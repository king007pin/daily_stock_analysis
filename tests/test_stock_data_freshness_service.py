# -*- coding: utf-8 -*-
"""Prices that quietly stop updating must be reported.

On 2026-09-01 the 08:57 run logged `数据保存成功（新增 0 条）` for `BCG.NS` and
`EASEMYTRIP.NS` — save succeeded, nothing written, because the vendor had not published
the 08-31 bar yet. `588200` was six bars behind because Pytdx could reach no server and
only the fallback source worked. All three produced signals that day anyway.

The system was pricing today's signals off days-old bars and nothing noticed. These tests
pin the detector. Fully offline: a temporary database, rows inserted directly, and every
"today" injected rather than read from the clock.
"""

import os
from datetime import date, datetime, timedelta

import pytest

from src.config import Config
from src.services.stock_data_freshness_service import (
    DEFAULT_MAX_AGE_DAYS,
    StockDataFreshnessService,
)
from src.storage import DatabaseManager, DecisionSignalRecord, StockDaily

AS_OF = date(2026, 9, 1)


@pytest.fixture()
def db(tmp_path):
    previous = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "freshness.db")
    Config.reset_instance()
    DatabaseManager.reset_instance()
    manager = DatabaseManager.get_instance()
    try:
        yield manager
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if previous is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = previous


def _bar(session, code, day):
    """Insert one daily bar. ``get_session`` does not commit on exit - callers must."""
    session.add(StockDaily(code=code, date=day, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0))
    session.commit()


def _signal(session, code, created_at):
    session.add(
        DecisionSignalRecord(
            stock_code=code,
            market="in",
            source_type="analysis",
            trigger_source="system",
            action="buy",
            created_at=created_at,
        )
    )
    session.commit()


def _service(db, **kwargs):
    return StockDataFreshnessService(db_manager=db, **kwargs)


class TestWhatCountsAsStale:
    def test_a_current_code_is_not_reported(self, db):
        with db.get_session() as session:
            _bar(session, "IDEA.NS", date(2026, 8, 31))

        assert _service(db).find_stale(as_of=AS_OF) == []

    def test_a_code_days_behind_is_reported_with_its_gap(self, db):
        """The BCG.NS case, as it stood this morning."""
        with db.get_session() as session:
            _bar(session, "BCG.NS", date(2026, 8, 28))

        stale = _service(db, max_age_days=2).find_stale(as_of=AS_OF)

        assert len(stale) == 1
        assert stale[0].code == "BCG.NS"
        assert stale[0].last_bar == date(2026, 8, 28)
        assert stale[0].days_behind == 4

    def test_the_threshold_is_exclusive_at_the_boundary(self, db):
        """Exactly at the limit is not yet stale; one day past it is."""
        with db.get_session() as session:
            _bar(session, "EDGE.NS", AS_OF - timedelta(days=DEFAULT_MAX_AGE_DAYS))
            _bar(session, "OVER.NS", AS_OF - timedelta(days=DEFAULT_MAX_AGE_DAYS + 1))

        codes = [item.code for item in _service(db).find_stale(as_of=AS_OF)]

        assert codes == ["OVER.NS"]

    def test_a_code_with_no_bars_at_all_is_not_called_stale(self, db):
        """Never fetched is a different problem from fallen behind, and needs a different fix."""
        with db.get_session() as session:
            _signal(session, "GHOST.NS", datetime(2026, 9, 1, 3, 0))

        assert _service(db).find_stale(as_of=AS_OF) == []

    def test_the_worst_offender_comes_first(self, db):
        with db.get_session() as session:
            _bar(session, "SLIGHT.NS", date(2026, 8, 25))
            _bar(session, "AWFUL.BO", date(2026, 7, 17))

        codes = [item.code for item in _service(db).find_stale(as_of=AS_OF)]

        assert codes == ["AWFUL.BO", "SLIGHT.NS"]


class TestTheHarmfulCase:
    """A stale code nobody trades is untidy. A stale code still producing signals is wrong."""

    def test_signals_written_after_the_last_bar_are_counted(self, db):
        with db.get_session() as session:
            _bar(session, "BCG.NS", date(2026, 8, 28))
            _signal(session, "BCG.NS", datetime(2026, 8, 31, 3, 0))
            _signal(session, "BCG.NS", datetime(2026, 9, 1, 3, 0))

        stale = _service(db, max_age_days=2).find_stale(as_of=AS_OF)[0]

        assert stale.signals_since_last_bar == 2
        assert stale.signalled_while_stale is True

    def test_signals_from_before_the_last_bar_do_not_count(self, db):
        with db.get_session() as session:
            _bar(session, "OLD.NS", date(2026, 8, 20))
            _signal(session, "OLD.NS", datetime(2026, 8, 10, 3, 0))

        stale = _service(db).find_stale(as_of=AS_OF)[0]

        assert stale.signals_since_last_bar == 0
        assert stale.signalled_while_stale is False

    def test_a_dead_watchlist_entry_is_reported_but_not_flagged(self, db):
        """500325.BO: 46 days behind and signalled by nobody. Real, but not urgent."""
        with db.get_session() as session:
            _bar(session, "500325.BO", date(2026, 7, 17))

        stale = _service(db).find_stale(as_of=AS_OF)[0]

        assert stale.days_behind == 46
        assert stale.signalled_while_stale is False


class TestSummary:
    def test_summary_separates_the_two_populations(self, db):
        with db.get_session() as session:
            _bar(session, "BCG.NS", date(2026, 8, 25))
            _signal(session, "BCG.NS", datetime(2026, 9, 1, 3, 0))
            _bar(session, "500325.BO", date(2026, 7, 17))

        summary = _service(db).summary(as_of=AS_OF)

        assert summary["stale_count"] == 2
        assert summary["signalled_while_stale_count"] == 1
        assert summary["as_of"] == "2026-09-01"
        assert summary["max_age_days"] == DEFAULT_MAX_AGE_DAYS

    def test_a_scope_of_codes_narrows_the_check(self, db):
        with db.get_session() as session:
            _bar(session, "BCG.NS", date(2026, 8, 20))
            _bar(session, "500325.BO", date(2026, 7, 17))

        stale = _service(db).find_stale(as_of=AS_OF, codes=["BCG.NS"])

        assert [item.code for item in stale] == ["BCG.NS"]


class TestTheCheckIsWiredIntoTheRun:
    """The service existing is not the point - the last three items in this repo were all
    'built and never called'. These pin the wiring."""

    def test_the_check_runs_on_the_empty_portfolio_path(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        import main

        config = SimpleNamespace(backtest_enabled=True, market_review_enabled=False)
        args = SimpleNamespace(portfolio=None, no_market_review=True)

        with patch.object(main, "_resolve_portfolio_stock_codes", return_value=[]), patch.object(
            main, "_run_auto_backtest"
        ), patch.object(main, "_run_decision_signal_outcomes"), patch.object(
            main, "_run_bhavcopy_reconciliation"
        ), patch.object(main, "_run_stock_data_freshness_check") as check:
            assert main.run_full_analysis(config, args, []) is True

        check.assert_called_once_with(config)

    def test_the_check_runs_on_the_shared_return_path(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        import main

        config = SimpleNamespace(
            backtest_enabled=True,
            market_review_enabled=False,
            stock_list=[],
            refresh_stock_list=lambda: None,
        )
        args = SimpleNamespace(portfolio=None, no_market_review=True, dry_run=False)

        with patch.object(main, "_resolve_portfolio_stock_codes", return_value=None), patch.object(
            main, "_refresh_stock_index_cache_for_analysis"
        ), patch.object(main, "_run_auto_backtest"), patch.object(
            main, "_run_decision_signal_outcomes"
        ), patch.object(main, "_run_bhavcopy_reconciliation"), patch.object(
            main, "_run_stock_data_freshness_check"
        ) as check:
            assert main.run_full_analysis(config, args, None) is False

        check.assert_called_once_with(config)


class TestTheAlertOnlyFiresForTheHarmfulCase:
    SERVICE = "src.services.stock_data_freshness_service.StockDataFreshnessService"
    NOTIFIER = "src.notification.NotificationService"

    def _summary(self, **overrides):
        from src.services.stock_data_freshness_service import StaleCode

        summary = {
            "as_of": "2026-09-01",
            "max_age_days": 4,
            "stale_count": 1,
            "signalled_while_stale_count": 1,
            "stale": [StaleCode(code="BCG.NS", last_bar=date(2026, 8, 25), days_behind=7, signals_since_last_bar=2)],
        }
        summary.update(overrides)
        return summary

    def _run(self, summary):
        from types import SimpleNamespace
        from unittest.mock import patch

        import main

        with patch(self.SERVICE) as service_cls, patch(self.NOTIFIER) as notifier_cls:
            service_cls.return_value.summary.return_value = summary
            main._run_stock_data_freshness_check(SimpleNamespace())
        return notifier_cls

    def test_a_stale_code_still_being_signalled_alerts(self):
        notifier = self._run(self._summary())

        notifier.return_value.send.assert_called_once()
        sent = notifier.return_value.send.call_args[0][0]
        assert "BCG.NS" in sent
        assert "7 days behind" in sent

    def test_a_stale_code_nobody_signals_does_not_alert(self):
        from src.services.stock_data_freshness_service import StaleCode

        notifier = self._run(
            self._summary(
                signalled_while_stale_count=0,
                stale=[StaleCode(code="500325.BO", last_bar=date(2026, 7, 17), days_behind=46, signals_since_last_bar=0)],
            )
        )

        notifier.return_value.send.assert_not_called()

    def test_a_clean_run_says_nothing(self):
        notifier = self._run(self._summary(stale_count=0, signalled_while_stale_count=0, stale=[]))

        notifier.return_value.send.assert_not_called()

    def test_the_quiet_ones_still_appear_in_the_alert_body(self):
        """Named for context, never as the trigger."""
        from src.services.stock_data_freshness_service import StaleCode

        import main

        summary = self._summary(
            stale_count=2,
            stale=[
                StaleCode(code="BCG.NS", last_bar=date(2026, 8, 25), days_behind=7, signals_since_last_bar=2),
                StaleCode(code="500325.BO", last_bar=date(2026, 7, 17), days_behind=46, signals_since_last_bar=0),
            ],
        )
        text = main._format_stale_data_alert(summary)

        assert "1 of 2 stale codes" in text
        assert "500325.BO (46d)" in text

    def test_a_notifier_that_raises_cannot_break_the_run(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        import main

        with patch(self.SERVICE) as service_cls, patch(self.NOTIFIER, side_effect=RuntimeError("smtp down")):
            service_cls.return_value.summary.return_value = self._summary()
            main._run_stock_data_freshness_check(SimpleNamespace())  # must not raise

    def test_a_service_that_raises_cannot_break_the_run(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        import main

        with patch(self.SERVICE, side_effect=RuntimeError("database is locked")):
            main._run_stock_data_freshness_check(SimpleNamespace())  # must not raise
