# -*- coding: utf-8 -*-
"""Unit tests for the NSE bhavcopy vs. stock_daily reconciliation service.

Fully offline. ``fetch_bhavcopy`` and the whole database seam
(``BhavcopyReconciliationStore``) are injected as fakes, so nothing here touches
the network or the DB. The fake bhavcopy row mirrors the ``BhavcopyRow``
contract from ``src/services/nse_bhavcopy_client.py`` (symbol, OHLC, volume,
delivery_qty, delivery_pct), which lets these tests run before that module
exists.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src.services.bhavcopy_reconciliation_service import (
    PRICE_CHECK_ENABLED,
    PRICE_CHECK_UNAVAILABLE,
    REASON_CLOSE_MISMATCH,
    REASON_STORED_VOLUME_MISSING,
    REASON_VOLUME_MISMATCH,
    STATUS_DISABLED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    BhavcopyReconciliationService,
    BhavcopyUnavailable,
    QuarantineDraft,
    StoredBar,
)

TRADE_DATE = date(2026, 8, 28)


@dataclass(frozen=True)
class FakeBhavcopyRow:
    """Mirrors the ``BhavcopyRow`` contract owned by the bhavcopy client."""

    symbol: str
    open: float = 100.0
    high: float = 105.0
    low: float = 99.0
    close: float = 102.0
    volume: float = 1_000_000.0
    delivery_qty: Optional[float] = 400_000.0
    delivery_pct: Optional[float] = 40.0


class FakeStore:
    """In-memory stand-in for every DB read/write the service performs."""

    def __init__(
        self,
        bars: Optional[Dict[str, StoredBar]] = None,
        quarantined: Optional[Set[str]] = None,
    ):
        self.bars = dict(bars or {})
        self._quarantined: Set[str] = set(quarantined or set())
        self.written: List[QuarantineDraft] = []
        self.delivery_calls: List[Tuple[date, Dict[str, Tuple[Optional[float], Optional[float]]]]] = []
        self.load_calls: List[Optional[List[str]]] = []

    def load_bars(
        self,
        trade_date: date,
        codes: Optional[Iterable[str]] = None,
    ) -> Dict[str, StoredBar]:
        code_list = list(codes) if codes else None
        self.load_calls.append(code_list)
        if code_list is None:
            return dict(self.bars)
        return {code: bar for code, bar in self.bars.items() if code in set(code_list)}

    def quarantined_codes(self, trade_date: date) -> Set[str]:
        return set(self._quarantined)

    def write_quarantine(self, drafts) -> int:
        drafts = list(drafts)
        self.written.extend(drafts)
        self._quarantined.update(draft.code for draft in drafts)
        return len(drafts)

    def backfill_delivery(self, trade_date: date, updates) -> int:
        updates = dict(updates)
        self.delivery_calls.append((trade_date, updates))
        filled = 0
        for code, (qty, pct) in updates.items():
            bar = self.bars.get(code)
            if bar is None:
                continue
            changed = False
            new_qty, new_pct = bar.delivery_qty, bar.delivery_pct
            if qty is not None and bar.delivery_qty is None:
                new_qty, changed = float(qty), True
            if pct is not None and bar.delivery_pct is None:
                new_pct, changed = float(pct), True
            if changed:
                self.bars[code] = StoredBar(
                    code=bar.code,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    delivery_qty=new_qty,
                    delivery_pct=new_pct,
                )
                filled += 1
        return filled


def _exact_price_match(stored: float, published: float) -> bool:
    """Stand-in for the bhavcopy client's price tolerance helper."""

    return abs(stored - published) <= 0.01


def _service(
    store: FakeStore,
    published: Optional[Dict[str, FakeBhavcopyRow]] = None,
    *,
    enabled: bool = True,
    price_matches: Any = _exact_price_match,
    fetch_error: Optional[Exception] = None,
    **kwargs: Any,
) -> BhavcopyReconciliationService:
    def fake_fetch(trade_date: date) -> Dict[str, FakeBhavcopyRow]:
        if fetch_error is not None:
            raise fetch_error
        return dict(published or {})

    return BhavcopyReconciliationService(
        store=store,
        fetch_bhavcopy=fake_fetch,
        price_matches=price_matches,
        enabled=enabled,
        **kwargs,
    )


