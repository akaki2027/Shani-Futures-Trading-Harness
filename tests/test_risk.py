"""Risk gate and registry tests.

These protect the safety properties the README claims. Two in particular are
load-bearing:

- Live brokers are *unregistered*, not merely guarded — asking for one raises.
- The daily loss limit uses the trading day (18:00 ET), not the calendar day,
  so an overnight session cannot silently grant a fresh loss allowance.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from shani.audit import AuditLog, EventType
from shani.brokers.base import AccountSnapshot
from shani.brokers.paper import PaperBroker
from shani.brokers.registry import BrokerRegistry, build_registry
from shani.config import LIVE_CONFIRMATION_PHRASE, Config, LiveTradingDisabledError
from shani.db import Database
from shani.models import Order, OrderType, Side, Trade
from shani.risk.policy import RiskPolicy
from shani.sessions import EASTERN

OPEN = datetime(2026, 3, 10, 11, 0, tzinfo=EASTERN)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "risk.db")
    yield database
    database.close()


@pytest.fixture
def audit(db: Database) -> AuditLog:
    return AuditLog(db)


@pytest.fixture
def policy(db: Database, audit: AuditLog) -> RiskPolicy:
    config = Config()
    return RiskPolicy(config=config.risk, db=db, audit=audit)


def account(open_positions: int = 0, balance: str = "100000") -> AccountSnapshot:
    return AccountSnapshot(
        balance=Decimal(balance), realized_pnl=Decimal(0), unrealized_pnl=Decimal(0),
        commission_paid=Decimal(0), open_positions=open_positions, as_of=OPEN,
    )


def entry(symbol: str = "ES", quantity: int = 1, side: Side = Side.BUY) -> Order:
    return Order(symbol=symbol, side=side, quantity=quantity, order_type=OrderType.MARKET)


class TestKillSwitch:
    def test_rejects_everything_when_engaged(self, policy: RiskPolicy) -> None:
        policy.config.kill_switch = True
        decision = policy.evaluate(entry(), account(), at=OPEN, has_stop=True)
        assert not decision.approved
        assert decision.rule == "kill_switch"

    def test_takes_precedence_over_every_other_setting(self, policy: RiskPolicy) -> None:
        policy.config.kill_switch = True
        policy.config.require_stop_loss = False
        policy.config.max_position_contracts = 999
        assert not policy.evaluate(entry(), account(), at=OPEN).approved


class TestDailyLossLimit:
    def _losing_trade(self, db: Database, exit_at: datetime, pnl: str) -> None:
        db.trades.save(Trade(
            symbol="ES", side=Side.BUY, quantity=1,
            entry_price=Decimal("5000"), exit_price=Decimal("4990"),
            entry_at=exit_at - timedelta(minutes=30), exit_at=exit_at,
            gross_pnl=Decimal(pnl), commission=Decimal(0),
        ))

    def test_allows_trading_below_the_limit(self, policy: RiskPolicy, db: Database) -> None:
        self._losing_trade(db, OPEN, "-500")
        assert policy.evaluate(entry(), account(), at=OPEN, has_stop=True).approved

    def test_halts_at_the_limit(self, policy: RiskPolicy, db: Database) -> None:
        self._losing_trade(db, OPEN, "-1000")
        decision = policy.evaluate(entry(), account(), at=OPEN, has_stop=True)
        assert not decision.approved
        assert decision.rule == "max_daily_loss"

    def test_uses_the_trading_day_not_the_calendar_day(
        self, policy: RiskPolicy, db: Database
    ) -> None:
        """A Sunday-evening loss belongs to Monday's session.

        With a calendar-day limit, midnight would reset the allowance in the
        middle of the Asian session.
        """
        sunday_evening = datetime(2026, 3, 15, 20, 0, tzinfo=EASTERN)
        monday_morning = datetime(2026, 3, 16, 10, 0, tzinfo=EASTERN)
        self._losing_trade(db, sunday_evening, "-1000")
        decision = policy.evaluate(entry(), account(), at=monday_morning, has_stop=True)
        assert not decision.approved, "Sunday evening loss must count against Monday"

    def test_previous_session_losses_do_not_carry_over(
        self, policy: RiskPolicy, db: Database
    ) -> None:
        self._losing_trade(db, datetime(2026, 3, 9, 11, 0, tzinfo=EASTERN), "-1000")
        assert policy.evaluate(entry(), account(), at=OPEN, has_stop=True).approved

    def test_remaining_allowance_shrinks_with_losses(
        self, policy: RiskPolicy, db: Database
    ) -> None:
        assert policy.remaining_daily_loss(OPEN) == Decimal("1000")
        self._losing_trade(db, OPEN, "-400")
        assert policy.remaining_daily_loss(OPEN) == Decimal("600")

    def test_remaining_allowance_never_goes_negative(
        self, policy: RiskPolicy, db: Database
    ) -> None:
        self._losing_trade(db, OPEN, "-5000")
        assert policy.remaining_daily_loss(OPEN) == Decimal("0")

    def test_profits_do_not_inflate_the_allowance(
        self, policy: RiskPolicy, db: Database
    ) -> None:
        """A green session must not license a larger loss than configured."""
        self._losing_trade(db, OPEN, "5000")
        assert policy.remaining_daily_loss(OPEN) == Decimal("1000")


class TestPositionLimits:
    def test_rejects_an_order_over_the_size_limit(self, policy: RiskPolicy) -> None:
        decision = policy.evaluate(entry(quantity=10), account(), at=OPEN, has_stop=True)
        assert not decision.approved
        assert decision.rule == "max_position_contracts"

    def test_checks_the_resulting_position_not_the_order(
        self, policy: RiskPolicy, db: Database
    ) -> None:
        """Otherwise five one-lots build the position the limit forbids."""
        broker = PaperBroker(db, slippage_ticks=0)
        broker.on_price("ES", Decimal("5000"), OPEN)
        broker.submit(entry(quantity=5), at=OPEN)
        decision = policy.evaluate(entry(quantity=1), account(1), at=OPEN, has_stop=True)
        assert not decision.approved
        assert decision.rule == "max_position_contracts"

    def test_reducing_an_oversized_position_is_allowed(
        self, policy: RiskPolicy, db: Database
    ) -> None:
        """The gate must never trap a trader in a position."""
        broker = PaperBroker(db, slippage_ticks=0)
        broker.on_price("ES", Decimal("5000"), OPEN)
        broker.submit(entry(quantity=5), at=OPEN)
        decision = policy.evaluate(
            entry(quantity=5, side=Side.SELL), account(1), at=OPEN, has_stop=True
        )
        assert decision.approved

    def test_rejects_too_many_concurrent_instruments(self, policy: RiskPolicy) -> None:
        decision = policy.evaluate(entry(), account(open_positions=3), at=OPEN, has_stop=True)
        assert not decision.approved
        assert decision.rule == "max_open_positions"

    def test_adding_to_an_existing_position_ignores_the_instrument_cap(
        self, policy: RiskPolicy, db: Database
    ) -> None:
        broker = PaperBroker(db, slippage_ticks=0)
        broker.on_price("ES", Decimal("5000"), OPEN)
        broker.submit(entry(quantity=1), at=OPEN)
        decision = policy.evaluate(entry(quantity=1), account(open_positions=3), at=OPEN,
                                   has_stop=True)
        assert decision.approved


class TestStopLossRequirement:
    def test_entry_without_a_stop_is_refused(self, policy: RiskPolicy) -> None:
        decision = policy.evaluate(entry(), account(), at=OPEN, has_stop=False)
        assert not decision.approved
        assert decision.rule == "require_stop_loss"

    def test_entry_with_a_stop_passes(self, policy: RiskPolicy) -> None:
        assert policy.evaluate(entry(), account(), at=OPEN, has_stop=True).approved

    def test_closing_a_position_needs_no_stop(self, policy: RiskPolicy, db: Database) -> None:
        broker = PaperBroker(db, slippage_ticks=0)
        broker.on_price("ES", Decimal("5000"), OPEN)
        broker.submit(entry(quantity=2), at=OPEN)
        decision = policy.evaluate(
            entry(quantity=2, side=Side.SELL), account(1), at=OPEN, has_stop=False
        )
        assert decision.approved

    def test_can_be_disabled(self, policy: RiskPolicy) -> None:
        policy.config.require_stop_loss = False
        assert policy.evaluate(entry(), account(), at=OPEN, has_stop=False).approved


class TestPerTradeRisk:
    def test_rejects_risk_over_the_limit(self, policy: RiskPolicy) -> None:
        decision = policy.evaluate(
            entry(), account(), at=OPEN, planned_risk=Decimal("900"), has_stop=True
        )
        assert not decision.approved
        assert decision.rule == "max_risk_per_trade"

    def test_allows_risk_within_the_limit(self, policy: RiskPolicy) -> None:
        assert policy.evaluate(
            entry(), account(), at=OPEN, planned_risk=Decimal("300"), has_stop=True
        ).approved


class TestRateLimit:
    def test_trips_after_too_many_orders_in_a_minute(self, policy: RiskPolicy) -> None:
        policy.config.max_orders_per_minute = 3
        policy.config.require_stop_loss = False
        for _ in range(3):
            assert policy.evaluate(entry(), account(), at=OPEN).approved
        decision = policy.evaluate(entry(), account(), at=OPEN)
        assert not decision.approved
        assert decision.rule == "max_orders_per_minute"

    def test_the_window_slides(self, policy: RiskPolicy) -> None:
        policy.config.max_orders_per_minute = 2
        policy.config.require_stop_loss = False
        for _ in range(2):
            policy.evaluate(entry(), account(), at=OPEN)
        assert not policy.evaluate(entry(), account(), at=OPEN).approved
        later = OPEN + timedelta(minutes=2)
        assert policy.evaluate(entry(), account(), at=later).approved


class TestPositionSizing:
    def test_sizes_to_the_risk_budget(self, policy: RiskPolicy) -> None:
        """ES with a 10-point stop risks $500/contract; a $500 budget allows 1."""
        assert policy.position_size_for_risk(
            "ES", Decimal("5000"), Decimal("4990"), Decimal("500")
        ) == 1

    def test_rounds_down(self, policy: RiskPolicy) -> None:
        """$1200 budget over $500/contract is 2, not 2.4."""
        assert policy.position_size_for_risk(
            "ES", Decimal("5000"), Decimal("4990"), Decimal("1200")
        ) == 2

    def test_returns_zero_when_even_one_contract_is_too_much(self, policy: RiskPolicy) -> None:
        """A real answer meaning 'this stop is too wide for your account'."""
        assert policy.position_size_for_risk(
            "ES", Decimal("5000"), Decimal("4900"), Decimal("500")
        ) == 0

    def test_micros_allow_a_larger_count(self, policy: RiskPolicy) -> None:
        """MES risks $50 per 10 points, so a $500 budget allows 10."""
        assert policy.position_size_for_risk(
            "MES", Decimal("5000"), Decimal("4990"), Decimal("500")
        ) == 10


class TestAuditTrail:
    def test_refusals_are_logged_with_their_reason(
        self, policy: RiskPolicy, audit: AuditLog
    ) -> None:
        """'Why did it not trade' must have an answer."""
        policy.evaluate(entry(quantity=10), account(), at=OPEN, has_stop=True)
        refusals = audit.refusals()
        assert len(refusals) == 1
        assert refusals[0].payload["rule"] == "max_position_contracts"

    def test_approvals_are_logged_too(self, policy: RiskPolicy, audit: AuditLog) -> None:
        policy.evaluate(entry(), account(), at=OPEN, has_stop=True)
        assert any(e.event_type == EventType.RISK_APPROVED for e in audit.recent())


class TestLiveTradingIsUnreachable:
    """The safety property the README claims. These tests are the proof."""

    def test_paper_is_always_available(self, db: Database) -> None:
        registry = build_registry(Config(), db)
        assert "paper" in registry
        assert not registry.get("paper").is_live

    def test_live_venues_are_absent_by_default(self, db: Database) -> None:
        registry = build_registry(Config(), db)
        assert registry.names() == ["paper"]

    def test_requesting_a_live_venue_raises(self, db: Database) -> None:
        registry = build_registry(Config(), db)
        with pytest.raises(LiveTradingDisabledError, match="live trading is disabled"):
            registry.get("ninjatrader")

    def test_the_error_explains_how_to_enable_it(self, db: Database) -> None:
        registry = build_registry(Config(), db)
        with pytest.raises(LiveTradingDisabledError, match="allow_live"):
            registry.get("ninjatrader")

    def test_the_flag_alone_does_not_enable_live(self, db: Database) -> None:
        """A boolean is too easy to flip while skimming; the phrase must match."""
        config = Config()
        config.broker.allow_live = True
        assert not config.broker.live_enabled
        with pytest.raises(LiveTradingDisabledError):
            build_registry(config, db).get("ninjatrader")

    def test_the_phrase_alone_does_not_enable_live(self, db: Database) -> None:
        config = Config()
        config.broker.live_confirmation = LIVE_CONFIRMATION_PHRASE
        assert not config.broker.live_enabled

    def test_both_together_enable_it(self, db: Database) -> None:
        config = Config()
        config.broker.allow_live = True
        config.broker.live_confirmation = LIVE_CONFIRMATION_PHRASE
        assert config.broker.live_enabled

    def test_a_mismatched_phrase_is_logged_loudly(self, db: Database, audit: AuditLog) -> None:
        """The trader believes live is on and it is not. Say so."""
        config = Config()
        config.broker.allow_live = True
        config.broker.live_confirmation = "close enough"
        build_registry(config, db, audit)
        assert any(
            e.event_type == EventType.LIVE_TRADING_BLOCKED for e in audit.recent()
        )

    def test_registering_a_live_broker_while_disabled_raises(self) -> None:
        class FakeLive:
            name = "fake"
            is_live = True

        registry = BrokerRegistry(live_enabled=False)
        with pytest.raises(LiveTradingDisabledError, match="will not be available"):
            registry.register(FakeLive())  # type: ignore[arg-type]

    def test_unknown_non_live_broker_gives_a_plain_keyerror(self, db: Database) -> None:
        registry = build_registry(Config(), db)
        with pytest.raises(KeyError, match="No broker named"):
            registry.get("nonsense")
