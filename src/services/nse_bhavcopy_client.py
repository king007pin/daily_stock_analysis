# -*- coding: utf-8 -*-
"""
NSE full bhavcopy (``sec_bhavdata_full``) fetch, parse and bar-comparison client.

Why this exists
---------------
Everything upstream of ``stock_daily`` comes from one vendor (yfinance). A
vendor-vs-vendor check cannot detect a vendor that is simply wrong. NSE
publishes the exchange's own end-of-day file, so it is ground truth rather than
a second opinion, and it is the only source in this repo that can falsify a
stored Indian bar.

The file is public, needs no API key, and lives at::

    https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv

Zero-Hallucination Invariant (AGENTS.md Sec 1.3): nothing here estimates,
interpolates or back-fills. A row that cannot be parsed is dropped, a file that
cannot be trusted raises ``BhavcopyUnavailable``, and a comparison that cannot
be made reports "not comparable" rather than a made-up verdict.

Three verified traps this module exists to handle
-------------------------------------------------
1. **The archive never 404s for a non-trading day - it serves the PREVIOUS
   session's file under the requested date's name.** Verified 2026-08-31:
   ``sec_bhavdata_full_26062026.csv`` (Muharram, market closed) is byte-identical
   to ``sec_bhavdata_full_25062026.csv`` and its own ``DATE1`` column reads
   ``25-Jun-2026``. The same holds for Sunday 2026-08-30 (serves 28-Aug) and
   Republic Day 2026-01-26 (serves 23-Jan). A future date returns HTML with
   status 404. Therefore ``DATE1`` is validated against the requested date and a
   mismatch raises - without that check every reconciliation on a holiday would
   compare Thursday's stored bars to Wednesday's exchange data.
2. **Missing browser headers hang rather than fail.** A request without a
   ``User-Agent`` + ``Referer`` pair does not get rejected; it read-times-out.
   Both headers are mandatory, and a timeout is always supplied.
3. **``stock_daily`` stores DIVIDEND-ADJUSTED closes; bhavcopy publishes RAW
   traded prices.** See ``compare_bar`` below for the comparison method and the
   evidence behind it.

Network access is opt-in via ``NSE_BHAVCOPY_FETCH_ENABLED`` (default false), so
importing or unit-testing this module can never reach the network; the offline
suite runs entirely against ``parse_bhavcopy`` and the committed fixtures.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

BHAVCOPY_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{stamp}.csv"
)

# NSE serves this file only to something that looks like a browser arriving from
# its own site. Without both headers the connection stalls until the read
# timeout (verified 2026-08-31), which is a far worse failure mode than a 403.
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com/",
    "Accept": "text/csv,application/csv,*/*",
}

DEFAULT_TIMEOUT_SECONDS = 30

# Only the rolling-settlement equity segment. 'BE' (trade-for-trade), 'GS'
# (government securities), 'SM'/'ST' (SME) and the rest are different
# instruments with different tick and settlement semantics; ``stock_daily``'s
# ``.NS`` codes correspond to EQ.
EQ_SERIES = "EQ"

_REQUIRED_COLUMNS = (
    "SYMBOL",
    "SERIES",
    "DATE1",
    "OPEN_PRICE",
    "HIGH_PRICE",
    "LOW_PRICE",
    "CLOSE_PRICE",
    "TTL_TRD_QNTY",
    "DELIV_QTY",
    "DELIV_PER",
)

# NSE writes DATE1 as ``25-Aug-2026``.
_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

# A cell that is exactly one of these means "the exchange did not publish this
# value", not zero. DELIV_QTY / DELIV_PER are blank as '-' for series where
# delivery is not reported.
_NULL_CELLS = frozenset({"-", "", "NA", "N/A"})

PRICE_FIELDS: Tuple[str, ...] = ("open", "high", "low", "close")


class BhavcopyUnavailable(Exception):
    """Raised when a trustworthy bhavcopy for the requested date cannot be produced.

    Covers every reason: fetch disabled, transport error, non-CSV response, an
    unparsable header, an empty EQ section, and - the important one - a file
    whose own ``DATE1`` is not the date that was asked for.
    """


@dataclass(frozen=True)
class BhavcopyRow:
    """One EQ row of NSE's own end-of-day file.

    ``close`` is the RAW traded close as published by the exchange. It is not
    dividend- or split-adjusted, and must never be compared naively against
    ``stock_daily.close`` (which is adjusted). Use :func:`compare_bar`.
    """

    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    delivery_qty: Optional[float]
    delivery_pct: Optional[float]


# ---------------------------------------------------------------------------
# Comparison thresholds
#
# All four numbers below were fitted against real data on 2026-08-31/09-01:
# 702 stored ``.NS`` bars from ``stock_daily`` (13 codes, 2026-04-27..2026-08-31)
# against the 88 bhavcopy files whose own DATE1 matched the requested date.
# ---------------------------------------------------------------------------

# Price residual tolerance, in rupees: |stored - k * published| must not exceed
# PRICE_ABS_TOLERANCE_INR + PRICE_REL_TOLERANCE * published.
#
# The absolute term is NSE's own publication precision: prices are printed to
# 2dp, so a half-tick on each side of the comparison is 0.005 + 0.005 = 0.01.
# The relative term absorbs float32 representation error - yfinance hands back
# float32, which at HAL's ~4900 price level already carries ~2.4e-4 of absolute
# error, and the ratio arithmetic below adds a few more ulps.
#
# Measured headroom: across all 702 real bars the worst residual reached 2.4% of
# this tolerance (HAL 2026-07-08). Sensitivity, measured by injecting a
# close-only error into each of the same 702 bars: 100% of 1.0% errors are
# caught, 90.7% of 0.25% errors, 55.6% of 0.1% errors. The misses are all
# sub-Rs-5 scrips, where 0.1% is below the exchange's own tick resolution.
PRICE_ABS_TOLERANCE_INR = 0.01
PRICE_REL_TOLERANCE = 1e-6

# Volume tolerance, as a percentage of the published volume.
#
# Volume needs no corporate-action adjustment at all, which makes it the
# sharpest check available. Measured: 690 of 702 stored volumes match the
# exchange EXACTLY. Of the 12 that do not, 10 are the known IDEA.NS corruption
# (-57.7% to -94.8%, including the confirmed 2026-08-25 case: stored
# 460,728,374 vs published 1,534,470,198) and 2 are same-day capture drift on
# 2026-08-31 (-0.14% RELIANCE.NS, -0.50% TCS.NS, i.e. rows written before the
# exchange's final tally).
#
# 1% sits inside a 115x gap between the largest benign disagreement (0.50%) and
# the smallest real one (57.7%): 2x headroom above the noise, 57x margin below
# the errors. It is deliberately not 0% - that would flag every bar captured
# before the exchange finalises the session.
VOLUME_TOLERANCE_PCT = 1.0

# How far the per-bar adjustment factor k may drift from 1.0 before the bar is
# flagged for human review (advisory, not a mismatch).
#
# The largest genuine dividend adjustment observed is TCS.NS at k=0.994547
# (Rs 12 on a ~Rs 2200 share, ex-date 2026-07-15); HAL.NS shows k=0.997978. All
# other 11 codes sit at k=1.0 exactly. 25% leaves ~45x headroom over the largest
# observed dividend while still surfacing split/bonus-scale factors (k=0.5,
# k=0.1, k=10), which are structural events that deserve a human look rather
# than silent acceptance.
ADJUSTMENT_REVIEW_THRESHOLD = 0.25

# Ratio-consistency requires all four price fields. Fewer than this many usable
# ratios means "not comparable", never a pass.
_MIN_RATIOS_FOR_FACTOR = len(PRICE_FIELDS)


@dataclass(frozen=True)
class BarComparison:
    """Result of comparing one stored bar to one published bhavcopy row.

    ``adjustment_factor`` is the corporate-action scalar k inferred from the bar
    itself (see :func:`compare_bar`); it is ``None`` when the bar was not
    comparable. ``reasons`` is empty exactly when the bar is clean.
    """

    symbol: str
    comparable: bool
    adjustment_factor: Optional[float]
    price_ratios: Mapping[str, float]
    price_residuals: Mapping[str, float]
    price_match: bool
    adjustment_flagged: bool
    volume_diff_pct: Optional[float]
    volume_match: bool
    reasons: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        """True only when the bar was comparable and nothing disagreed."""
        return self.comparable and not self.reasons


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_date1(raw: str) -> Optional[date]:
    """``25-Aug-2026`` -> ``date(2026, 8, 25)``. None if unparsable."""
    parts = raw.strip().split("-")
    if len(parts) != 3:
        return None
    day, mon, year = parts
    try:
        month = _MONTH_ABBR.index(mon[:3].title()) + 1
        return date(int(year), month, int(day))
    except (ValueError, IndexError):
        return None


def _optional_float(raw: str) -> Optional[float]:
    """A published number, or None when the exchange printed a null marker.

    Never substitutes 0.0 for a missing value - a scrip with no reported
    delivery is not a scrip with zero delivery.
    """
    if raw.strip() in _NULL_CELLS:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_bhavcopy(
    text: str,
    *,
    expected_date: Optional[date] = None,
) -> Dict[str, BhavcopyRow]:
    """Parse a ``sec_bhavdata_full`` CSV body into ``{SYMBOL: BhavcopyRow}``.

    EQ series only, keyed by NSE symbol with no ``.NS`` suffix.

    ``expected_date`` is optional here so the parser stays usable on a file of
    unknown date, but :func:`fetch_bhavcopy` always supplies it. When given, a
    ``DATE1`` that disagrees raises ``BhavcopyUnavailable``: the archive serves
    the previous session's file for every non-trading date, and a silent
    off-by-one-session comparison is worse than no comparison.

    Raises ``BhavcopyUnavailable`` for a non-CSV body, a header missing any
    required column, a stale ``DATE1``, or a file with no usable EQ rows.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise BhavcopyUnavailable("bhavcopy body is empty")

    header = [cell.strip().upper() for cell in lines[0].split(",")]
    if header[:1] != ["SYMBOL"]:
        # A 404 from the archive is an HTML page served with status 404; a
        # gateway/WAF interstitial is HTML with status 200. Both land here.
        preview = lines[0][:80]
        raise BhavcopyUnavailable(f"response is not a bhavcopy CSV (first line: {preview!r})")

    index = {name: position for position, name in enumerate(header)}
    missing = [column for column in _REQUIRED_COLUMNS if column not in index]
    if missing:
        raise BhavcopyUnavailable(f"bhavcopy header missing required columns: {missing}")

    rows: Dict[str, BhavcopyRow] = {}
    file_dates: Set[date] = set()
    skipped = 0

    for line in lines[1:]:
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) < len(header):
            skipped += 1
            continue
        if cells[index["SERIES"]] != EQ_SERIES:
            continue

        row_date = _parse_date1(cells[index["DATE1"]])
        if row_date is None:
            skipped += 1
            continue
        file_dates.add(row_date)

        symbol = cells[index["SYMBOL"]]
        try:
            row = BhavcopyRow(
                symbol=symbol,
                open=float(cells[index["OPEN_PRICE"]]),
                high=float(cells[index["HIGH_PRICE"]]),
                low=float(cells[index["LOW_PRICE"]]),
                close=float(cells[index["CLOSE_PRICE"]]),
                volume=float(cells[index["TTL_TRD_QNTY"]]),
                delivery_qty=_optional_float(cells[index["DELIV_QTY"]]),
                delivery_pct=_optional_float(cells[index["DELIV_PER"]]),
            )
        except ValueError:
            # A price cell the exchange did not publish. Drop the row rather
            # than invent a number for it.
            skipped += 1
            continue

        if symbol in rows:
            logger.warning("[NseBhavcopy] duplicate EQ symbol %s in one file; keeping the first", symbol)
            continue
        rows[symbol] = row

    if not rows:
        raise BhavcopyUnavailable("bhavcopy contains no parsable EQ rows")

    if expected_date is not None:
        # Every row in one file carries the same DATE1; a set with anything
        # other than exactly the expected date means the archive handed back a
        # different session.
        if file_dates != {expected_date}:
            served = ", ".join(sorted(d.isoformat() for d in file_dates))
            raise BhavcopyUnavailable(
                f"bhavcopy for {expected_date.isoformat()} actually carries DATE1 {served} - "
                "NSE serves the previous session's file for non-trading dates"
            )

    if skipped:
        logger.debug("[NseBhavcopy] skipped %d unparsable rows", skipped)
    return rows


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def bhavcopy_url(trade_date: date) -> str:
    """Public archive URL for one trading session's full bhavcopy."""
    return BHAVCOPY_URL_TEMPLATE.format(stamp=trade_date.strftime("%d%m%Y"))


