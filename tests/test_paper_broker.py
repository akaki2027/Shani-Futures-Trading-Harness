"""Paper broker tests.

Every expected dollar figure here is computed by hand from the contract specs
and written as a literal. Deriving expectations from ``instrument.pnl()`` would
make these tests agree with the code no matter what the code did.

The broker takes time and price as arguments, so every test is deterministic:
no sleeping, no network, and no dependence on whether the market is open when
the suite runs.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from shani.brokers.base import MarketClosedError, OrderRejectedError
from shani.brokers.paper import PaperBroker
from shani.db import Database
from shani.models import Order, OrderStatus, OrderType, Side
from shani.sessions import EASTERN

# Tuesday 10 March 2026, 11:00 ET — mid-RTH, unambiguously open.
OPEN = datetime(2026, 3, 10, 11, 0, tzinfo=EASTERN)
LATER = datetime(2026, 3, 10, 11, 30, tzinfo=EASTERN)
# Saturday — unambiguously closed.
CLOSED = datetime(2026, 3, 14, 12, 0, tzinfo=EASTERN)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "broker.db")
    yield database
    database.close()


@pytest.fixture
def broker(db: Database) -> PaperBroker:
    """Zero slippage by default so P&L assertions isolate one variable."""
    return PaperBroker(db, slippage_ticks=0)


@pytest.fixture
def slipping(db: Database) -> PaperBroker:
    return PaperBroker(db, slippage_ticks=1)


def market(symbol: str, side: Side, quantity: int = 1) -> Order:
    return Order(symbol=symbol, side=side, quantity=quantity, order_type=OrderType.MARKET)


class TestValidation:
    def test_off_tick_limit_is_rejected(self, broker: PaperBroker) -> None:
        """The exchange would reject 5001.13. Better to fail here than there."""
        order = Order(symbol="ES", side=Side.BUY, quantity=1,
                      order_type=OrderType.LIMIT, limit_price=Decimal("5001.13"))
        with pytest.raises(OrderRejectedError, match="not a multiple"):
            broker.submit(order, at=OPEN)

    def test_rejection_message_suggests_the_nearest_valid_price(self, broker: PaperBroker) -> None:
        order = Order(symbol="ES", side=Side.BUY, quantity=1,
                      order_type=OrderType.LIMIT, limit_price=Decimal("5001.13"))
        with pytest.raises(OrderRejectedError, match=r"5001\.25"):
            broker.submit(order, at=OPEN)

    def test_on_tick_limit_is_accepted(self, broker: PaperBroker) -> None:
        order = Order(symbol="ES", side=Side.BUY, quantity=1,
                      order_type=OrderType.LIMIT, limit_price=Decimal("5001.25"))
        assert broker.submit(order, at=OPEN).status is OrderStatus.WORKING

    def test_market_closed_is_rejected_with_the_next_open(self, broker: PaperBroker) -> None:
        broker.on_price("ES", Decimal("5000.00"), CLOSED)
        with pytest.raises(MarketClosedError) as exc:
            broker.submit(market("ES", Side.BUY), at=CLOSED)
        assert exc.value.next_open is not None

    def test_market_hours_can_be_disabled_for_replay(self, db: Database) -> None:
        relaxed = PaperBroker(db, slippage_ticks=0, enforce_market_hours=False)
        relaxed.on_price("ES", Decimal("5000.00"), CLOSED)
        assert relaxed.submit(market("ES", Side.BUY), at=CLOSED).status is OrderStatus.FILLED

    def test_market_order_without_a_known_price_is_rejected(self, broker: PaperBroker) -> None:
        """Inventing a reference price would produce fake but plausible P&L."""
        with pytest.raises(OrderRejectedError, match="No price seen"):
            broker.submit(market("ES", Side.BUY), at=OPEN)

    def test_unfillable_stop_limit_is_rejected(self, broker: PaperBroker) -> None:
        order = Order(symbol="ES", side=Side.BUY, quantity=1, order_type=OrderType.STOP_LIMIT,
                      stop_price=Decimal("5010.00"), limit_price=Decimal("5005.00"))
        with pytest.raises(OrderRejectedError, match="can never fill"):
            broker.submit(order, at=OPEN)


class TestMarketOrders:
    def test_fills_immediately_at_the_last_price(self, broker: PaperBroker) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        order = broker.submit(market("ES", Side.BUY), at=OPEN)
        assert order.status is OrderStatus.FILLED
        assert order.average_fill_price == Decimal("5000.00")

    def test_buy_slippage_pays_up(self, slipping: PaperBroker) -> None:
        slipping.on_price("ES", Decimal("5000.00"), OPEN)
        order = slipping.submit(market("ES", Side.BUY), at=OPEN)
        assert order.average_fill_price == Decimal("5000.25")

    def test_sell_slippage_pays_down(self, slipping: PaperBroker) -> None:
        slipping.on_price("ES", Decimal("5000.00"), OPEN)
        order = slipping.submit(market("ES", Side.SELL), at=OPEN)
        assert order.average_fill_price == Decimal("4999.75")

    def test_fill_charges_one_side_of_commission(self, broker: PaperBroker, db: Database) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=2), at=OPEN)
        fill = db.fills.all()[0]
        assert fill.commission == Decimal("5.00")  # 2 contracts * $2.50 per side


class TestLimitOrders:
    def test_buy_limit_rests_until_price_trades_down_to_it(self, broker: PaperBroker) -> None:
        order = Order(symbol="ES", side=Side.BUY, quantity=1,
                      order_type=OrderType.LIMIT, limit_price=Decimal("4990.00"))
        broker.submit(order, at=OPEN)
        assert not broker.on_price("ES", Decimal("5000.00"), OPEN)
        fills = broker.on_price("ES", Decimal("4990.00"), LATER)
        assert len(fills) == 1

    def test_fills_at_the_limit_not_the_touch_price(self, broker: PaperBroker) -> None:
        """Assuming price improvement would be the simulator inventing profit."""
        order = Order(symbol="ES", side=Side.BUY, quantity=1,
                      order_type=OrderType.LIMIT, limit_price=Decimal("4990.00"))
        broker.submit(order, at=OPEN)
        fills = broker.on_price("ES", Decimal("4985.00"), LATER)
        assert fills[0].price == Decimal("4990.00")

    def test_sell_limit_fills_when_price_trades_up_to_it(self, broker: PaperBroker) -> None:
        order = Order(symbol="ES", side=Side.SELL, quantity=1,
                      order_type=OrderType.LIMIT, limit_price=Decimal("5010.00"))
        broker.submit(order, at=OPEN)
        assert not broker.on_price("ES", Decimal("5000.00"), OPEN)
        assert broker.on_price("ES", Decimal("5010.00"), LATER)


class TestStopOrders:
    def test_sell_stop_triggers_when_price_falls_through(self, broker: PaperBroker) -> None:
        order = Order(symbol="ES", side=Side.SELL, quantity=1,
                      order_type=OrderType.STOP, stop_price=Decimal("4990.00"))
        broker.submit(order, at=OPEN)
        assert not broker.on_price("ES", Decimal("5000.00"), OPEN)
        assert broker.on_price("ES", Decimal("4989.00"), LATER)

    def test_triggered_stop_pays_slippage(self, slipping: PaperBroker) -> None:
        """A stop becomes a market order exactly when the book is thinnest."""
        order = Order(symbol="ES", side=Side.SELL, quantity=1,
                      order_type=OrderType.STOP, stop_price=Decimal("4990.00"))
        slipping.submit(order, at=OPEN)
        fills = slipping.on_price("ES", Decimal("4990.00"), LATER)
        assert fills[0].price == Decimal("4989.75")

    def test_stop_limit_that_gaps_through_stays_working(self, broker: PaperBroker) -> None:
        """The real failure mode of this order type: triggered but unfilled."""
        order = Order(symbol="ES", side=Side.SELL, quantity=1, order_type=OrderType.STOP_LIMIT,
                      stop_price=Decimal("4990.00"), limit_price=Decimal("4985.00"))
        submitted = broker.submit(order, at=OPEN)
        assert not broker.on_price("ES", Decimal("4970.00"), LATER)
        assert broker.db.orders.get(submitted.id).status is OrderStatus.WORKING


class TestPositions:
    def test_opening_sets_quantity_and_average_price(self, broker: PaperBroker) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=2), at=OPEN)
        position = broker.position("ES")
        assert position.quantity == 2
        assert position.average_price == Decimal("5000.00")

    def test_short_position_is_negative(self, broker: PaperBroker) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.SELL, quantity=3), at=OPEN)
        position = broker.position("ES")
        assert position.quantity == -3
        assert not position.is_long

    def test_adding_computes_a_weighted_average_entry(self, broker: PaperBroker) -> None:
        """2 @ 5000 then 2 @ 5010 averages to 5005."""
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=2), at=OPEN)
        broker.on_price("ES", Decimal("5010.00"), LATER)
        broker.submit(market("ES", Side.BUY, quantity=2), at=LATER)
        position = broker.position("ES")
        assert position.quantity == 4
        assert position.average_price == Decimal("5005.00")

    def test_closing_realizes_exact_pnl(self, broker: PaperBroker) -> None:
        """Long 2 ES from 5000 to 5004 = 4 pts * $50 * 2 = $400."""
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=2), at=OPEN)
        broker.on_price("ES", Decimal("5004.00"), LATER)
        broker.submit(market("ES", Side.SELL, quantity=2), at=LATER)
        position = broker.position("ES")
        assert position.is_flat
        assert position.realized_pnl == Decimal("400.00")

    def test_short_that_profits_realizes_positive_pnl(self, broker: PaperBroker) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.SELL, quantity=1), at=OPEN)
        broker.on_price("ES", Decimal("4996.00"), LATER)
        broker.submit(market("ES", Side.BUY, quantity=1), at=LATER)
        assert broker.position("ES").realized_pnl == Decimal("200.00")

    def test_partial_close_realizes_only_the_closed_portion(self, broker: PaperBroker) -> None:
        """Close 1 of 3 ES from 5000 to 5004 = $200, with 2 still open."""
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=3), at=OPEN)
        broker.on_price("ES", Decimal("5004.00"), LATER)
        broker.submit(market("ES", Side.SELL, quantity=1), at=LATER)
        position = broker.position("ES")
        assert position.quantity == 2
        assert position.realized_pnl == Decimal("200.00")
        assert position.average_price == Decimal("5000.00")  # unchanged by a reduction

    def test_flipping_through_flat_realizes_and_reopens(self, broker: PaperBroker) -> None:
        """Long 2, sell 3: realize the long, end up short 1 at the fill price."""
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=2), at=OPEN)
        broker.on_price("ES", Decimal("5004.00"), LATER)
        broker.submit(market("ES", Side.SELL, quantity=3), at=LATER)
        position = broker.position("ES")
        assert position.quantity == -1
        assert position.realized_pnl == Decimal("400.00")
        assert position.average_price == Decimal("5004.00")

    def test_pnl_across_instruments_uses_the_right_multiplier(self, broker: PaperBroker) -> None:
        """CL is $10 a penny; using the ES multiplier would be 5x wrong."""
        broker.on_price("CL", Decimal("75.00"), OPEN)
        broker.submit(market("CL", Side.BUY, quantity=1), at=OPEN)
        broker.on_price("CL", Decimal("75.50"), LATER)
        broker.submit(market("CL", Side.SELL, quantity=1), at=LATER)
        assert broker.position("CL").realized_pnl == Decimal("500.00")


class TestExcursions:
    def test_tracks_the_worst_drawdown_while_open(self, broker: PaperBroker) -> None:
        """MAE is what tells you whether your stop is in the right place."""
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=1), at=OPEN)
        broker.on_price("ES", Decimal("4996.00"), LATER)   # -$200
        broker.on_price("ES", Decimal("5008.00"), LATER)   # +$400
        position = broker.position("ES")
        assert position.max_adverse_excursion == Decimal("-200.00")
        assert position.max_favorable_excursion == Decimal("400.00")

    def test_excursions_land_on_the_closed_trade(self, broker: PaperBroker, db: Database) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=1), at=OPEN)
        broker.on_price("ES", Decimal("4996.00"), LATER)
        broker.on_price("ES", Decimal("5004.00"), LATER)
        broker.submit(market("ES", Side.SELL, quantity=1), at=LATER)
        trade = db.trades.all()[0]
        assert trade.max_adverse_excursion == Decimal("-200.00")


class TestOCO:
    def test_stop_filling_cancels_the_target(self, broker: PaperBroker, db: Database) -> None:
        """Otherwise the leftover target opens a position nobody asked for."""
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        _, stop, target = broker.submit_bracket(
            symbol="ES", side=Side.BUY, quantity=1,
            stop_loss=Decimal("4990.00"), take_profit=Decimal("5020.00"), at=OPEN,
        )
        broker.on_price("ES", Decimal("4989.00"), LATER)
        assert db.orders.get(stop.id).status is OrderStatus.FILLED
        assert db.orders.get(target.id).status is OrderStatus.CANCELLED

    def test_target_filling_cancels_the_stop(self, broker: PaperBroker, db: Database) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        _, stop, target = broker.submit_bracket(
            symbol="ES", side=Side.BUY, quantity=1,
            stop_loss=Decimal("4990.00"), take_profit=Decimal("5020.00"), at=OPEN,
        )
        broker.on_price("ES", Decimal("5020.00"), LATER)
        assert db.orders.get(target.id).status is OrderStatus.FILLED
        assert db.orders.get(stop.id).status is OrderStatus.CANCELLED

    def test_bracket_leaves_the_position_flat_after_the_stop(self, broker: PaperBroker) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit_bracket(
            symbol="ES", side=Side.BUY, quantity=1,
            stop_loss=Decimal("4990.00"), take_profit=Decimal("5020.00"), at=OPEN,
        )
        broker.on_price("ES", Decimal("4989.00"), LATER)
        assert broker.position("ES").is_flat


class TestJournalIntegration:
    def test_opening_a_position_creates_a_trade(self, broker: PaperBroker, db: Database) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=1), at=OPEN)
        trades = db.trades.all()
        assert len(trades) == 1
        assert trades[0].is_open

    def test_trade_records_session_context_at_entry(self, broker: PaperBroker, db: Database) -> None:
        """Captured at entry because that is when it is true."""
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=1), at=OPEN)
        trade = db.trades.all()[0]
        assert trade.session is not None and trade.session.value == "rth"
        assert trade.time_of_day is not None and trade.time_of_day.value == "late_morning"

    def test_closing_completes_the_trade(self, broker: PaperBroker, db: Database) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=2), at=OPEN)
        broker.on_price("ES", Decimal("5004.00"), LATER)
        broker.submit(market("ES", Side.SELL, quantity=2), at=LATER)
        trade = db.trades.all()[0]
        assert not trade.is_open
        assert trade.gross_pnl == Decimal("400.00")
        assert trade.commission == Decimal("10.00")   # 2 contracts, both sides
        assert trade.net_pnl == Decimal("390.00")
        assert trade.outcome.value == "win"

    def test_bracket_records_planned_risk_for_r_multiples(
        self, broker: PaperBroker, db: Database
    ) -> None:
        """Risk is captured at entry; recomputing later from a moved stop
        would silently flatter every R multiple in the journal."""
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit_bracket(
            symbol="ES", side=Side.BUY, quantity=1,
            stop_loss=Decimal("4990.00"), take_profit=Decimal("5020.00"), at=OPEN,
        )
        trade = db.trades.all()[0]
        assert trade.planned_risk == Decimal("500.00")   # 10 pts * $50
        assert trade.initial_stop == Decimal("4990.00")

    def test_r_multiple_is_computed_from_planned_risk(
        self, broker: PaperBroker, db: Database
    ) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit_bracket(
            symbol="ES", side=Side.BUY, quantity=1,
            stop_loss=Decimal("4990.00"), take_profit=Decimal("5020.00"), at=OPEN,
        )
        broker.on_price("ES", Decimal("5020.00"), LATER)
        trade = db.trades.all()[0]
        assert trade.gross_pnl == Decimal("1000.00")
        # (1000 - 5.00 commission) / 500 planned risk
        assert trade.r_multiple == pytest.approx(1.99)


class TestAccount:
    def test_starting_balance_with_no_activity(self, broker: PaperBroker) -> None:
        account = broker.account(as_of=OPEN)
        assert account.balance == Decimal("100000")
        assert account.equity == Decimal("100000")

    def test_realized_profit_and_commission_hit_the_balance(self, broker: PaperBroker) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=1), at=OPEN)
        broker.on_price("ES", Decimal("5004.00"), LATER)
        broker.submit(market("ES", Side.SELL, quantity=1), at=LATER)
        account = broker.account(as_of=LATER)
        assert account.realized_pnl == Decimal("200.00")
        assert account.commission_paid == Decimal("5.00")
        assert account.balance == Decimal("100195.00")

    def test_open_position_marks_to_market_in_equity_not_balance(
        self, broker: PaperBroker
    ) -> None:
        """Risk limits read equity, because an open loss is already lost."""
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.submit(market("ES", Side.BUY, quantity=1), at=OPEN)
        broker.on_price("ES", Decimal("5010.00"), LATER)
        account = broker.account(as_of=LATER)
        assert account.unrealized_pnl == Decimal("500.00")
        assert account.equity == account.balance + Decimal("500.00")

    def test_open_position_count(self, broker: PaperBroker) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        broker.on_price("CL", Decimal("75.00"), OPEN)
        broker.submit(market("ES", Side.BUY), at=OPEN)
        broker.submit(market("CL", Side.SELL), at=OPEN)
        assert broker.account(as_of=OPEN).open_positions == 2


class TestCancellation:
    def test_cancelling_a_working_order(self, broker: PaperBroker) -> None:
        order = Order(symbol="ES", side=Side.BUY, quantity=1,
                      order_type=OrderType.LIMIT, limit_price=Decimal("4990.00"))
        broker.submit(order, at=OPEN)
        assert broker.cancel(order.id).status is OrderStatus.CANCELLED

    def test_cancelled_orders_do_not_fill(self, broker: PaperBroker) -> None:
        order = Order(symbol="ES", side=Side.BUY, quantity=1,
                      order_type=OrderType.LIMIT, limit_price=Decimal("4990.00"))
        broker.submit(order, at=OPEN)
        broker.cancel(order.id)
        assert not broker.on_price("ES", Decimal("4985.00"), LATER)

    def test_cancelling_a_filled_order_raises(self, broker: PaperBroker) -> None:
        broker.on_price("ES", Decimal("5000.00"), OPEN)
        order = broker.submit(market("ES", Side.BUY), at=OPEN)
        with pytest.raises(OrderRejectedError, match="already filled"):
            broker.cancel(order.id)