class TestDisabledByDefault(unittest.TestCase):
    """The flag defaults off, and off means no fetch and no DB access."""

    def test_disabled_flag_short_circuits(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0)})

        def exploding_fetch(trade_date):
            raise AssertionError("fetch_bhavcopy must not run while disabled")

        service = BhavcopyReconciliationService(
            store=store,
            fetch_bhavcopy=exploding_fetch,
            enabled=False,
        )
        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["status"], STATUS_DISABLED)
        self.assertEqual(summary["compared"], 0)
        self.assertEqual(store.load_calls, [])
        self.assertEqual(store.written, [])

    def test_config_default_is_off(self):
        from src.config import Config

        self.assertFalse(Config().bhavcopy_reconciliation_enabled)

    def test_env_key_is_documented_and_registered(self):
        """The key must be in .env.example and known to the config registry.

        Like DECISION_OUTCOME_DAILY_REFILL_ENABLED it is an operator-level
        switch kept out of the Web settings page, so registration means the
        explicit hidden-from-UI set rather than a UI field definition.
        """
        from pathlib import Path

        from src.core.config_registry import (
            WEB_SETTINGS_HIDDEN_FROM_UI,
            get_registered_field_keys,
        )

        env_example = Path(__file__).resolve().parents[1] / ".env.example"
        self.assertIn(
            "BHAVCOPY_RECONCILIATION_ENABLED=false",
            env_example.read_text(encoding="utf-8"),
        )
        known = set(get_registered_field_keys()) | set(WEB_SETTINGS_HIDDEN_FROM_UI)
        self.assertIn("BHAVCOPY_RECONCILIATION_ENABLED", known)

    def test_env_true_turns_the_flag_on(self):
        import os
        from unittest import mock

        from src.config import Config

        with mock.patch.dict(os.environ, {"BHAVCOPY_RECONCILIATION_ENABLED": "true"}, clear=False):
            self.assertTrue(Config._load_from_env().bhavcopy_reconciliation_enabled)


class TestMissingBhavcopyIsNormal(unittest.TestCase):
    """Weekend / holiday / NSE outage must be reported, not raised."""

    def test_unavailable_returns_summary(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0)})
        service = _service(store, fetch_error=BhavcopyUnavailable("no bhavcopy for 2026-08-29"))

        summary = service.reconcile(date(2026, 8, 29))

        self.assertEqual(summary["status"], STATUS_UNAVAILABLE)
        self.assertIn("no bhavcopy", summary["reason"])
        self.assertEqual(summary["compared"], 0)
        self.assertEqual(summary["quarantined"], 0)
        self.assertEqual(store.written, [])
        # No stored bar was read or touched.
        self.assertEqual(store.load_calls, [])

    def test_empty_bhavcopy_is_unavailable_not_a_full_mismatch(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0)})
        service = _service(store, published={})

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["status"], STATUS_UNAVAILABLE)
        self.assertEqual(store.written, [])


