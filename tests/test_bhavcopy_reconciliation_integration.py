# -*- coding: utf-8 -*-
"""The reconciliation path against a real database.

Built by three agents working in parallel against an interface contract. Their
unit tests all passed and the pieces still did not fit: the service wrote one
quarantine row per *bar* with a null ``field_name``, while the table requires one
row per disagreeing *field*. That surfaced only here, as an IntegrityError.

So this test exercises the real SQLAlchemy store rather than a fake, and asserts
the property the whole feature exists for:

    a disagreeing bar is quarantined as evidence, never overwritten.
"""

import datetime as dt
import os
import tempfile
import unittest

from src.services.nse_bhavcopy_client import BhavcopyRow

TRADE_DATE = dt.date(2026, 8, 25)

# The real vendor error found on 2026-08-31.
IDEA_STORED_VOLUME = 460_728_374.0
IDEA_PUBLISHED_VOLUME = 1_534_470_198.0


class ReconciliationAgainstRealStorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._prev_db = os.environ.get("DATABASE_PATH")
        self._prev_flag = os.environ.get("BHAVCOPY_RECONCILIATION_ENABLED")
        os.environ["DATABASE_PATH"] = os.path.join(self._tmp.name, "recon.db")
        os.environ["BHAVCOPY_RECONCILIATION_ENABLED"] = "true"

        from src.config import Config
        from src.storage import DatabaseManager, StockDaily

        Config._instance = None
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.addCleanup(self._restore_env)

        with self.db.get_session() as session:
            session.add(StockDaily(
                code="IDEA.NS", date=TRADE_DATE, open=15.1, high=15.3, low=14.9,
                close=15.19, volume=IDEA_STORED_VOLUME, data_source="test",
            ))
            session.add(StockDaily(
                code="TCS.NS", date=TRADE_DATE, open=2245.0, high=2260.0, low=2230.0,
                close=2255.0, volume=3_036_839.0, data_source="test",
            ))
            session.commit()

    def _restore_env(self) -> None:
        from src.config import Config
        from src.storage import DatabaseManager

        for key, value in (("DATABASE_PATH", self._prev_db),
                           ("BHAVCOPY_RECONCILIATION_ENABLED", self._prev_flag)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        Config._instance = None
        DatabaseManager.reset_instance()

    @staticmethod
    def _published(idea_volume: float):
        return {
            "IDEA": BhavcopyRow("IDEA", 15.1, 15.3, 14.9, 15.19, idea_volume,
                                idea_volume * 0.28, 28.34),
            "TCS": BhavcopyRow("TCS", 2245.0, 2260.0, 2230.0, 2255.0, 3_036_839.0,
                               1_494_946.0, 49.23),
        }

    def _service(self, idea_volume: float):
        from src.services.bhavcopy_reconciliation_service import (
            BhavcopyReconciliationService,
        )
        published = self._published(idea_volume)
        return BhavcopyReconciliationService(fetch_bhavcopy=lambda _d: published)

    def _quarantine_rows(self):
        from src.storage import BarReconciliationRecord

        with self.db.get_session() as session:
            return [
                (r.code, r.field_name, r.stored_value, r.official_value, r.status)
                for r in session.query(BarReconciliationRecord).all()
            ]

    def test_disagreement_is_quarantined_and_the_bar_is_left_alone(self) -> None:
        summary = self._service(IDEA_PUBLISHED_VOLUME).reconcile(TRADE_DATE)

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["compared"], 2)
        self.assertEqual(summary["agreed"], 1)
        self.assertEqual(summary["quarantined"], 1)

        rows = self._quarantine_rows()
        self.assertEqual(len(rows), 1)
        code, field_name, stored, official, status = rows[0]
        self.assertEqual(code, "IDEA.NS")
        self.assertEqual(field_name, "volume")       # per field, not per bar
        self.assertEqual(stored, IDEA_STORED_VOLUME)
        self.assertEqual(official, IDEA_PUBLISHED_VOLUME)
        self.assertEqual(status, "quarantined")

        # The point of the feature: the disputed bar is evidence, not garbage.
        from src.storage import StockDaily
        with self.db.get_session() as session:
            bar = session.query(StockDaily).filter_by(
                code="IDEA.NS", date=TRADE_DATE).one()
            self.assertEqual(bar.volume, IDEA_STORED_VOLUME)

    def test_agreeing_bars_get_delivery_backfilled(self) -> None:
        self._service(IDEA_PUBLISHED_VOLUME).reconcile(TRADE_DATE)

        from src.storage import StockDaily
        with self.db.get_session() as session:
            tcs = session.query(StockDaily).filter_by(
                code="TCS.NS", date=TRADE_DATE).one()
            idea = session.query(StockDaily).filter_by(
                code="IDEA.NS", date=TRADE_DATE).one()
        self.assertAlmostEqual(tcs.delivery_pct, 49.23)
        # A quarantined bar is not trusted enough to enrich.
        self.assertIsNone(idea.delivery_pct)

    def test_rerunning_writes_no_duplicate(self) -> None:
        service = self._service(IDEA_PUBLISHED_VOLUME)
        service.reconcile(TRADE_DATE)
        service.reconcile(TRADE_DATE)
        self.assertEqual(len(self._quarantine_rows()), 1)

    def test_full_agreement_quarantines_nothing(self) -> None:
        summary = self._service(IDEA_STORED_VOLUME).reconcile(TRADE_DATE)
        self.assertEqual(summary["quarantined"], 0)
        self.assertEqual(self._quarantine_rows(), [])


if __name__ == "__main__":
    unittest.main()
