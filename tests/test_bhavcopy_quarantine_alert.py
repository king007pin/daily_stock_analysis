# -*- coding: utf-8 -*-
"""A quarantined bar must reach a human.

Reconciliation was scheduled in `a6ef121`, but it wrote a quarantine row, logged one
line and told nobody — the same defect as a service with no callers, one layer up. A
vendor error nobody is told about is indistinguishable from no vendor error, and the
one already in this database (IDEA.NS volume, −70%) was found by hand, not by the
system.

These tests pin four things: the alert fires when a bar is quarantined, it says which
bar and both numbers, it stays quiet when the day was already reported, and it can
never take down the analysis run.

Fully offline: the notifier is patched in every test, so nothing is ever sent.
"""

from types import SimpleNamespace
from unittest.mock import patch

import main

SERVICE_PATH = "src.services.bhavcopy_reconciliation_service.BhavcopyReconciliationService"
PREVIOUS_DAY_PATH = "src.services.nse_trading_day_guard.previous_nse_trading_day"
NOTIFIER_PATH = "src.notification.NotificationService"
BUILDER_PATH = "src.notification.NotificationBuilder"

from datetime import date

LAST_SESSION = date(2026, 8, 25)

IDEA_DETAIL = {
    "code": "IDEA.NS",
    "symbol": "IDEA",
    "reasons": ["volume_mismatch"],
    "stored_close": 13.70,
    "published_close": 13.70,
    "stored_volume": 460_728_374.0,
    "published_volume": 1_534_470_198.0,
}


def _summary(**overrides):
    summary = {
        "status": "ok",
        "trade_date": LAST_SESSION.isoformat(),
        "reason": None,
        "compared": 225,
        "agreed": 224,
        "quarantined": 1,
        "quarantine_records_written": 1,
        "quarantine_records_skipped": 0,
        "quarantine_details": [dict(IDEA_DETAIL)],
        "delivery_backfilled": 224,
        "price_check": "enabled",
    }
    summary.update(overrides)
    return summary


def _run(summary):
    """Drive the scheduled runner with a canned reconciliation result."""
    config = SimpleNamespace(bhavcopy_reconciliation_enabled=True)
    with patch(PREVIOUS_DAY_PATH, return_value=LAST_SESSION), patch(SERVICE_PATH) as service_cls, patch(
        NOTIFIER_PATH
    ) as notifier_cls:
        service_cls.return_value.reconcile.return_value = summary
        main._run_bhavcopy_reconciliation(config)
    return notifier_cls


class TestTheAlertFires:
    def test_a_quarantined_bar_sends_an_alert(self):
        notifier_cls = _run(_summary())
        notifier_cls.return_value.send.assert_called_once()

    def test_the_alert_is_routed_as_an_alert_not_a_report(self):
        notifier_cls = _run(_summary())
        _args, kwargs = notifier_cls.return_value.send.call_args
        assert kwargs["route_type"] == "alert"

    def test_the_alert_names_the_bar_and_both_numbers(self):
        """A count alone would tell the reader something is wrong, but not what."""
        notifier_cls = _run(_summary())
        sent = notifier_cls.return_value.send.call_args[0][0]

        assert "IDEA.NS" in sent
        assert "460,728,374" in sent
        assert "1,534,470,198" in sent
        assert "-70.0%" in sent

    def test_the_alert_says_the_stored_bar_was_not_touched(self):
        notifier_cls = _run(_summary())
        sent = notifier_cls.return_value.send.call_args[0][0]
        assert "unchanged" in sent

    def test_rows_written_and_bars_affected_are_not_conflated(self):
        """One bar can write several quarantine rows - one per disagreeing field."""
        notifier_cls = _run(_summary(quarantine_records_written=2))
        sent = notifier_cls.return_value.send.call_args[0][0]

        assert "1 of 225 reconciled bars" in sent
        assert "2 quarantine rows written" in sent