class TestAgreementAndDeliveryBackfill(unittest.TestCase):
    def test_matching_bar_backfills_delivery_only(self):
        bar = StoredBar(code="IDEA.NS", open=100.0, high=105.0, low=99.0, close=102.0, volume=1_000_000.0)
        store = FakeStore({"IDEA.NS": bar})
        service = _service(store, published={"IDEA": FakeBhavcopyRow(symbol="IDEA")})

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["status"], STATUS_OK)
        self.assertEqual(summary["compared"], 1)
        self.assertEqual(summary["agreed"], 1)
        self.assertEqual(summary["quarantined"], 0)
        self.assertEqual(summary["delivery_backfilled"], 1)
        self.assertEqual(summary["price_check"], PRICE_CHECK_ENABLED)

        updated = store.bars["IDEA.NS"]
        self.assertEqual(updated.delivery_qty, 400_000.0)
        self.assertEqual(updated.delivery_pct, 40.0)
        # The traded bar itself is untouched.
        self.assertEqual((updated.open, updated.high, updated.low, updated.close, updated.volume),
                         (100.0, 105.0, 99.0, 102.0, 1_000_000.0))

    def test_existing_delivery_values_are_not_overwritten(self):
        bar = StoredBar(
            code="IDEA.NS",
            close=102.0,
            volume=1_000_000.0,
            delivery_qty=123.0,
            delivery_pct=1.5,
        )
        store = FakeStore({"IDEA.NS": bar})
        service = _service(store, published={"IDEA": FakeBhavcopyRow(symbol="IDEA")})

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["agreed"], 1)
        self.assertEqual(summary["delivery_backfilled"], 0)
        self.assertEqual(store.delivery_calls, [])
        self.assertEqual(store.bars["IDEA.NS"].delivery_qty, 123.0)
        self.assertEqual(store.bars["IDEA.NS"].delivery_pct, 1.5)

    def test_absent_published_delivery_is_not_invented(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0)})
        published = {"IDEA": FakeBhavcopyRow(symbol="IDEA", delivery_qty=None, delivery_pct=None)}
        service = _service(store, published=published)

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["agreed"], 1)
        self.assertEqual(summary["delivery_backfilled"], 0)
        self.assertIsNone(store.bars["IDEA.NS"].delivery_qty)
        self.assertIsNone(store.bars["IDEA.NS"].delivery_pct)


class TestDisagreementQuarantine(unittest.TestCase):
    def test_volume_mismatch_is_quarantined_and_bar_kept(self):
        bar = StoredBar(code="IDEA.NS", close=102.0, volume=900_000.0)
        store = FakeStore({"IDEA.NS": bar})
        service = _service(store, published={"IDEA": FakeBhavcopyRow(symbol="IDEA")})

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["compared"], 1)
        self.assertEqual(summary["agreed"], 0)
        self.assertEqual(summary["quarantined"], 1)
        self.assertEqual(summary["quarantine_records_written"], 1)
        self.assertEqual(summary["delivery_backfilled"], 0)

        draft = store.written[0]
        self.assertEqual(draft.code, "IDEA.NS")
        self.assertEqual(draft.symbol, "IDEA")
        self.assertEqual(draft.trade_date, TRADE_DATE)
        self.assertIn(REASON_VOLUME_MISMATCH, draft.reasons)
        self.assertEqual(draft.stored_volume, 900_000.0)
        self.assertEqual(draft.published_volume, 1_000_000.0)
        # The mismatching bar is left exactly as stored, including its empty
        # delivery fields — a disagreeing bar must not be backfilled.
        self.assertEqual(store.bars["IDEA.NS"], bar)

    def test_close_mismatch_uses_the_client_tolerance_helper(self):
        calls: List[Tuple[float, float]] = []

        def recording_matcher(stored: float, published: float) -> bool:
            calls.append((stored, published))
            return False

        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=95.0, volume=1_000_000.0)})
        service = _service(
            store,
            published={"IDEA": FakeBhavcopyRow(symbol="IDEA")},
            price_matches=recording_matcher,
        )

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(calls, [(95.0, 102.0)])
        self.assertEqual(summary["quarantined"], 1)
        self.assertIn(REASON_CLOSE_MISMATCH, store.written[0].reasons)
        self.assertNotIn(REASON_VOLUME_MISMATCH, store.written[0].reasons)

    def test_missing_price_helper_falls_back_to_volume_only(self):
        # Dividend-adjusted local closes cannot be compared without the
        # client's tolerance rule, so price comparison is reported as
        # unavailable rather than silently declared a match.
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=95.0, volume=1_000_000.0)})
        service = _service(
            store,
            published={"IDEA": FakeBhavcopyRow(symbol="IDEA")},
            price_matches=None,
        )
        # Force resolution to find no helper even if the client module lands.
        service._resolve_price_matcher = lambda: None  # type: ignore[method-assign]

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["price_check"], PRICE_CHECK_UNAVAILABLE)
        self.assertEqual(summary["agreed"], 1)
        self.assertEqual(summary["quarantined"], 0)

    def test_missing_stored_volume_is_a_disagreement(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=None)})
        service = _service(store, published={"IDEA": FakeBhavcopyRow(symbol="IDEA")})

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["quarantined"], 1)
        self.assertIn(REASON_STORED_VOLUME_MISSING, store.written[0].reasons)

    def test_volume_within_tolerance_agrees(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=999_000.0)})
        service = _service(
            store,
            published={"IDEA": FakeBhavcopyRow(symbol="IDEA")},
            volume_tolerance_pct=0.5,
        )

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["agreed"], 1)
        self.assertEqual(summary["quarantined"], 0)

    def test_zero_tolerance_flags_the_same_bar(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=999_000.0)})
        service = _service(
            store,
            published={"IDEA": FakeBhavcopyRow(symbol="IDEA")},
            volume_tolerance_pct=0.0,
        )

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["quarantined"], 1)


