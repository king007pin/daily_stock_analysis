# -*- coding: utf-8 -*-
"""
Tests for src/services/nse_bhavcopy_client.py - the NSE bhavcopy fetch/parse/compare client.

Contract under test:
    parse_bhavcopy(text, *, expected_date=None) -> dict[str, BhavcopyRow]
    fetch_bhavcopy(trade_date, *, session=None, timeout=...) -> dict[str, BhavcopyRow]
    compare_bar(stored, published, ...) -> BarComparison
    compare_bars(stored_bars, published, ...) -> dict[str, BarComparison]
    BhavcopyUnavailable

Fully offline. Every byte of exchange data used here is REAL: the two fixtures under
tests/fixtures/nse_bhavcopy/ are verbatim row subsets of NSE's own published
sec_bhavdata_full files for 2026-08-25 and 2026-07-10, and every stored bar asserted
against them was read out of the live stock_daily table. Nothing is estimated
(AGENTS.md Sec 1.3). The only synthetic CSV in this file is a deliberately fake
symbol used to exercise the '-' null cell, and it is labelled as such.

The three behaviours these tests exist to protect:
  1. The archive serves the PREVIOUS session's file for any non-trading date instead
     of 404ing, so a DATE1 mismatch must raise rather than silently compare the wrong
     session.
  2. stock_daily stores dividend-ADJUSTED prices while bhavcopy publishes RAW prices,
     so the comparison must accept a per-bar scalar without going blind to real errors.
  3. Volume carries no adjustment, and the one confirmed corruption in this dataset
     (IDEA.NS 2026-08-25) is a volume error that must be caught.
"""

from datetime import date
from pathlib import Path

import pytest

