# -*- coding: utf-8 -*-
"""A signal's label must not depend on who triggered the run.

Signals written by the scheduled path carry no ``report_language``, and
``normalize_report_language(None)`` answers ``zh``. On a deployment configured
``REPORT_LANGUAGE=en`` that produced Chinese labels for system-triggered signals and
English ones for CLI-triggered signals **in the same run**:

    id=1   588200  cn  buy  买入  2026-08-16  trigger_source=system
    id=74  588200  cn  buy  买入  2026-08-31  trigger_source=system
    id=49  IDEA.NS in  buy  Buy   2026-08-24  trigger_source=cli

Every `buy` with `trigger_source=system` carried 买入; every `cli` one carried Buy. The
vault recorded this as a one-row artefact from the system's first day, which the second
row disproves. These tests pin the repair.
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.config import Config
from src.services.decision_signal_service import DecisionSignalService
from src.storage import DatabaseManager

RESOLVE = DecisionSignalService._resolve_report_language


def _config(language):
    return SimpleNamespace(report_language=language)


class TestUnspecifiedLanguageFollowsTheDeployment:
    def test_missing_language_uses_the_configured_one(self):
        """The case that produced id=74: no language in the payload, English deployment."""
        with patch("src.config.get_config", return_value=_config("en")):
            assert RESOLVE(None) == "en"

    def test_blank_language_is_treated_as_unspecified(self):
        with patch("src.config.get_config", return_value=_config("en")):
            assert RESOLVE("   ") == "en"

    def test_a_chinese_deployment_is_unchanged(self):
        """This fix must not quietly switch Chinese installations to English."""
        with patch("src.config.get_config", return_value=_config("zh")):
            assert RESOLVE(None) == "zh"

    def test_an_unconfigured_deployment_still_defaults_to_chinese(self):
        with patch("src.config.get_config", return_value=SimpleNamespace()):
            assert RESOLVE(None) == "zh"


class TestExplicitLanguageStillWins:
    def test_a_payload_language_beats_the_configuration(self):
        with patch("src.config.get_config", return_value=_config("en")):
            assert RESOLVE("zh") == "zh"

    def test_the_other_direction_too(self):
        with patch("src.config.get_config", return_value=_config("zh")):
            assert RESOLVE("en") == "en"


class TestItCannotBreakAWrite:
    def test_a_broken_config_falls_back_instead_of_raising(self):
        """A configuration problem must not stop a signal being stored."""
        with patch("src.config.get_config", side_effect=RuntimeError("config unavailable")):
            assert RESOLVE(None) == "zh"


@pytest.fixture()
def isolated_db(tmp_path):
    old_database_path = os.environ.get("DATABASE_PATH")
    old_language = os.environ.get("REPORT_LANGUAGE")
    os.environ["DATABASE_PATH"] = str(tmp_path / "label_language.db")
    os.environ["REPORT_LANGUAGE"] = "en"
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        for key, previous in (("DATABASE_PATH", old_database_path), ("REPORT_LANGUAGE", old_language)):
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _system_payload(**overrides):
    """A payload shaped like the scheduled path's: no ``report_language`` anywhere."""
    payload = {
        "stock_code": "588200",
        "stock_name": "中信建投",
        "market": "cn",
        "source_type": "analysis",
        "trigger_source": "system",
        "action": "buy",
        "confidence": 0.7,
        "score": 70,
        "horizon": "5d",
        "reason": "system-triggered write, exactly as id=74 was produced",
    }
    payload.update(overrides)
    return payload


class TestTheWritePathItself:
    """Not the resolver in isolation - the path that actually produced id=74."""

    def test_a_system_written_signal_is_labelled_in_the_configured_language(self, isolated_db):
        service = DecisionSignalService(db_manager=isolated_db)

        item = service.create_signal(_system_payload())["item"]

        assert item["action"] == "buy"
        assert item["action_label"] == "Buy", (
            "a signal written without a report_language was labelled in Chinese on an "
            "English deployment - this is the id=74 defect"
        )

    def test_an_explicit_language_in_the_payload_still_wins(self, isolated_db):
        service = DecisionSignalService(db_manager=isolated_db)

        item = service.create_signal(_system_payload(report_language="zh"))["item"]

        assert item["action_label"] == "买入"

    def test_a_caller_supplied_label_is_still_preserved(self, isolated_db):
        """Known gap, pinned deliberately: a supplied label is display text and wins.

        An upstream that hands us a Chinese label on an English deployment still gets it
        stored. That is a separate decision from this fix, and this test exists so the
        behaviour is a choice on record rather than an accident.
        """
        service = DecisionSignalService(db_manager=isolated_db)

        item = service.create_signal(_system_payload(action_label="买入"))["item"]

        assert item["action_label"] == "买入"