class TestIdempotency(unittest.TestCase):
    def test_second_run_writes_no_duplicate_quarantine_record(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=900_000.0)})
        service = _service(store, published={"IDEA": FakeBhavcopyRow(symbol="IDEA")})

        first = service.reconcile(TRADE_DATE)
        second = service.reconcile(TRADE_DATE)

        self.assertEqual(first["quarantine_records_written"], 1)
        self.assertEqual(second["quarantined"], 1)
        self.assertEqual(second["quarantine_records_written"], 0)
        self.assertEqual(second["quarantine_records_skipped"], 1)
        self.assertEqual(len(store.written), 1)

    def test_preexisting_quarantine_record_blocks_rewrite(self):
        store = FakeStore(
            {"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=900_000.0)},
            quarantined={"IDEA.NS"},
        )
        service = _service(store, published={"IDEA": FakeBhavcopyRow(symbol="IDEA")})

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["quarantined"], 1)
        self.assertEqual(summary["quarantine_records_written"], 0)
        self.assertEqual(store.written, [])


class TestSymbolMappingAndCoverage(unittest.TestCase):
    def test_symbol_ns_mapping_both_directions(self):
        bars = {
            "IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0),
            "TCS.NS": StoredBar(code="TCS.NS", close=102.0, volume=1_000_000.0),
        }
        store = FakeStore(bars)
        service = _service(
            store,
            published={
                "IDEA": FakeBhavcopyRow(symbol="IDEA"),
                "INFY": FakeBhavcopyRow(symbol="INFY"),
            },
        )

        summary = service.reconcile(TRADE_DATE, codes=["IDEA.NS", "TCS.NS", "INFY.NS"])

        self.assertEqual(summary["compared"], 1)
        self.assertEqual(summary["agreed"], 1)
        # TCS is stored locally but absent from the published bhavcopy.
        self.assertEqual(summary["missing_in_bhavcopy"], ["TCS.NS"])
        # INFY is published but has no stored bar.
        self.assertEqual(summary["missing_in_stock_daily"], ["INFY"])

    def test_code_filter_is_pushed_into_the_repository(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0)})
        service = _service(store, published={"IDEA": FakeBhavcopyRow(symbol="IDEA")})

        service.reconcile(TRADE_DATE, codes=["idea.ns"])

        self.assertEqual(store.load_calls, [["IDEA.NS"]])

    def test_bare_symbol_input_is_accepted(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0)})
        service = _service(store, published={"IDEA": FakeBhavcopyRow(symbol="IDEA")})

        summary = service.reconcile(TRADE_DATE, codes=["IDEA"])

        self.assertEqual(store.load_calls, [["IDEA.NS"]])
        self.assertEqual(summary["agreed"], 1)

    def test_non_nse_stored_bars_are_skipped(self):
        bars = {
            "IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0),
            "600519": StoredBar(code="600519", close=102.0, volume=1_000_000.0),
            "AAPL": StoredBar(code="AAPL", close=102.0, volume=1_000_000.0),
        }
        store = FakeStore(bars)
        service = _service(store, published={"IDEA": FakeBhavcopyRow(symbol="IDEA")})

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["compared"], 1)
        self.assertEqual(summary["skipped_non_nse"], ["600519", "AAPL"])
        self.assertEqual(summary["missing_in_bhavcopy"], [])

    def test_non_nse_only_request_does_not_widen_to_the_whole_day(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0)})

        def exploding_fetch(trade_date):
            raise AssertionError("a request with no NSE code must not fetch")

        service = BhavcopyReconciliationService(
            store=store,
            fetch_bhavcopy=exploding_fetch,
            price_matches=_exact_price_match,
            enabled=True,
        )

        summary = service.reconcile(TRADE_DATE, codes=["600519.SS", "0700.HK"])

        self.assertEqual(summary["compared"], 0)
        self.assertEqual(summary["skipped_non_nse"], ["0700.HK", "600519.SS"])
        self.assertEqual(store.load_calls, [])
        self.assertEqual(store.written, [])

    def test_empty_code_list_is_not_treated_as_whole_day(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0)})
        service = _service(store, published={"IDEA": FakeBhavcopyRow(symbol="IDEA")})

        summary = service.reconcile(TRADE_DATE, codes=[])

        self.assertEqual(summary["compared"], 0)
        self.assertEqual(store.load_calls, [])

    def test_full_market_bhavcopy_does_not_report_unwatched_symbols(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=1_000_000.0)})
        published = {
            "IDEA": FakeBhavcopyRow(symbol="IDEA"),
            "INFY": FakeBhavcopyRow(symbol="INFY"),
            "TCS": FakeBhavcopyRow(symbol="TCS"),
        }
        service = _service(store, published=published)

        summary = service.reconcile(TRADE_DATE)

        self.assertEqual(summary["missing_in_stock_daily"], [])
        self.assertEqual(summary["published_symbol_count"], 3)