class TestTheAlertStaysQuiet:
    def test_no_alert_when_nothing_was_written(self):
        notifier_cls = _run(_summary(quarantined=0, quarantine_records_written=0, quarantine_details=[]))
        notifier_cls.return_value.send.assert_not_called()

    def test_a_repeat_run_of_the_same_day_is_silent(self):
        """The store dedupes per trade date; the alert must follow it, not the count."""
        notifier_cls = _run(
            _summary(quarantined=1, quarantine_records_written=0, quarantine_records_skipped=1, quarantine_details=[])
        )
        notifier_cls.return_value.send.assert_not_called()

    def test_an_unavailable_bhavcopy_is_not_an_alert(self):
        """Weekend, holiday and NSE outage are normal results, not incidents."""
        notifier_cls = _run(
            _summary(status="unavailable", reason="bhavcopy 不可用", compared=0, agreed=0,
                     quarantined=0, quarantine_records_written=0, quarantine_details=[])
        )
        notifier_cls.return_value.send.assert_not_called()


class TestTheAlertCannotBreakTheRun:
    def test_a_notifier_that_raises_is_swallowed(self):
        config = SimpleNamespace(bhavcopy_reconciliation_enabled=True)
        with patch(PREVIOUS_DAY_PATH, return_value=LAST_SESSION), patch(SERVICE_PATH) as service_cls, patch(
            NOTIFIER_PATH, side_effect=RuntimeError("smtp is down")
        ):
            service_cls.return_value.reconcile.return_value = _summary()
            main._run_bhavcopy_reconciliation(config)  # must not raise

    def test_a_send_that_returns_false_is_logged_not_raised(self, caplog):
        config = SimpleNamespace(bhavcopy_reconciliation_enabled=True)
        with patch(PREVIOUS_DAY_PATH, return_value=LAST_SESSION), patch(SERVICE_PATH) as service_cls, patch(
            NOTIFIER_PATH
        ) as notifier_cls:
            service_cls.return_value.reconcile.return_value = _summary()
            notifier_cls.return_value.send.return_value = False
            main._run_bhavcopy_reconciliation(config)

        assert any("未送达" in record.message for record in caplog.records)


class TestDeduplicationKey:
    def test_the_key_carries_the_trade_date(self):
        key = main._quarantine_dedup_key(_summary())
        assert key.startswith(f"bhavcopy-quarantine:{LAST_SESSION.isoformat()}:")

    def test_the_same_bars_produce_the_same_key(self):
        assert main._quarantine_dedup_key(_summary()) == main._quarantine_dedup_key(_summary())

    def test_a_different_set_of_bars_produces_a_different_key(self):
        """Keying on the date alone would drop a second, genuinely new finding."""
        other = _summary(quarantine_details=[dict(IDEA_DETAIL, code="RELIANCE.NS")])
        assert main._quarantine_dedup_key(_summary()) != main._quarantine_dedup_key(other)


class TestFormatting:
    def test_a_flood_is_capped_and_says_so(self):
        details = [dict(IDEA_DETAIL, code=f"CODE{i}.NS") for i in range(25)]
        text = main._format_quarantine_alert(_summary(quarantined=25, quarantine_details=details))

        assert text.count("volume: stored") == main.QUARANTINE_ALERT_MAX_ROWS
        assert "...and 5 more bars" in text

    def test_a_missing_stored_value_is_named_not_zeroed(self):
        detail = dict(IDEA_DETAIL, reasons=["stored_volume_missing"], stored_volume=None)
        text = main._format_quarantine_alert(_summary(quarantine_details=[detail]))

        assert "stored missing" in text
        assert "0%" not in text

    def test_a_close_disagreement_is_rendered_as_a_price(self):
        detail = dict(
            IDEA_DETAIL,
            reasons=["close_mismatch"],
            stored_close=12.50,
            published_close=12.95,
        )
        text = main._format_quarantine_alert(_summary(quarantine_details=[detail]))

        assert "close: stored 12.50 vs NSE 12.95" in text
        assert "-3.5%" in text

    def test_both_fields_are_listed_when_both_disagree(self):
        detail = dict(IDEA_DETAIL, reasons=["volume_mismatch", "close_mismatch"], stored_close=12.50, published_close=12.95)
        text = main._format_quarantine_alert(_summary(quarantine_details=[detail]))

        assert "volume: stored" in text
        assert "close: stored" in text
