# -*- coding: utf-8 -*-
"""Per-horizon neutral band configuration parsing."""

from src.config import parse_neutral_band_by_horizon


def test_unset_yields_empty_mapping_so_behaviour_is_unchanged() -> None:
    assert parse_neutral_band_by_horizon(None) == {}
    assert parse_neutral_band_by_horizon("") == {}
    assert parse_neutral_band_by_horizon("   ") == {}


def test_parses_horizon_band_pairs() -> None:
    parsed = parse_neutral_band_by_horizon("intraday:0.6,1d:1.0,3d:2.0,5d:2.5,10d:3.0")

    assert parsed == {"intraday": 0.6, "1d": 1.0, "3d": 2.0, "5d": 2.5, "10d": 3.0}


def test_tolerates_whitespace_and_trailing_separators() -> None:
    assert parse_neutral_band_by_horizon(" intraday : 0.6 , 1d:1.0 , ") == {
        "intraday": 0.6,
        "1d": 1.0,
    }


def test_skips_malformed_entries_without_dropping_valid_ones() -> None:
    """A typo in one horizon must not silently disable the others."""
    parsed = parse_neutral_band_by_horizon("intraday:0.6,garbage,1d:abc,3d:-1,5d:2.5")

    assert parsed == {"intraday": 0.6, "5d": 2.5}


def test_zero_band_is_allowed() -> None:
    assert parse_neutral_band_by_horizon("intraday:0") == {"intraday": 0.0}