if __name__ == "__main__":
    unittest.main()


class TestQuarantineDetails(unittest.TestCase):
    """The summary must describe *which* bars disagreed, not only how many.

    Added 2026-09-01 with the quarantine alert. A count alone tells the reader that
    something is wrong without telling them what, so the alert would have been
    unactionable and the reconciliation would still, in effect, be telling nobody.
    """

    def _disagreeing_setup(self):
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=460_728_374.0)})
        published = {"IDEA": FakeBhavcopyRow(symbol="IDEA", close=102.0, volume=1_534_470_198.0)}
        return store, published

    def test_details_name_the_bar_and_both_numbers(self):
        store, published = self._disagreeing_setup()
        summary = _service(store, published).reconcile(TRADE_DATE)

        self.assertEqual(summary["quarantined"], 1)
        details = summary["quarantine_details"]
        self.assertEqual(len(details), 1)
        detail = details[0]
        self.assertEqual(detail["code"], "IDEA.NS")
        self.assertEqual(detail["symbol"], "IDEA")
        self.assertIn(REASON_VOLUME_MISMATCH, detail["reasons"])
        self.assertEqual(detail["stored_volume"], 460_728_374.0)
        self.assertEqual(detail["published_volume"], 1_534_470_198.0)

    def test_a_repeat_run_reports_no_details(self):
        """Idempotence has to reach the details too, or the same day alerts twice."""
        store, published = self._disagreeing_setup()
        _service(store, published).reconcile(TRADE_DATE)
        second = _service(store, published).reconcile(TRADE_DATE)

        self.assertEqual(second["quarantined"], 1)
        self.assertEqual(second["quarantine_records_written"], 0)
        self.assertEqual(second["quarantine_details"], [])

    def test_missing_values_stay_none(self):
        """A missing stored volume must not be reported as zero."""
        store = FakeStore({"IDEA.NS": StoredBar(code="IDEA.NS", close=102.0, volume=None)})
        published = {"IDEA": FakeBhavcopyRow(symbol="IDEA", close=102.0, volume=1_000_000.0)}
        summary = _service(store, published).reconcile(TRADE_DATE)

        detail = summary["quarantine_details"][0]
        self.assertIsNone(detail["stored_volume"])
        self.assertEqual(detail["published_volume"], 1_000_000.0)

    def test_an_empty_summary_carries_an_empty_detail_list(self):
        """Callers may read the key unconditionally, on every status."""
        store = FakeStore()
        summary = _service(store, {}, enabled=False).reconcile(TRADE_DATE)
        self.assertEqual(summary["quarantine_details"], [])