def _fetch_enabled() -> bool:
    try:
        from src.config import get_config

        return bool(getattr(get_config(), "nse_bhavcopy_fetch_enabled", False))
    except Exception:  # noqa: BLE001 - config must never be the reason we go online
        return False


def fetch_bhavcopy(
    trade_date: date,
    *,
    session=None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, BhavcopyRow]:
    """Download and parse one session's bhavcopy, keyed by SYMBOL, EQ series only.

    Network access is opt-in: with ``NSE_BHAVCOPY_FETCH_ENABLED`` unset this
    raises ``BhavcopyUnavailable`` without touching the network, which is what
    keeps the offline suite offline.

    ``session`` accepts a ``requests.Session``-shaped object for callers that
    want connection reuse across a date range; tests inject a stub instead of
    reaching the archive. Injecting a session does NOT bypass the flag - the
    switch is checked first, unconditionally, so no code path can reach the
    network while it is off.

    Raises ``BhavcopyUnavailable`` on any failure, including the stale-file case
    described in the module docstring. Never returns a partial or substituted
    result.
    """
    if not _fetch_enabled():
        raise BhavcopyUnavailable(
            "NSE bhavcopy fetch is disabled; set NSE_BHAVCOPY_FETCH_ENABLED=true to enable it"
        )

    url = bhavcopy_url(trade_date)
    try:
        if session is None:
            import requests

            response = requests.get(url, headers=_REQUEST_HEADERS, timeout=timeout)
        else:
            response = session.get(url, headers=_REQUEST_HEADERS, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - transport failures are all the same to callers
        raise BhavcopyUnavailable(f"bhavcopy fetch failed for {trade_date.isoformat()}: {exc}") from exc

    status = getattr(response, "status_code", None)
    if status != 200:
        raise BhavcopyUnavailable(
            f"bhavcopy fetch for {trade_date.isoformat()} returned HTTP {status}"
        )

    return parse_bhavcopy(response.text, expected_date=trade_date)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare_bar(
    stored: Mapping[str, Optional[float]],
    published: BhavcopyRow,
    *,
    price_abs_tolerance: float = PRICE_ABS_TOLERANCE_INR,
    price_rel_tolerance: float = PRICE_REL_TOLERANCE,
    volume_tolerance_pct: float = VOLUME_TOLERANCE_PCT,
    adjustment_review_threshold: float = ADJUSTMENT_REVIEW_THRESHOLD,
) -> BarComparison:
    """Compare one stored bar against the exchange's published row.

    ``stored`` is a mapping with ``open``/``high``/``low``/``close``/``volume``
    (a ``StockDaily`` row works directly via ``vars()`` or an explicit dict).

    THE ADJUSTMENT PROBLEM AND WHY THIS METHOD
    ------------------------------------------
    ``stock_daily`` stores dividend-adjusted prices; bhavcopy publishes raw
    traded prices. Subtracting them flags every dividend payer. Three options
    were weighed against 702 real bars:

    *A flat percentage tolerance* is not viable. TCS.NS sits 0.545% below the
    raw price for every session before its 2026-07-15 ex-dividend, so a flat
    band must exceed 0.6% to avoid a permanent false alarm - and is then
    structurally blind to every price error smaller than 0.6%. Worse, the band
    is unbounded in principle: a larger dividend or any split moves the gap
    arbitrarily far, so no fixed number is both quiet and useful.

    *Volume-only comparison* is safe but throws away the price check entirely.
    It would still have caught the one confirmed error (IDEA.NS 2026-08-25
    volume), but it cannot catch a wrong price at correct volume.

    *This module uses per-bar adjustment-factor detection*, which needs no
    corporate-action feed and no history:

      1. yfinance applies the adjustment as ONE scalar k to all four prices of a
         bar. So a correctly stored bar satisfies
         ``stored_f = k * published_f`` for f in (open, high, low, close),
         with the SAME k.
      2. k is estimated as the median of the four per-field ratios - median, not
         mean, so a single corrupted field cannot drag k onto itself and hide.
      3. Each field is then checked in rupees against that single k.

    A dividend adjustment passes (all four fields share k). A stale bar, a wrong
    field, or a different session's data fails, because those do not produce one
    common scalar.

    Evidence (2026-04-27..2026-08-31, 13 ``.NS`` codes, 702 date-validated bars):
    the per-bar spread between the four field ratios never exceeded 1.05e-7 -
    pure float noise - and only three distinct k values occur across the whole
    sample: 1.0, 0.997978 (HAL.NS), 0.994547 (TCS.NS). All 702 bars pass with a
    worst-case residual of 2.4% of the tolerance. Injecting a close-only error
    into the same 702 bars catches 100% at 1.0% and 90.7% at 0.25%, versus a
    flat band that cannot go below 0.6% at all. Replacing each bar with the
    previous session's bar - the stale-data failure mode - is caught 98.3% of
    the time (the residue is genuinely near-identical consecutive sessions in
    sub-Rs-2 scrips).

    Volume is compared directly: no corporate action affects a share count.
    """
    symbol = published.symbol
    reasons: List[str] = []

    ratios: Dict[str, float] = {}
    for field in PRICE_FIELDS:
        stored_value = stored.get(field)
        published_value = getattr(published, field)
        if stored_value is None or published_value is None or published_value <= 0:
            continue
        ratios[field] = float(stored_value) / float(published_value)

    stored_volume = stored.get("volume")
    volume_diff_pct: Optional[float] = None
    volume_match = True
    if stored_volume is None or published.volume is None:
        volume_match = False
        reasons.append("volume_not_comparable")
    elif published.volume <= 0:
        # A genuinely untraded session: any stored volume other than 0 is wrong,
        # but a percentage of zero is undefined, so compare absolutely.
        volume_diff_pct = None
        if float(stored_volume) != 0.0:
            volume_match = False
            reasons.append("volume_mismatch")
    else:
        volume_diff_pct = (float(stored_volume) - published.volume) / published.volume * 100.0
        if abs(volume_diff_pct) > volume_tolerance_pct:
            volume_match = False
            reasons.append("volume_mismatch")

    if len(ratios) < _MIN_RATIOS_FOR_FACTOR:
        # Fewer than four usable prices: report honestly rather than pass a bar
        # on partial evidence.
        reasons.append("price_not_comparable")
        return BarComparison(
            symbol=symbol,
            comparable=False,
            adjustment_factor=None,
            price_ratios=dict(ratios),
            price_residuals={},
            price_match=False,
            adjustment_flagged=False,
            volume_diff_pct=volume_diff_pct,
            volume_match=volume_match,
            reasons=tuple(reasons),
        )

    factor = statistics.median(ratios.values())

    residuals: Dict[str, float] = {}
    price_match = True
    for field in PRICE_FIELDS:
        published_value = float(getattr(published, field))
        stored_value = float(stored[field])
        residual = abs(stored_value - factor * published_value)
        residuals[field] = residual
        if residual > price_abs_tolerance + price_rel_tolerance * published_value:
            price_match = False
    if not price_match:
        reasons.append("price_mismatch")

    adjustment_flagged = abs(factor - 1.0) > adjustment_review_threshold
    if adjustment_flagged:
        # Not a mismatch: the four fields do agree on one scalar, so the bar is
        # internally coherent. It is simply too far from 1.0 to be an ordinary
        # dividend, so the caller should confirm a real corporate action exists.
        reasons.append("adjustment_out_of_band")

    return BarComparison(
        symbol=symbol,
        comparable=True,
        adjustment_factor=factor,
        price_ratios=dict(ratios),
        price_residuals=residuals,
        price_match=price_match,
        adjustment_flagged=adjustment_flagged,
        volume_diff_pct=volume_diff_pct,
        volume_match=volume_match,
        reasons=tuple(reasons),
    )


def symbol_from_code(code: str) -> str:
    """``"IDEA.NS"`` -> ``"IDEA"``. Bhavcopy is keyed by bare NSE symbol."""
    return code[:-3] if code.upper().endswith(".NS") else code


def compare_bars(
    stored_bars: Mapping[str, Mapping[str, Optional[float]]],
    published: Mapping[str, BhavcopyRow],
    **kwargs,
) -> Dict[str, BarComparison]:
    """Compare many stored bars at once, keyed by the caller's own code.

    Keys of ``stored_bars`` may be either ``.NS`` codes or bare NSE symbols.
    A code absent from the bhavcopy is omitted from the result - it is a
    "not published" fact for the caller to record, not a mismatch this module
    can assert.
    """
    results: Dict[str, BarComparison] = {}
    for code, bar in stored_bars.items():
        row = published.get(symbol_from_code(code))
        if row is None:
            continue
        results[code] = compare_bar(bar, row, **kwargs)
    return results


__all__: Sequence[str] = (
    "ADJUSTMENT_REVIEW_THRESHOLD",
    "BHAVCOPY_URL_TEMPLATE",
    "BarComparison",
    "BhavcopyRow",
    "BhavcopyUnavailable",
    "DEFAULT_TIMEOUT_SECONDS",
    "EQ_SERIES",
    "PRICE_ABS_TOLERANCE_INR",
    "PRICE_FIELDS",
    "PRICE_REL_TOLERANCE",
    "VOLUME_TOLERANCE_PCT",
    "bhavcopy_url",
    "compare_bar",
    "compare_bars",
    "fetch_bhavcopy",
    "parse_bhavcopy",
    "symbol_from_code",
)


def prices_match(
    stored_close: Optional[float],
    published_close: Optional[float],
    *,
    stored_bar: Optional[Mapping[str, Optional[float]]] = None,
    published_bar: Optional[BhavcopyRow] = None,
) -> bool:
    """Close-only price check, for callers that compare one field at a time.

    :func:`compare_bar` is the stronger check and should be preferred: it infers
    the corporate-action factor ``k`` from all four price fields and verifies the
    ratios agree with each other, so a dividend of any size is handled without a
    tolerance that has to be widened to cover it.

    This adapter exists because the reconciliation service compares fields
    individually. With only one price it cannot infer ``k``, so it assumes ``k=1``
    and accepts the published price within the same residual tolerance. **That
    means an unadjusted dividend will read as a mismatch here** - which is why the
    service treats price as advisory and lets volume decide.

    Pass ``stored_bar`` and ``published_bar`` to get the full, adjustment-aware
    comparison instead; the close-only path is then bypassed entirely.
    """
    if stored_bar is not None and published_bar is not None:
        return compare_bar(stored_bar, published_bar).price_match

    if stored_close is None or published_close is None:
        return False
    tolerance = PRICE_ABS_TOLERANCE_INR + PRICE_REL_TOLERANCE * abs(
        float(published_close)
    )
    return abs(float(stored_close) - float(published_close)) <= tolerance
