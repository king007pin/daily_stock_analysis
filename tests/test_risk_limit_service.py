# -*- coding: utf-8 -*-
"""
Tests for src/services/risk_limit_service.py - the fail-CLOSED portfolio risk limit gate.

Why this layer exists: the repo already refuses individual orders whose circuit buffer is
too narrow (``BrokerExecutionService.create_bracket_order`` gating on the
``circuit_risk_flag`` computed in ``kronos_service``). That is instrument-level. Nothing
looked at the book as a whole, so twenty individually-sane sub-Rs10 orders could still add
up to a terminal month. These tests pin the portfolio-level contract.

Contract under test:
    RiskLimits(...)                                   # frozen; every field None = disabled
    RiskLimits.from_config(config) -> RiskLimits      # reads risk_limit_* via getattr
    RiskLimitService(limits).evaluate_position(proposed, portfolio) -> RiskVerdict
    RiskVerdict.allowed: bool
    RiskVerdict.breaches: tuple[RiskBreach, ...]      # ALL breaches, not just the first
    RiskBreach.limit / .reason / .observed / .limit_value

Fully offline and deterministic: no network, no DB, no clock reads, no filesystem.
"""

from __future__ import annotations

import pytest

from src.services.risk_limit_service import (
    LIMIT_EVALUATION_ERROR,
    LIMIT_MAX_CORRELATED_POSITIONS,
    LIMIT_MAX_DAILY_LOSS_PCT,
    LIMIT_MAX_DRAWDOWN_PCT,
    LIMIT_MAX_GROSS_EXPOSURE_PCT,
    LIMIT_MAX_POSITIONS,
    LIMIT_MAX_POSITIONS_PER_SECTOR,
    LIMIT_MAX_RISK_PER_TRADE_PCT,
    PortfolioState,
    Position,
    ProposedPosition,
    RiskLimits,
    RiskLimitService,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

def _proposal(**overrides) -> ProposedPosition:
    """A benign proposal: Rs8.50 entry, Rs8.00 stop, 20 shares => Rs10 at risk."""
    base = {
        "stock_code": "IDEA",
        "quantity": 20,
        "entry_price": 8.50,
        "stop_loss_price": 8.00,
        "sector": "TELECOM",
    }
    base.update(overrides)
    return ProposedPosition(**base)


def _portfolio(**overrides) -> PortfolioState:
    """A benign book: Rs5,000 equity, flat on the day, at its equity high-water mark."""
    base = {
        "equity": 5000.0,
        "peak_equity": 5000.0,
        "day_start_equity": 5000.0,
        "positions": (),
        "correlations": {},
    }
    base.update(overrides)
    return PortfolioState(**base)


def _assert_refused(verdict, limit: str, label: str):
    """A verdict must refuse, and must name ``limit`` with a non-empty reason."""
    assert verdict.allowed is False, f"{label}: expected refusal, got allowed (breaches={verdict.breaches!r})"
    assert limit in verdict.breached_limits, (
        f"{label}: expected limit {limit!r} in {verdict.breached_limits!r}"
    )
    reason = verdict.reason_for(limit)
    assert isinstance(reason, str) and reason.strip(), f"{label}: expected a non-empty reason"
    return reason


def _assert_allowed(verdict, label: str):
    assert verdict.allowed is True, f"{label}: expected allowed, got refusal ({verdict.summary()})"
    assert verdict.breaches == (), f"{label}: allowed verdict must carry no breaches"


class _ExplodingProposal:
    """A proposal whose stop-loss lookup raises. Forces the generic exception path.

    Not a malformed number and not a missing field - an attribute access that blows
    up mid-evaluation, which is the case a happy-path-only test suite never reaches.
    """

    stock_code = "BOOM"
    quantity = 10
    entry_price = 9.0
    sector = "TELECOM"

    @property
    def stop_loss_price(self):
        raise RuntimeError("stop-loss lookup exploded")


class _ConfigStub:
    """Stands in for src/config.py - only the risk_limit_* attributes matter here."""

    def __init__(self, **attrs):
        for key, value in attrs.items():
            setattr(self, key, value)


# --------------------------------------------------------------------------
# Rule 3: all-None limits allow everything
# --------------------------------------------------------------------------

class TestAllLimitsDisabled:
    """Introducing the service must not change behaviour until it is configured."""

    def test_fully_none_limits_allow_a_normal_position(self):
        service = RiskLimitService(RiskLimits())
        _assert_allowed(service.evaluate_position(_proposal(), _portfolio()), "all-None limits")

    def test_default_constructed_service_allows(self):
        _assert_allowed(RiskLimitService().evaluate_position(_proposal(), _portfolio()), "no limits passed")

    def test_fully_none_limits_allow_an_egregious_position(self):
        """80% drawdown, 40x leverage, no stop-loss: still allowed while unconfigured."""
        service = RiskLimitService(RiskLimits())
        verdict = service.evaluate_position(
            _proposal(quantity=20000, stop_loss_price=None),
            _portfolio(equity=1000.0, peak_equity=5000.0, day_start_equity=4000.0),
        )
        _assert_allowed(verdict, "all-None limits, egregious position")

    def test_is_fully_disabled_flag(self):
        assert RiskLimits().is_fully_disabled() is True
        assert RiskLimits(max_positions=3).is_fully_disabled() is False


# --------------------------------------------------------------------------
# Each limit breaches independently
# --------------------------------------------------------------------------

class TestEachLimitBreachesIndependently:
    """Exactly one limit configured per test, so exactly one breach can be produced."""

    def test_max_drawdown_pct(self):
        service = RiskLimitService(RiskLimits(max_drawdown_pct=10.0))
        # Peak 5000 -> 4400 is a 12% drawdown, past the 10% kill switch.
        verdict = service.evaluate_position(_proposal(), _portfolio(equity=4400.0, peak_equity=5000.0))
        reason = _assert_refused(verdict, LIMIT_MAX_DRAWDOWN_PCT, "drawdown")
        assert "drawdown" in reason.lower()
        assert verdict.breached_limits == [LIMIT_MAX_DRAWDOWN_PCT]
        assert verdict.breaches[0].observed == pytest.approx(12.0)
        assert verdict.breaches[0].limit_value == pytest.approx(10.0)

    def test_max_drawdown_pct_allows_a_shallower_drawdown(self):
        service = RiskLimitService(RiskLimits(max_drawdown_pct=10.0))
        verdict = service.evaluate_position(_proposal(), _portfolio(equity=4600.0, peak_equity=5000.0))
        _assert_allowed(verdict, "8% drawdown under a 10% limit")

    def test_max_drawdown_pct_is_a_kill_switch_at_exactly_the_limit(self):
        """Kill switches trip at ``>=``: the drawdown you promised to stop at IS the stop."""
        service = RiskLimitService(RiskLimits(max_drawdown_pct=10.0))
        verdict = service.evaluate_position(_proposal(), _portfolio(equity=4500.0, peak_equity=5000.0))
        _assert_refused(verdict, LIMIT_MAX_DRAWDOWN_PCT, "drawdown exactly at the limit")

    def test_max_risk_per_trade_pct(self):
        service = RiskLimitService(RiskLimits(max_risk_per_trade_pct=1.0))
        # entry 8.50, stop 8.00, qty 200 => Rs100 at risk on Rs5,000 equity = 2%.
        verdict = service.evaluate_position(_proposal(quantity=200), _portfolio())
        reason = _assert_refused(verdict, LIMIT_MAX_RISK_PER_TRADE_PCT, "risk per trade")
        assert "risk per trade" in reason.lower()
        assert verdict.breaches[0].observed == pytest.approx(2.0)

    def test_max_risk_per_trade_pct_scales_with_equity_not_cash(self):
        """The same Rs100 risk is a breach at Rs5,000 equity and fine at Rs20,000."""
        service = RiskLimitService(RiskLimits(max_risk_per_trade_pct=1.0))
        proposal = _proposal(quantity=200)
        _assert_refused(
            service.evaluate_position(proposal, _portfolio(equity=5000.0, peak_equity=5000.0)),
            LIMIT_MAX_RISK_PER_TRADE_PCT,
            "Rs100 risk on Rs5,000",
        )
        _assert_allowed(
            service.evaluate_position(proposal, _portfolio(equity=20000.0, peak_equity=20000.0)),
            "Rs100 risk on Rs20,000",
        )

    def test_max_daily_loss_pct(self):
        service = RiskLimitService(RiskLimits(max_daily_loss_pct=3.0))
        # Session started at 5000, now 4750 => down 5%.
        verdict = service.evaluate_position(_proposal(), _portfolio(equity=4750.0, day_start_equity=5000.0))
        reason = _assert_refused(verdict, LIMIT_MAX_DAILY_LOSS_PCT, "daily loss")
        assert "daily loss" in reason.lower()
        assert verdict.breaches[0].observed == pytest.approx(5.0)

    def test_max_daily_loss_pct_allows_a_smaller_loss(self):
        service = RiskLimitService(RiskLimits(max_daily_loss_pct=3.0))
        verdict = service.evaluate_position(_proposal(), _portfolio(equity=4900.0, day_start_equity=5000.0))
        _assert_allowed(verdict, "2% daily loss under a 3% limit")

    def test_max_gross_exposure_pct(self):
        service = RiskLimitService(RiskLimits(max_gross_exposure_pct=200.0))
        held = (Position(stock_code="YESBANK", quantity=200, entry_price=8.0),)
        # 1,600 held + 9,000 proposed = 10,600 notional on 5,000 equity = 212%.
        verdict = service.evaluate_position(
            _proposal(stock_code="IDEA", quantity=1000, entry_price=9.0),
            _portfolio(positions=held),
        )
        reason = _assert_refused(verdict, LIMIT_MAX_GROSS_EXPOSURE_PCT, "gross exposure")
        assert "gross exposure" in reason.lower()
        assert verdict.breaches[0].observed == pytest.approx(212.0)

    def test_max_gross_exposure_pct_allows_two_x_mis_leverage(self):
        """A 200% ceiling is 2x MIS: exposure at exactly 2x equity must pass."""
        service = RiskLimitService(RiskLimits(max_gross_exposure_pct=200.0))
        held = (Position(stock_code="YESBANK", quantity=200, entry_price=8.0),)
        verdict = service.evaluate_position(
            _proposal(quantity=1000, entry_price=8.4),  # 8,400 + 1,600 = 10,000 = exactly 200%
            _portfolio(positions=held),
        )
        _assert_allowed(verdict, "exposure at exactly 2x equity")

    def test_max_gross_exposure_pct_marks_to_market_when_given_a_live_price(self):
        service = RiskLimitService(RiskLimits(max_gross_exposure_pct=100.0))
        held = (Position(stock_code="YESBANK", quantity=200, entry_price=8.0, current_price=20.0),)
        # Marked: 200 * 20.00 = 4,000, plus 130 * 8.50 = 1,105 proposed => 5,105 on 5,000
        # equity = 102.1%, a breach. At cost it would be 1,600 + 1,105 = 54.1% and pass, so
        # this case only fails when the live price is actually used.
        verdict = service.evaluate_position(_proposal(quantity=130), _portfolio(positions=held))
        _assert_refused(verdict, LIMIT_MAX_GROSS_EXPOSURE_PCT, "marked-to-market exposure")

    def test_max_positions(self):
        service = RiskLimitService(RiskLimits(max_positions=3))
        held = tuple(
            Position(stock_code=code, quantity=10, entry_price=9.0)
            for code in ("YESBANK", "SUZLON", "PNB")
        )
        verdict = service.evaluate_position(_proposal(stock_code="IDEA"), _portfolio(positions=held))
        reason = _assert_refused(verdict, LIMIT_MAX_POSITIONS, "position count")
        assert "position count" in reason.lower()
        assert verdict.breaches[0].observed == pytest.approx(4.0)

    def test_max_positions_allows_adding_to_a_name_already_held(self):
        """Topping up an existing name consumes no new slot."""
        service = RiskLimitService(RiskLimits(max_positions=3))
        held = tuple(
            Position(stock_code=code, quantity=10, entry_price=9.0)
            for code in ("IDEA", "SUZLON", "PNB")
        )
        _assert_allowed(
            service.evaluate_position(_proposal(stock_code="IDEA"), _portfolio(positions=held)),
            "top-up of a held name at the position cap",
        )

    def test_max_positions_per_sector(self):
        service = RiskLimitService(RiskLimits(max_positions_per_sector=2))
        held = (
            Position(stock_code="YESBANK", quantity=10, entry_price=9.0, sector="BANKING"),
            Position(stock_code="VODAFONE", quantity=10, entry_price=9.0, sector="TELECOM"),
            Position(stock_code="MTNL", quantity=10, entry_price=9.0, sector="TELECOM"),
        )
        verdict = service.evaluate_position(
            _proposal(stock_code="IDEA", sector="TELECOM"),
            _portfolio(positions=held),
        )
        reason = _assert_refused(verdict, LIMIT_MAX_POSITIONS_PER_SECTOR, "sector count")
        assert "sector" in reason.lower() and "TELECOM" in reason
        assert verdict.breaches[0].observed == pytest.approx(3.0)

    def test_max_positions_per_sector_ignores_other_sectors(self):
        service = RiskLimitService(RiskLimits(max_positions_per_sector=2))
        held = tuple(
            Position(stock_code=code, quantity=10, entry_price=9.0, sector="BANKING")
            for code in ("YESBANK", "PNB", "IOB")
        )
        _assert_allowed(
            service.evaluate_position(_proposal(sector="TELECOM"), _portfolio(positions=held)),
            "three banks do not fill the telecom bucket",
        )

    def test_max_correlated_positions(self):
        service = RiskLimitService(
            RiskLimits(max_correlated_positions=2, correlation_threshold=0.8)
        )
        held = (
            Position(stock_code="YESBANK", quantity=10, entry_price=9.0),
            Position(stock_code="SUZLON", quantity=10, entry_price=9.0),
        )
        correlations = {("IDEA", "YESBANK"): 0.91, ("IDEA", "SUZLON"): 0.85}
        verdict = service.evaluate_position(
            _proposal(stock_code="IDEA"),
            _portfolio(positions=held, correlations=correlations),
        )
        reason = _assert_refused(verdict, LIMIT_MAX_CORRELATED_POSITIONS, "correlated cluster")
        assert "correlated" in reason.lower()
        assert verdict.breaches[0].observed == pytest.approx(3.0)


# --------------------------------------------------------------------------
# Correlation semantics
# --------------------------------------------------------------------------

class TestCorrelationClustering:
    """N names correlated above the threshold are ONE concentrated bet, not N bets."""

    LIMITS = RiskLimits(max_correlated_positions=3, correlation_threshold=0.75)

    def test_cluster_of_correlated_names_counts_as_one_concentrated_bet(self):
        """Three correlated holdings + the correlated proposal = a cluster of 4 > 3."""
        service = RiskLimitService(self.LIMITS)
        held = tuple(
            Position(stock_code=code, quantity=10, entry_price=9.0)
            for code in ("YESBANK", "PNB", "IOB")
        )
        correlations = {
            ("IDEA", "YESBANK"): 0.88,
            ("IDEA", "PNB"): 0.81,
            ("IDEA", "IOB"): 0.79,
        }
        verdict = service.evaluate_position(
            _proposal(stock_code="IDEA"),
            _portfolio(positions=held, correlations=correlations),
        )
        reason = _assert_refused(verdict, LIMIT_MAX_CORRELATED_POSITIONS, "cluster of four")
        # The reason must name the whole cluster, so the operator sees the single bet.
        for code in ("IDEA", "YESBANK", "PNB", "IOB"):
            assert code in reason, f"expected {code} named in the cluster reason: {reason}"

    def test_uncorrelated_positions_do_not_enlarge_the_cluster(self):
        """Twenty independent names sit outside the cluster; only the correlated ones count."""
        service = RiskLimitService(self.LIMITS)
        held = tuple(
            Position(stock_code=f"NAME{i}", quantity=10, entry_price=9.0)
            for i in range(20)
        )
        correlations = {("IDEA", f"NAME{i}"): 0.10 for i in range(20)}
        _assert_allowed(
            service.evaluate_position(
                _proposal(stock_code="IDEA"),
                _portfolio(positions=held, correlations=correlations),
            ),
            "twenty uncorrelated names",
        )

    def test_cluster_exactly_at_the_limit_is_allowed(self):
        service = RiskLimitService(self.LIMITS)
        held = (
            Position(stock_code="YESBANK", quantity=10, entry_price=9.0),
            Position(stock_code="PNB", quantity=10, entry_price=9.0),
        )
        correlations = {("IDEA", "YESBANK"): 0.90, ("IDEA", "PNB"): 0.90}
        _assert_allowed(
            service.evaluate_position(
                _proposal(stock_code="IDEA"),
                _portfolio(positions=held, correlations=correlations),
            ),
            "cluster of exactly three under a limit of three",
        )

    def test_correlation_just_below_the_threshold_is_excluded(self):
        service = RiskLimitService(self.LIMITS)
        held = tuple(
            Position(stock_code=code, quantity=10, entry_price=9.0)
            for code in ("YESBANK", "PNB", "IOB")
        )
        correlations = {("IDEA", "YESBANK"): 0.90, ("IDEA", "PNB"): 0.90, ("IDEA", "IOB"): 0.74}
        _assert_allowed(
            service.evaluate_position(
                _proposal(stock_code="IDEA"),
                _portfolio(positions=held, correlations=correlations),
            ),
            "third name at 0.74 under a 0.75 threshold",
        )

    def test_negative_correlation_counts_by_magnitude(self):
        """-0.9 is as concentrating as +0.9 once both legs are held."""
        service = RiskLimitService(RiskLimits(max_correlated_positions=1, correlation_threshold=0.8))
        held = (Position(stock_code="YESBANK", quantity=10, entry_price=9.0),)
        verdict = service.evaluate_position(
            _proposal(stock_code="IDEA"),
            _portfolio(positions=held, correlations={("YESBANK", "IDEA"): -0.93}),
        )
        _assert_refused(verdict, LIMIT_MAX_CORRELATED_POSITIONS, "negative correlation")

    def test_correlation_lookup_is_order_insensitive(self):
        service = RiskLimitService(RiskLimits(max_correlated_positions=1, correlation_threshold=0.8))
        held = (Position(stock_code="YESBANK", quantity=10, entry_price=9.0),)
        verdict = service.evaluate_position(
            _proposal(stock_code="IDEA"),
            _portfolio(positions=held, correlations={("YESBANK", "IDEA"): 0.93}),
        )
        _assert_refused(verdict, LIMIT_MAX_CORRELATED_POSITIONS, "reversed key order")


# --------------------------------------------------------------------------
# Missing stop-loss
# --------------------------------------------------------------------------

class TestMissingStopLoss:
    """Unbounded risk cannot be sized, so it cannot be admitted."""

    def test_missing_stop_loss_is_a_breach(self):
        service = RiskLimitService(RiskLimits(max_risk_per_trade_pct=2.0))
        verdict = service.evaluate_position(_proposal(stop_loss_price=None), _portfolio())
        reason = _assert_refused(verdict, LIMIT_MAX_RISK_PER_TRADE_PCT, "missing stop-loss")
        assert "stop-loss" in reason.lower()
        assert "unbounded" in reason.lower()

    def test_missing_stop_loss_on_a_tiny_position_is_still_a_breach(self):
        """Size does not rescue it: without a stop the loss has no floor."""
        service = RiskLimitService(RiskLimits(max_risk_per_trade_pct=2.0))
        verdict = service.evaluate_position(
            _proposal(quantity=1, stop_loss_price=None), _portfolio()
        )
        _assert_refused(verdict, LIMIT_MAX_RISK_PER_TRADE_PCT, "one-share stopless position")

    def test_unparseable_stop_loss_fails_closed(self):
        service = RiskLimitService(RiskLimits(max_risk_per_trade_pct=2.0))
        verdict = service.evaluate_position(_proposal(stop_loss_price="not-a-price"), _portfolio())
        _assert_refused(verdict, LIMIT_EVALUATION_ERROR, "unparseable stop-loss")


# --------------------------------------------------------------------------
# Multiple simultaneous breaches
# --------------------------------------------------------------------------

class TestMultipleBreachesAreAllReported:
    """Reporting one breach at a time makes the operator fix a bad book one step at a time."""

    def test_all_seven_limits_breach_at_once(self):
        limits = RiskLimits(
            max_drawdown_pct=10.0,
            max_risk_per_trade_pct=1.0,
            max_daily_loss_pct=3.0,
            max_gross_exposure_pct=100.0,
            max_positions=2,
            max_positions_per_sector=1,
            max_correlated_positions=1,
            correlation_threshold=0.75,
        )
        held = (
            Position(stock_code="YESBANK", quantity=300, entry_price=8.0, sector="TELECOM"),
            Position(stock_code="PNB", quantity=300, entry_price=8.0, sector="TELECOM"),
        )
        portfolio = PortfolioState(
            equity=4000.0,          # peak 5,000 => 20% drawdown; day start 5,000 => 20% daily loss
            peak_equity=5000.0,
            day_start_equity=5000.0,
            positions=held,
            correlations={("IDEA", "YESBANK"): 0.95, ("IDEA", "PNB"): 0.92},
        )
        verdict = RiskLimitService(limits).evaluate_position(
            _proposal(stock_code="IDEA", quantity=500, entry_price=9.0, stop_loss_price=8.0),
            portfolio,
        )

        assert verdict.allowed is False
        assert set(verdict.breached_limits) == {
            LIMIT_MAX_DRAWDOWN_PCT,
            LIMIT_MAX_RISK_PER_TRADE_PCT,
            LIMIT_MAX_DAILY_LOSS_PCT,
            LIMIT_MAX_GROSS_EXPOSURE_PCT,
            LIMIT_MAX_POSITIONS,
            LIMIT_MAX_POSITIONS_PER_SECTOR,
            LIMIT_MAX_CORRELATED_POSITIONS,
        }, verdict.summary()
        assert len(verdict.breaches) == 7
        # Every breach must carry its own readable reason, not a shared placeholder.
        assert len({b.reason for b in verdict.breaches}) == 7

    def test_two_breaches_report_both(self):
        limits = RiskLimits(max_positions=1, max_risk_per_trade_pct=0.1)
        held = (Position(stock_code="YESBANK", quantity=10, entry_price=8.0),)
        verdict = RiskLimitService(limits).evaluate_position(
            _proposal(quantity=200), _portfolio(positions=held)
        )
        assert set(verdict.breached_limits) == {LIMIT_MAX_POSITIONS, LIMIT_MAX_RISK_PER_TRADE_PCT}

    def test_to_dict_carries_every_breach(self):
        limits = RiskLimits(max_positions=1, max_risk_per_trade_pct=0.1)
        held = (Position(stock_code="YESBANK", quantity=10, entry_price=8.0),)
        payload = RiskLimitService(limits).evaluate_position(
            _proposal(quantity=200), _portfolio(positions=held)
        ).to_dict()
        assert payload["allowed"] is False
        assert len(payload["breaches"]) == 2
        assert payload["summary"].startswith("refused: ")


# --------------------------------------------------------------------------
# Fail-closed
# --------------------------------------------------------------------------

class TestFailClosed:
    """Every error path must land on allowed=False. There is no fail-open branch."""

    def test_exception_inside_evaluation_yields_allowed_false(self):
        """Force the generic ``except Exception`` handler with an attribute that raises.

        ``_ExplodingProposal.stop_loss_price`` is a property that raises RuntimeError, so
        the failure happens INSIDE the risk-per-trade check rather than at input parsing.
        The verdict must still be a refusal naming ``evaluation_error``, and the reason
        must carry the exception type and message so the operator can debug it.
        """
        service = RiskLimitService(RiskLimits(max_risk_per_trade_pct=2.0))

        # Sanity: the injected input really does raise, so this test cannot silently
        # degrade into another happy path if the property is ever changed.
        with pytest.raises(RuntimeError):
            _ExplodingProposal().stop_loss_price

        verdict = service.evaluate_position(_ExplodingProposal(), _portfolio())
        reason = _assert_refused(verdict, LIMIT_EVALUATION_ERROR, "exploding proposal")
        assert "RuntimeError" in reason
        assert "stop-loss lookup exploded" in reason

    def test_exploding_portfolio_attribute_yields_allowed_false(self):
        """Same posture when the failure is on the portfolio side."""
        class _ExplodingPortfolio:
            peak_equity = 5000.0
            day_start_equity = 5000.0
            correlations = {}

            @property
            def equity(self):
                raise ZeroDivisionError("equity computation exploded")

        with pytest.raises(ZeroDivisionError):
            _ExplodingPortfolio().equity

        verdict = RiskLimitService(RiskLimits(max_positions=5)).evaluate_position(
            _proposal(), _ExplodingPortfolio()
        )
        reason = _assert_refused(verdict, LIMIT_EVALUATION_ERROR, "exploding portfolio")
        assert "ZeroDivisionError" in reason

    def test_evaluation_never_raises_out_of_the_service(self):
        """The gate absorbs the exception itself; a caller without try/except must not trade."""
        verdict = RiskLimitService(RiskLimits(max_risk_per_trade_pct=2.0)).evaluate_position(
            _ExplodingProposal(), _portfolio()
        )
        assert verdict.allowed is False

    @pytest.mark.parametrize(
        "proposal_kwargs, label",
        [
            ({"entry_price": None}, "missing entry price"),
            ({"entry_price": "eight-fifty"}, "unparseable entry price"),
            ({"entry_price": 0.0}, "zero entry price"),
            ({"entry_price": float("nan")}, "NaN entry price"),
            ({"quantity": None}, "missing quantity"),
            ({"quantity": "many"}, "unparseable quantity"),
            ({"quantity": 0}, "zero quantity"),
            ({"quantity": -10}, "negative quantity"),
        ],
    )
    def test_bad_proposal_input_fails_closed_even_with_no_limits(self, proposal_kwargs, label):
        """All-None limits are not a licence to act on numbers we could not read."""
        verdict = RiskLimitService(RiskLimits()).evaluate_position(
            _proposal(**proposal_kwargs), _portfolio()
        )
        _assert_refused(verdict, LIMIT_EVALUATION_ERROR, label)

    @pytest.mark.parametrize(
        "portfolio_kwargs, label",
        [
            ({"equity": None}, "missing equity"),
            ({"equity": "five thousand"}, "unparseable equity"),
            ({"equity": 0.0}, "zero equity"),
            ({"equity": -500.0}, "negative equity"),
        ],
    )
    def test_bad_portfolio_input_fails_closed(self, portfolio_kwargs, label):
        verdict = RiskLimitService(RiskLimits()).evaluate_position(
            _proposal(), _portfolio(**portfolio_kwargs)
        )
        _assert_refused(verdict, LIMIT_EVALUATION_ERROR, label)

    def test_none_proposal_fails_closed(self):
        _assert_refused(
            RiskLimitService(RiskLimits()).evaluate_position(None, _portfolio()),
            LIMIT_EVALUATION_ERROR,
            "None proposal",
        )

    def test_none_portfolio_fails_closed(self):
        _assert_refused(
            RiskLimitService(RiskLimits()).evaluate_position(_proposal(), None),
            LIMIT_EVALUATION_ERROR,
            "None portfolio",
        )

    def test_drawdown_limit_without_peak_equity_fails_closed(self):
        verdict = RiskLimitService(RiskLimits(max_drawdown_pct=10.0)).evaluate_position(
            _proposal(), _portfolio(peak_equity=None)
        )
        reason = _assert_refused(verdict, LIMIT_EVALUATION_ERROR, "no peak equity")
        assert "peak_equity" in reason

    def test_daily_loss_limit_without_day_start_equity_fails_closed(self):
        verdict = RiskLimitService(RiskLimits(max_daily_loss_pct=3.0)).evaluate_position(
            _proposal(), _portfolio(day_start_equity=None)
        )
        reason = _assert_refused(verdict, LIMIT_EVALUATION_ERROR, "no day start equity")
        assert "day_start_equity" in reason

    def test_sector_limit_without_a_sector_fails_closed(self):
        verdict = RiskLimitService(RiskLimits(max_positions_per_sector=2)).evaluate_position(
            _proposal(sector=None), _portfolio()
        )
        reason = _assert_refused(verdict, LIMIT_EVALUATION_ERROR, "no sector")
        assert "sector" in reason

    def test_correlation_limit_without_a_threshold_fails_closed(self):
        verdict = RiskLimitService(RiskLimits(max_correlated_positions=2)).evaluate_position(
            _proposal(), _portfolio()
        )
        reason = _assert_refused(verdict, LIMIT_EVALUATION_ERROR, "no correlation threshold")
        assert "correlation_threshold" in reason

    def test_correlation_limit_without_correlation_data_fails_closed(self):
        held = (Position(stock_code="YESBANK", quantity=10, entry_price=9.0),)
        verdict = RiskLimitService(
            RiskLimits(max_correlated_positions=2, correlation_threshold=0.8)
        ).evaluate_position(_proposal(), _portfolio(positions=held, correlations=None))
        reason = _assert_refused(verdict, LIMIT_EVALUATION_ERROR, "no correlation data")
        assert "correlations" in reason

    def test_unreadable_position_price_fails_closed(self):
        held = (Position(stock_code="YESBANK", quantity=10, entry_price=None),)
        verdict = RiskLimitService(RiskLimits(max_gross_exposure_pct=200.0)).evaluate_position(
            _proposal(), _portfolio(positions=held)
        )
        _assert_refused(verdict, LIMIT_EVALUATION_ERROR, "unreadable held price")

    def test_garbage_config_limit_fails_closed_rather_than_vanishing(self):
        """A malformed config value must refuse trades, not silently disable the limit."""
        limits = RiskLimits.from_config(_ConfigStub(risk_limit_max_drawdown_pct="ten percent"))
        assert limits.max_drawdown_pct == "ten percent"  # preserved, not dropped to None
        verdict = RiskLimitService(limits).evaluate_position(_proposal(), _portfolio())
        _assert_refused(verdict, LIMIT_EVALUATION_ERROR, "garbage drawdown limit")


# --------------------------------------------------------------------------
# from_config
# --------------------------------------------------------------------------

class TestFromConfig:
    """Reads ``risk_limit_*`` attributes with None defaults; never raises."""

    def test_absent_attributes_yield_all_none(self):
        limits = RiskLimits.from_config(_ConfigStub())
        assert limits.is_fully_disabled() is True
        assert limits.correlation_threshold is None

    def test_reads_every_limit(self):
        limits = RiskLimits.from_config(_ConfigStub(
            risk_limit_max_drawdown_pct=15.0,
            risk_limit_max_risk_per_trade_pct=1.5,
            risk_limit_max_daily_loss_pct=4.0,
            risk_limit_max_gross_exposure_pct=200.0,
            risk_limit_max_positions=5,
            risk_limit_max_positions_per_sector=2,
            risk_limit_max_correlated_positions=3,
            risk_limit_correlation_threshold=0.75,
        ))
        assert limits.max_drawdown_pct == pytest.approx(15.0)
        assert limits.max_risk_per_trade_pct == pytest.approx(1.5)
        assert limits.max_daily_loss_pct == pytest.approx(4.0)
        assert limits.max_gross_exposure_pct == pytest.approx(200.0)
        assert limits.max_positions == 5
        assert limits.max_positions_per_sector == 2
        assert limits.max_correlated_positions == 3
        assert limits.correlation_threshold == pytest.approx(0.75)

    def test_string_env_style_values_are_coerced(self):
        limits = RiskLimits.from_config(_ConfigStub(
            risk_limit_max_drawdown_pct="12.5",
            risk_limit_max_positions="4",
        ))
        assert limits.max_drawdown_pct == pytest.approx(12.5)
        assert limits.max_positions == 4

    def test_partial_config_leaves_other_limits_disabled(self):
        limits = RiskLimits.from_config(_ConfigStub(risk_limit_max_positions=3))
        assert limits.max_positions == 3
        assert limits.max_drawdown_pct is None
        assert limits.max_gross_exposure_pct is None

    def test_limits_are_frozen(self):
        limits = RiskLimits.from_config(_ConfigStub(risk_limit_max_positions=3))
        with pytest.raises(Exception):
            limits.max_positions = 99  # type: ignore[misc]


# --------------------------------------------------------------------------
# Worked scenario: this system's real profile
# --------------------------------------------------------------------------

class TestSmallAccountIntradayScenario:
    """Rs5,000 equity, 2x MIS intraday leverage, sub-Rs10 NSE names.

    This is the profile the repo actually trades (the same sub-Rs10 universe the
    circuit-buffer guard in broker_service exists for). At this account size a single
    careless quantity is a double-digit percentage of the book, which is exactly what
    max_risk_per_trade_pct is for.
    """

    LIMITS = RiskLimits(
        max_drawdown_pct=15.0,
        max_risk_per_trade_pct=1.0,      # Rs50 of risk on Rs5,000
        max_daily_loss_pct=3.0,          # Rs150 in a session
        max_gross_exposure_pct=200.0,    # 2x MIS
        max_positions=3,
        max_positions_per_sector=2,
        max_correlated_positions=2,
        correlation_threshold=0.80,
    )

    FRESH_BOOK = dict(equity=5000.0, peak_equity=5000.0, day_start_equity=5000.0)

    def test_oversized_sub_rs10_position_is_refused_on_risk_per_trade(self):
        """Rs8.50 entry, Rs8.00 stop, 200 shares = Rs100 at risk = 2% of a Rs5,000 book.

        Notional is only Rs1,700 (34% of equity, well inside 2x MIS) and every other
        limit is clean, so the refusal can only come from max_risk_per_trade_pct.
        """
        verdict = RiskLimitService(self.LIMITS).evaluate_position(
            ProposedPosition(
                stock_code="IDEA",
                quantity=200,
                entry_price=8.50,
                stop_loss_price=8.00,
                sector="TELECOM",
            ),
            PortfolioState(**self.FRESH_BOOK),
        )
        reason = _assert_refused(verdict, LIMIT_MAX_RISK_PER_TRADE_PCT, "Rs100 risk on Rs5,000")
        assert verdict.breached_limits == [LIMIT_MAX_RISK_PER_TRADE_PCT], verdict.summary()
        assert verdict.breaches[0].observed == pytest.approx(2.0)
        assert verdict.breaches[0].limit_value == pytest.approx(1.0)
        assert "2.00%" in reason and "1.00%" in reason

    def test_correctly_sized_version_of_the_same_trade_is_allowed(self):
        """Same name and stop, 100 shares: Rs50 at risk = exactly the 1% budget."""
        verdict = RiskLimitService(self.LIMITS).evaluate_position(
            ProposedPosition(
                stock_code="IDEA",
                quantity=100,
                entry_price=8.50,
                stop_loss_price=8.00,
                sector="TELECOM",
            ),
            PortfolioState(**self.FRESH_BOOK),
        )
        _assert_allowed(verdict, "Rs50 risk on Rs5,000 at a 1% budget")

    def test_tight_stop_permits_a_larger_sub_rs10_position(self):
        """A Rs0.15 stop on a Rs9.00 name funds 300 shares (Rs45 risk) inside 2x MIS."""
        verdict = RiskLimitService(self.LIMITS).evaluate_position(
            ProposedPosition(
                stock_code="SUZLON",
                quantity=300,
                entry_price=9.00,
                stop_loss_price=8.85,
                sector="POWER",
            ),
            PortfolioState(**self.FRESH_BOOK),
        )
        _assert_allowed(verdict, "Rs45 risk, Rs2,700 notional")

    def test_second_correlated_penny_name_after_a_losing_morning_is_refused_on_all_counts(self):
        """The realistic bad-day case: down 4%, already two correlated sub-Rs10 telecoms."""
        held = (
            Position(stock_code="VODAFONE", quantity=200, entry_price=9.0, sector="TELECOM"),
            Position(stock_code="MTNL", quantity=200, entry_price=9.0, sector="TELECOM"),
        )
        portfolio = PortfolioState(
            equity=4800.0,
            peak_equity=5000.0,
            day_start_equity=5000.0,   # down 4% today, past the 3% daily kill switch
            positions=held,
            correlations={("IDEA", "VODAFONE"): 0.93, ("IDEA", "MTNL"): 0.88},
        )
        verdict = RiskLimitService(self.LIMITS).evaluate_position(
            ProposedPosition(
                stock_code="IDEA",
                quantity=300,
                entry_price=8.50,
                stop_loss_price=8.00,  # Rs150 risk = 3.1% of a Rs4,800 book
                sector="TELECOM",
            ),
            portfolio,
        )
        assert set(verdict.breached_limits) == {
            LIMIT_MAX_DAILY_LOSS_PCT,
            LIMIT_MAX_RISK_PER_TRADE_PCT,
            LIMIT_MAX_POSITIONS_PER_SECTOR,
            LIMIT_MAX_CORRELATED_POSITIONS,
        }, verdict.summary()

    def test_unconfigured_service_would_have_waved_the_same_trade_through(self):
        """Proof the limits, not the plumbing, are what stop the trade."""
        proposal = ProposedPosition(
            stock_code="IDEA", quantity=200, entry_price=8.50, stop_loss_price=8.00, sector="TELECOM",
        )
        portfolio = PortfolioState(**self.FRESH_BOOK)
        assert RiskLimitService(self.LIMITS).evaluate_position(proposal, portfolio).allowed is False
        assert RiskLimitService(RiskLimits()).evaluate_position(proposal, portfolio).allowed is True