from src.services import nse_bhavcopy_client as client
from src.services.nse_bhavcopy_client import (
    BhavcopyRow,
    BhavcopyUnavailable,
    compare_bar,
    compare_bars,
    parse_bhavcopy,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "nse_bhavcopy"
BHAV_2026_08_25 = FIXTURE_DIR / "sec_bhavdata_full_25082026_sample.csv"
BHAV_2026_07_10 = FIXTURE_DIR / "sec_bhavdata_full_10072026_sample.csv"

DATE_25_AUG = date(2026, 8, 25)
DATE_10_JUL = date(2026, 7, 10)

# Bars read verbatim out of the live stock_analysis.db stock_daily table.
# yfinance hands back float32, hence the long tails.
STORED_2026_08_25 = {
    "RELIANCE.NS": dict(
        open=1304.300048828125, high=1317.0999755859375,
        low=1300.0, close=1317.0, volume=7115355.0,
    ),
    # The one confirmed real error in this dataset: prices agree exactly, volume
    # is stored at 30% of the exchange's published figure.
    "IDEA.NS": dict(
        open=14.09000015258789, high=15.3100004196167,
        low=14.079999923706055, close=15.1899995803833, volume=460728374.0,
    ),
}

# 2026-07-10 predates TCS's 2026-07-15 ex-dividend, so both TCS and HAL are stored
# dividend-adjusted here while the exchange published the raw traded prices.
STORED_2026_07_10 = {
    "TCS.NS": dict(
        open=2093.7200783959847, high=2121.666943317309,
        low=2053.341620075063, close=2057.717529296875, volume=6714551.0,
    ),
    "HAL.NS": dict(
        open=4387.110292758577, high=4515.74953031723,
        low=4387.110292758577, close=4497.68603515625, volume=1078390.0,
    ),
    "RELIANCE.NS": dict(
        open=1291.9000244140625, high=1311.0999755859375,
        low=1287.800048828125, close=1307.800048828125, volume=8412537.0,
    ),
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture
def rows_25_aug():
    return parse_bhavcopy(_read(BHAV_2026_08_25), expected_date=DATE_25_AUG)


@pytest.fixture
def rows_10_jul():
    return parse_bhavcopy(_read(BHAV_2026_07_10), expected_date=DATE_10_JUL)


class _StubResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _StubSession:
    """Records the request instead of making one."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self._raises is not None:
            raise self._raises
        return self._response


# ---------------------------------------------------------------------------
# parse_bhavcopy
# ---------------------------------------------------------------------------


class TestParseBhavcopy:
    def test_keys_are_bare_eq_symbols(self, rows_25_aug):
        """EQ only, keyed by NSE SYMBOL with no .NS suffix."""
        assert "RELIANCE" in rows_25_aug
        assert "RELIANCE.NS" not in rows_25_aug
        # The fixture also carries a BE row (3IINFOLTD) and a GS row (1018GS2026);
        # neither is rolling-settlement equity, so neither may appear.
        assert "3IINFOLTD" not in rows_25_aug
        assert "1018GS2026" not in rows_25_aug
        assert len(rows_25_aug) == 12

    def test_fields_match_the_published_row_exactly(self, rows_25_aug):
        """No rounding, no derivation - the row is what NSE printed."""
        reliance = rows_25_aug["RELIANCE"]
        assert isinstance(reliance, BhavcopyRow)
        assert reliance.symbol == "RELIANCE"
        assert reliance.open == 1304.30
        assert reliance.high == 1317.10
        assert reliance.low == 1300.00
        # CLOSE_PRICE, not LAST_PRICE - both are 1317.00 for RELIANCE, so assert
        # on a row where they differ to prove the right column was read.
        assert reliance.close == 1317.00
        assert reliance.volume == 7115355.0
        assert reliance.delivery_qty == 3766609.0
        assert reliance.delivery_pct == 52.94

    def test_reads_close_price_not_last_price(self, rows_25_aug):
        """ALOKINDS closed at 11.29 after a last trade of 11.33."""
        alok = rows_25_aug["ALOKINDS"]
        assert alok.close == 11.29
        assert alok.close != 11.33

    def test_dividend_free_row_is_untouched_raw_price(self, rows_10_jul):
        tcs = rows_10_jul["TCS"]
        assert tcs.close == 2069.00
        assert tcs.volume == 6714551.0

    def test_null_delivery_cells_become_none_not_zero(self):
        """A '-' means the exchange did not publish the value, never zero.

        The row below is SYNTHETIC (ZZTESTSYM is not a listed scrip); it exists
        only to drive the '-' branch of the parser and is not market data.
        """
        text = (
            "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, "
            "LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, "
            "NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
            "ZZTESTSYM, EQ, 25-Aug-2026, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, "
            "10.00, 100, 0.01, 1, -, -\n"
        )
        rows = parse_bhavcopy(text, expected_date=DATE_25_AUG)
        assert rows["ZZTESTSYM"].delivery_qty is None
        assert rows["ZZTESTSYM"].delivery_pct is None
        assert rows["ZZTESTSYM"].volume == 100.0

    def test_expected_date_matching_file_is_accepted(self):
        rows = parse_bhavcopy(_read(BHAV_2026_08_25), expected_date=DATE_25_AUG)
        assert rows

    def test_stale_file_raises_rather_than_comparing_the_wrong_session(self):
        """The archive's non-trading-day behaviour, verified 2026-08-31.

        sec_bhavdata_full_<holiday>.csv is byte-identical to the previous
        session's file (Muharram 2026-06-26 == 2026-06-25; Sunday 2026-08-30 ==
        Friday 2026-08-28; Republic Day 2026-01-26 == 2026-01-23) and returns
        HTTP 200. Only the DATE1 column reveals it. Asking for 2026-08-26 and
        receiving the 2026-08-25 file must fail loudly.
        """
        with pytest.raises(BhavcopyUnavailable) as excinfo:
            parse_bhavcopy(_read(BHAV_2026_08_25), expected_date=date(2026, 8, 26))
        message = str(excinfo.value)
        assert "2026-08-26" in message
        assert "2026-08-25" in message

    def test_without_expected_date_no_date_assertion_is_made(self):
        rows = parse_bhavcopy(_read(BHAV_2026_08_25))
        assert "TCS" in rows

    def test_html_body_raises(self):
        """A 404 from the archive is an HTML page, not a CSV."""
        with pytest.raises(BhavcopyUnavailable, match="not a bhavcopy CSV"):
            parse_bhavcopy("<!DOCTYPE html>\n<html lang=\"en\">\n</html>\n")

    def test_empty_body_raises(self):
        with pytest.raises(BhavcopyUnavailable, match="empty"):
            parse_bhavcopy("   \n\n")

    def test_header_missing_a_required_column_raises(self):
        text = "SYMBOL, SERIES, DATE1, OPEN_PRICE\nTCS, EQ, 25-Aug-2026, 2305.00\n"
        with pytest.raises(BhavcopyUnavailable, match="missing required columns"):
            parse_bhavcopy(text)

    def test_file_with_no_eq_rows_raises(self):
        lines = _read(BHAV_2026_08_25).splitlines()
        header = lines[0]
        non_eq = [line for line in lines[1:] if ", EQ," not in line]
        with pytest.raises(BhavcopyUnavailable, match="no parsable EQ rows"):
            parse_bhavcopy("\n".join([header] + non_eq))

    def test_unparsable_price_row_is_dropped_not_guessed(self):
        """SYNTHETIC row (ZZBROKEN is not a listed scrip): a blank OPEN_PRICE
        must remove the row, never become 0.0."""
        text = (
            "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, "
            "LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, "
            "NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
            "ZZBROKEN, EQ, 25-Aug-2026, 10.00, , 10.00, 10.00, 10.00, 10.00, "
            "10.00, 100, 0.01, 1, 50, 50.00\n"
            "ZZGOOD, EQ, 25-Aug-2026, 10.00, 10.00, 10.00, 10.00, 10.00, 10.00, "
            "10.00, 100, 0.01, 1, 50, 50.00\n"
        )
        rows = parse_bhavcopy(text)
        assert "ZZBROKEN" not in rows
        assert "ZZGOOD" in rows


# ---------------------------------------------------------------------------
# compare_bar - the dividend-adjustment problem
# ---------------------------------------------------------------------------


class TestCompareUnadjustedBars:
    def test_clean_bar_passes(self, rows_25_aug):
        result = compare_bar(STORED_2026_08_25["RELIANCE.NS"], rows_25_aug["RELIANCE"])
        assert result.ok
        assert result.comparable
        assert result.price_match
        assert result.volume_match
        assert result.reasons == ()
        assert result.adjustment_factor == pytest.approx(1.0, abs=1e-6)


class TestCompareDividendAdjustedBars:
    """The core problem: adjusted storage vs raw publication."""

    def test_tcs_pre_ex_dividend_bar_passes_despite_a_0_55_percent_raw_gap(self, rows_10_jul):
        """TCS.NS 2026-07-10 sits 0.545% below the exchange's raw close.

        This is the exact bar that makes a naive comparison useless. The stored
        close is 2057.72 against a published 2069.00 because TCS's Rs 12 dividend
        (ex-date 2026-07-15) is applied retroactively. It must NOT be a mismatch.
        """
        published = rows_10_jul["TCS"]
        stored = STORED_2026_07_10["TCS.NS"]

        raw_gap_pct = abs(stored["close"] - published.close) / published.close * 100
        assert raw_gap_pct > 0.5, "fixture no longer exercises the adjustment problem"

        result = compare_bar(stored, published)
        assert result.ok, result.reasons
        assert result.adjustment_factor == pytest.approx(0.994547, abs=1e-5)
        assert not result.adjustment_flagged

    def test_hal_adjusted_bar_passes_with_its_own_factor(self, rows_10_jul):
        result = compare_bar(STORED_2026_07_10["HAL.NS"], rows_10_jul["HAL"])
        assert result.ok, result.reasons
        assert result.adjustment_factor == pytest.approx(0.997978, abs=1e-5)

    def test_two_codes_on_one_day_carry_different_factors(self, rows_10_jul):
        """The factor is per code and per bar, never a global constant."""
        tcs = compare_bar(STORED_2026_07_10["TCS.NS"], rows_10_jul["TCS"])
        hal = compare_bar(STORED_2026_07_10["HAL.NS"], rows_10_jul["HAL"])
        reliance = compare_bar(STORED_2026_07_10["RELIANCE.NS"], rows_10_jul["RELIANCE"])
        assert tcs.adjustment_factor != hal.adjustment_factor
        assert reliance.adjustment_factor == pytest.approx(1.0, abs=1e-6)
        assert all(r.ok for r in (tcs, hal, reliance))

    def test_the_factor_is_shared_by_all_four_price_fields(self, rows_10_jul):
        """Why this method works at all: one scalar covers open/high/low/close."""
        result = compare_bar(STORED_2026_07_10["TCS.NS"], rows_10_jul["TCS"])
        ratios = list(result.price_ratios.values())
        assert len(ratios) == 4
        assert max(ratios) - min(ratios) < 1e-6

    def test_a_flat_tolerance_wide_enough_for_tcs_would_hide_a_real_error(self, rows_10_jul):
        """Documents why the rejected option is rejected.

        A flat band must exceed TCS's 0.545% dividend gap to stay quiet, so it is
        structurally blind to a 0.4% price error. The factor method catches it.
        """
        published = rows_10_jul["RELIANCE"]
        stored = dict(STORED_2026_07_10["RELIANCE.NS"])
        stored["close"] = stored["close"] * 1.004

        error_pct = abs(stored["close"] - published.close) / published.close * 100
        assert error_pct < 0.545, "this error would also be caught by a 0.545% flat band"

        result = compare_bar(stored, published)
        assert not result.price_match
        assert "price_mismatch" in result.reasons


class TestCompareCatchesRealErrors:
    def test_confirmed_idea_volume_corruption_is_caught(self, rows_25_aug):
        """IDEA.NS 2026-08-25: stored 460,728,374 vs published 1,534,470,198.

        The prices on this bar are correct to the paisa, so a price-only check
        would have passed it. Volume is the field that carries no corporate-action
        adjustment, which is exactly why it is checked strictly.
        """
        published = rows_25_aug["IDEA"]
        assert published.volume == 1534470198.0

        result = compare_bar(STORED_2026_08_25["IDEA.NS"], published)
        assert result.price_match, "prices on this bar are correct"
        assert not result.volume_match
        assert result.reasons == ("volume_mismatch",)
        assert result.volume_diff_pct == pytest.approx(-69.975, abs=0.01)
        assert not result.ok

    def test_one_percent_close_only_error_is_caught(self, rows_25_aug):
        published = rows_25_aug["RELIANCE"]
        stored = dict(STORED_2026_08_25["RELIANCE.NS"])
        stored["close"] = stored["close"] * 1.01
        result = compare_bar(stored, published)
        assert not result.price_match
        assert "price_mismatch" in result.reasons

    def test_stale_previous_session_bar_is_caught(self, rows_25_aug, rows_10_jul):
        """A bar from a different session does not share one scalar with today's."""
        result = compare_bar(STORED_2026_07_10["RELIANCE.NS"], rows_25_aug["RELIANCE"])
        assert not result.price_match
        assert "price_mismatch" in result.reasons

    def test_a_single_corrupt_field_cannot_drag_the_factor_onto_itself(self, rows_25_aug):
        """The factor is a median of four ratios, so one bad field stays visible."""
        published = rows_25_aug["RELIANCE"]
        stored = dict(STORED_2026_08_25["RELIANCE.NS"])
        stored["high"] = stored["high"] * 1.05
        result = compare_bar(stored, published)
        assert result.adjustment_factor == pytest.approx(1.0, abs=1e-6)
        assert not result.price_match

    def test_zero_published_volume_with_stored_trades_is_a_mismatch(self):
        published = BhavcopyRow(
            symbol="ZZTESTSYM", open=10.0, high=10.0, low=10.0, close=10.0,
            volume=0.0, delivery_qty=None, delivery_pct=None,
        )
        stored = dict(open=10.0, high=10.0, low=10.0, close=10.0, volume=5000.0)
        result = compare_bar(stored, published)
        assert not result.volume_match
        assert result.volume_diff_pct is None
        assert "volume_mismatch" in result.reasons


class TestCompareEdgeCases:
    def test_missing_price_field_is_not_comparable_rather_than_passing(self, rows_25_aug):
        stored = dict(STORED_2026_08_25["RELIANCE.NS"])
        stored["low"] = None
        result = compare_bar(stored, rows_25_aug["RELIANCE"])
        assert not result.comparable
        assert not result.price_match
        assert result.adjustment_factor is None
        assert "price_not_comparable" in result.reasons
        assert not result.ok

    def test_missing_stored_volume_is_reported_not_ignored(self, rows_25_aug):
        stored = dict(STORED_2026_08_25["RELIANCE.NS"])
        stored["volume"] = None
        result = compare_bar(stored, rows_25_aug["RELIANCE"])
        assert not result.volume_match
        assert "volume_not_comparable" in result.reasons

    def test_split_scale_factor_is_flagged_for_review_not_silently_accepted(self, rows_25_aug):
        """A self-consistent k far from 1.0 is a structural event, not a dividend."""
        published = rows_25_aug["RELIANCE"]
        stored = {
            field: getattr(published, field) * 0.5 for field in ("open", "high", "low", "close")
        }
        stored["volume"] = published.volume
        result = compare_bar(stored, published)
        assert result.price_match, "all four fields still share one scalar"
        assert result.adjustment_flagged
        assert "adjustment_out_of_band" in result.reasons
        assert not result.ok

    def test_ordinary_dividend_factors_are_never_flagged(self, rows_10_jul):
        for code, symbol in (("TCS.NS", "TCS"), ("HAL.NS", "HAL")):
            result = compare_bar(STORED_2026_07_10[code], rows_10_jul[symbol])
            assert not result.adjustment_flagged, code


class TestCompareBars:
    def test_maps_ns_codes_onto_bhavcopy_symbols(self, rows_25_aug):
        results = compare_bars(STORED_2026_08_25, rows_25_aug)
        assert set(results) == {"RELIANCE.NS", "IDEA.NS"}
        assert results["RELIANCE.NS"].ok
        assert not results["IDEA.NS"].ok

    def test_code_absent_from_bhavcopy_is_omitted_not_flagged(self, rows_25_aug):
        stored = dict(STORED_2026_08_25)
        stored["NOTLISTED.NS"] = dict(open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0)
        results = compare_bars(stored, rows_25_aug)
        assert "NOTLISTED.NS" not in results

    def test_symbol_from_code_strips_only_the_ns_suffix(self):
        assert client.symbol_from_code("IDEA.NS") == "IDEA"
        assert client.symbol_from_code("IDEA") == "IDEA"
        assert client.symbol_from_code("600519") == "600519"


# ---------------------------------------------------------------------------
# fetch_bhavcopy
# ---------------------------------------------------------------------------


class TestFetchBhavcopy:
    def test_url_shape(self):
        assert client.bhavcopy_url(DATE_25_AUG) == (
            "https://nsearchives.nseindia.com/products/content/"
            "sec_bhavdata_full_25082026.csv"
        )

    def test_disabled_by_default_and_never_touches_the_network(self, monkeypatch):
        """The switch is what keeps the offline suite offline."""
        monkeypatch.setattr(client, "_fetch_enabled", lambda: False)
        session = _StubSession(_StubResponse(200, _read(BHAV_2026_08_25)))
        with pytest.raises(BhavcopyUnavailable, match="NSE_BHAVCOPY_FETCH_ENABLED"):
            client.fetch_bhavcopy(DATE_25_AUG, session=session)
        assert session.calls == [], "a disabled fetch must not issue a request"

    def test_enabled_fetch_sends_the_browser_headers_nse_requires(self, monkeypatch):
        """Without User-Agent + Referer the archive hangs to the read timeout."""
        monkeypatch.setattr(client, "_fetch_enabled", lambda: True)
        session = _StubSession(_StubResponse(200, _read(BHAV_2026_08_25)))
        rows = client.fetch_bhavcopy(DATE_25_AUG, session=session)

        assert rows["IDEA"].volume == 1534470198.0
        assert len(session.calls) == 1
        call = session.calls[0]
        assert call["url"].endswith("sec_bhavdata_full_25082026.csv")
        assert "Mozilla" in call["headers"]["User-Agent"]
        assert call["headers"]["Referer"] == "https://www.nseindia.com/"
        assert call["timeout"] == client.DEFAULT_TIMEOUT_SECONDS

    def test_custom_timeout_is_passed_through(self, monkeypatch):
        monkeypatch.setattr(client, "_fetch_enabled", lambda: True)
        session = _StubSession(_StubResponse(200, _read(BHAV_2026_08_25)))
        client.fetch_bhavcopy(DATE_25_AUG, session=session, timeout=7)
        assert session.calls[0]["timeout"] == 7

    def test_non_200_raises(self, monkeypatch):
        monkeypatch.setattr(client, "_fetch_enabled", lambda: True)
        session = _StubSession(_StubResponse(404, "<!DOCTYPE html><html></html>"))
        with pytest.raises(BhavcopyUnavailable, match="HTTP 404"):
            client.fetch_bhavcopy(DATE_25_AUG, session=session)

    def test_transport_failure_raises_bhavcopy_unavailable(self, monkeypatch):
        monkeypatch.setattr(client, "_fetch_enabled", lambda: True)
        session = _StubSession(raises=OSError("read timed out"))
        with pytest.raises(BhavcopyUnavailable, match="read timed out"):
            client.fetch_bhavcopy(DATE_25_AUG, session=session)

    def test_stale_holiday_file_raises_through_fetch(self, monkeypatch):
        """A 200 carrying the previous session must not become a silent comparison."""
        monkeypatch.setattr(client, "_fetch_enabled", lambda: True)
        session = _StubSession(_StubResponse(200, _read(BHAV_2026_08_25)))
        with pytest.raises(BhavcopyUnavailable, match="non-trading dates"):
            client.fetch_bhavcopy(date(2026, 8, 26), session=session)

    def test_flag_defaults_to_off_in_config(self):
        from src.config import Config

        assert Config().nse_bhavcopy_fetch_enabled is False
