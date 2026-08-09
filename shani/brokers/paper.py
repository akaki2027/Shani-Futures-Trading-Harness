"""Futures paper broker.

The default venue, and the one that makes a fresh clone of Shani immediately
useful: no account, no API key, no money. It is also the engine the learning
loop runs against, so its arithmetic has to be exact even though its fills are
imaginary.

**Deterministic by construction.** The broker owns no clock and fetches no
prices. Time and price both arrive through :meth:`PaperBroker.on_price`. That
single decision is what makes the whole thing testable: a test can replay a
precise sequence of prices and assert on exact fills, with no sleeping, no
network, and no dependence on whether the market happens to be open when CI
runs.

**Deliberately pessimistic, but not pessimistic enough.** Market orders pay
slippage in the adverse direction, stop orders pay slippage on trigger (they
become market orders, and that is exactly when the book is thinnest), and both
sides pay commission. What it still cannot model is queue position, partial
fills in a thin book, or a genuine fast market where your stop is the liquidity.

So paper results flatter you. They always do. :doc:`DISCLAIMER.md` says so
plainly, and the portal repeats it, because a trader who mistakes these numbers
for achievable ones will size accordingly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from shani.brokers.base import (
    AccountSnapshot,
    MarketClosedError,
    OrderRejectedError,
)
from shani.db import Database
from shani.instruments import Instrument, get_instrument
from shani.models import (
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
    Trade,
)
from shani.sessions import classify_session, is_market_open, next_open, time_of_day

__all__ = ["PaperBroker"]

DEFAULT_STARTING_BALANCE = Decimal("100000")
DEFAULT_SLIPPAGE_TICKS = 1


class PaperBroker:
    """A simulated futures execution venue."""

    name = "paper"
    is_live = False

    def __init__(
        self,
        db: Database,
        *,
        starting_balance: Decimal = DEFAULT_STARTING_BALANCE,
        slippage_ticks: int = DEFAULT_SLIPPAGE_TICKS,
        enforce_market_hours: bool = True,
    ) -> None:
        self.db = db
        self.starting_balance = starting_balance
        self.slippage_ticks = slippage_ticks
        self.enforce_market_hours = enforce_market_hours
        #: Last seen price per symbol, for marking positions to market.
        self._last_price: dict[str, Decimal] = {}

    # ── submission ───────────────────────────────────────────────────────────

    def submit(self, order: Order, *, at: datetime | None = None) -> Order:
        """Validate and accept an order.

        Market orders fill immediately *if* a price is known. Everything else
        rests until :meth:`on_price` moves through its trigger.
        """
        instrument = get_instrument(order.symbol)
        now = at or datetime.now(tz=None).astimezone()

        self._validate(order, instrument, now)

        order.broker = self.name
        order.status = OrderStatus.WORKING
        self.db.orders.save(order)

        if order.order_type is OrderType.MARKET:
            price = self._last_price.get(order.symbol)
            if price is None:
                # Refuse rather than invent a reference price. A market order
                # filled against a made-up price produces a P&L figure that
                # looks real and is not.
                order.status = OrderStatus.REJECTED
                order.reject_reason = (
                    f"No price seen for {order.symbol} yet — feed the broker a price via "
                    f"on_price() before submitting a market order."
                )
                self.db.orders.save(order)
                raise OrderRejectedError(order.reject_reason)
            self._execute(order, self._slipped(price, order.side, instrument), now, instrument)

        return order

    def _validate(self, order: Order, instrument: Instrument, now: datetime) -> None:
        if order.quantity <= 0:
            raise OrderRejectedError(f"Quantity must be positive, got {order.quantity}")

        for label, price in (("limit", order.limit_price), ("stop", order.stop_price)):
            if price is None:
                continue
            if price <= 0:
                raise OrderRejectedError(f"{label} price must be positive, got {price}")
            if not instrument.is_on_tick(price):
                raise OrderRejectedError(
                    f"{label} price {price} is not a multiple of {instrument.root}'s "
                    f"{instrument.tick_size} tick — the exchange would reject this. "
                    f"Nearest valid price: {instrument.round_to_tick(price)}"
                )

        if order.order_type is OrderType.STOP_LIMIT:
            assert order.stop_price is not None and order.limit_price is not None
            if order.side is Side.BUY and order.limit_price < order.stop_price:
                raise OrderRejectedError(
                    "Buy stop-limit needs limit >= stop, or it can never fill"
                )
            if order.side is Side.SELL and order.limit_price > order.stop_price:
                raise OrderRejectedError(
                    "Sell stop-limit needs limit <= stop, or it can never fill"
                )

        if self.enforce_market_hours and not is_market_open(now, instrument):
            raise MarketClosedError(
                order.symbol, now, next_open=next_open(now, instrument)
            )

    # ── price-driven execution ───────────────────────────────────────────────

    def on_price(self, symbol: str, price: Decimal, at: datetime) -> list[Fill]:
        """Advance the simulation with a new price for ``symbol``.

        Triggers any resting order whose condition the price satisfies, and
        updates excursion tracking on the open position.
        """
        self._last_price[symbol] = price
        instrument = get_instrument(symbol)
        fills: list[Fill] = []

        self._update_excursions(symbol, price, instrument)

        for order in self._working_orders(symbol):
            fill_price = self._triggered_at(order, price, instrument)
            if fill_price is None:
                continue
            fills.append(self._execute(order, fill_price, at, instrument))

        return fills

    def _triggered_at(
        self, order: Order, price: Decimal, instrument: Instrument
    ) -> Decimal | None:
        """The fill price if this order triggers at ``price``, else ``None``."""
        if order.order_type is OrderType.MARKET:
            return self._slipped(price, order.side, instrument)

        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            # A buy limit sits below the market and fills when price trades down
            # to it. It fills *at the limit*, not at the touch price — assuming
            # price improvement would be the simulator inventing profit.
            if order.side is Side.BUY and price <= order.limit_price:
                return order.limit_price
            if order.side is Side.SELL and price >= order.limit_price:
                return order.limit_price
            return None

        if order.order_type is OrderType.STOP:
            assert order.stop_price is not None
            # A triggered stop becomes a market order, and it triggers precisely
            # when the market is moving against you — so it pays slippage.
            if order.side is Side.BUY and price >= order.stop_price:
                return self._slipped(price, order.side, instrument)
            if order.side is Side.SELL and price <= order.stop_price:
                return self._slipped(price, order.side, instrument)
            return None

        if order.order_type is OrderType.STOP_LIMIT:
            assert order.stop_price is not None and order.limit_price is not None
            triggered = (
                price >= order.stop_price if order.side is Side.BUY else price <= order.stop_price
            )
            if not triggered:
                return None
            if order.side is Side.BUY and price <= order.limit_price:
                return order.limit_price
            if order.side is Side.SELL and price >= order.limit_price:
                return order.limit_price
            # Triggered but gapped through the limit: stays working, which is
            # exactly the real-world failure mode this order type has.
            return None

        return None

    def _slipped(self, price: Decimal, side: Side, instrument: Instrument) -> Decimal:
        """Apply slippage in whichever direction hurts."""
        offset = instrument.tick_size * Decimal(self.slippage_ticks)
        return price + offset if side is Side.BUY else price - offset

    # ── fills and position bookkeeping ───────────────────────────────────────

    def _execute(
        self, order: Order, price: Decimal, at: datetime, instrument: Instrument
    ) -> Fill:
        quantity = order.remaining_quantity
        commission = instrument.commission(quantity, sides=1)

        fill = Fill(
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            commission=commission,
            filled_at=at,
            simulated=True,
        )
        self.db.fills.save(fill)

        order.filled_quantity += quantity
        order.average_fill_price = price
        order.status = OrderStatus.FILLED
        self.db.orders.save(order)

        self._apply_to_position(fill, instrument, at)
        self._cancel_oco_siblings(order, at)
        return fill

    def _cancel_oco_siblings(self, order: Order, at: datetime) -> None:
        """One leg of a bracket filling cancels the other.

        Without this, a stopped-out trade leaves its target resting, and the
        next time price reaches that level the simulator opens a brand-new
        position the trader never asked for.
        """
        if order.oco_group is None:
            return
        for sibling in self.db.orders.where(
            "oco_group = ? AND id != ? AND status IN ('working','pending')",
            [str(order.oco_group), str(order.id)],
        ):
            sibling.status = OrderStatus.CANCELLED
            sibling.reject_reason = f"OCO: sibling order {order.id} filled"
            self.db.orders.save(sibling)

    def _apply_to_position(self, fill: Fill, instrument: Instrument, at: datetime) -> None:
        position = self.position(fill.symbol)
        signed = fill.quantity * fill.side.sign
        before = position.quantity
        after = before + signed

        if before == 0:
            # Opening.
            position.quantity = after
            position.average_price = fill.price
            position.opened_at = at
            position.max_adverse_excursion = Decimal(0)
            position.max_favorable_excursion = Decimal(0)
            self._open_trade(fill, instrument, at)

        elif (before > 0) == (signed > 0):
            # Adding in the same direction — weighted average entry.
            total = abs(before) + abs(signed)
            position.average_price = (
                position.average_price * abs(before) + fill.price * abs(signed)
            ) / total
            position.quantity = after

        else:
            # Reducing, closing, or flipping.
            closing = min(abs(before), abs(signed))
            realized = instrument.pnl(
                position.average_price, fill.price, closing, is_long=before > 0
            )
            position.realized_pnl += realized
            position.quantity = after

            if after == 0:
                self._close_trade(fill, instrument, at, realized, position)
                position.average_price = Decimal(0)
                position.opened_at = None
            elif (after > 0) != (before > 0):
                # Flipped through flat: close the old trade, open a new one at
                # the fill price for the residual.
                self._close_trade(fill, instrument, at, realized, position)
                position.average_price = fill.price
                position.opened_at = at
                position.max_adverse_excursion = Decimal(0)
                position.max_favorable_excursion = Decimal(0)
                self._open_trade(fill, instrument, at, quantity=abs(after))

        self.db.positions.save(position)

    def _update_excursions(
        self, symbol: str, price: Decimal, instrument: Instrument
    ) -> None:
        """Track how far the open position ran for and against.

        MAE is what tells a trader whether a stop is in the right place — a
        string of winners that each went $400 against you first is a very
        different account from one where they never did, and no P&L column
        distinguishes them.
        """
        position = self.position(symbol)
        if position.is_flat:
            return
        open_pnl = instrument.pnl(
            position.average_price, price, position.abs_quantity, is_long=position.is_long
        )
        if open_pnl < position.max_adverse_excursion:
            position.max_adverse_excursion = open_pnl
        if open_pnl > position.max_favorable_excursion:
            position.max_favorable_excursion = open_pnl
        self.db.positions.save(position)

    # ── journal integration ──────────────────────────────────────────────────

    def _open_trade(
        self, fill: Fill, instrument: Instrument, at: datetime, quantity: int | None = None
    ) -> Trade:
        """Create the journal entry as the position opens.

        Created at entry rather than at exit so the chart context — timeframe,
        studies, screenshot — can be attached while it is still true.
        """
        order = self.db.orders.get(fill.order_id)
        trade = Trade(
            symbol=fill.symbol,
            side=fill.side,
            quantity=quantity or fill.quantity,
            entry_price=fill.price,
            entry_at=at,
            commission=fill.commission,
            session=classify_session(at, instrument),
            time_of_day=time_of_day(at),
            signal_id=order.signal_id if order else None,
            proposal_id=order.proposal_id if order else None,
            broker=self.name,
        )
        self.db.trades.save(trade)
        return trade

    def _close_trade(
        self,
        fill: Fill,
        instrument: Instrument,
        at: datetime,
        realized: Decimal,
        position: Position,
    ) -> None:
        open_trades = self.db.trades.where(
            "symbol = ? AND exit_at IS NULL", [fill.symbol], order_by="entry_at DESC", limit=1
        )
        if not open_trades:
            return
        trade = open_trades[0]
        trade.exit_price = fill.price
        trade.exit_at = at
        trade.gross_pnl = realized
        trade.commission += fill.commission
        trade.max_adverse_excursion = position.max_adverse_excursion
        trade.max_favorable_excursion = position.max_favorable_excursion
        self.db.trades.save(trade)

    # ── queries ──────────────────────────────────────────────────────────────

    def position(self, symbol: str) -> Position:
        existing = self.db.positions.where("symbol = ?", [symbol], limit=1)
        return existing[0] if existing else Position(symbol=symbol)

    def positions(self) -> list[Position]:
        return [p for p in self.db.positions.all() if not p.is_flat]

    def open_orders(self) -> list[Order]:
        return self.db.orders.where("status IN ('working','pending','partially_filled')")

    def _working_orders(self, symbol: str) -> list[Order]:
        return self.db.orders.where(
            "symbol = ? AND status IN ('working','pending')", [symbol], order_by="created_at"
        )

    def cancel(self, order_id: UUID) -> Order:
        order = self.db.orders.get(order_id)
        if order is None:
            raise OrderRejectedError(f"No such order: {order_id}")
        if order.status.is_terminal:
            raise OrderRejectedError(
                f"Order {order_id} is already {order.status.value} and cannot be cancelled"
            )
        order.status = OrderStatus.CANCELLED
        self.db.orders.save(order)
        return order

    def account(self, as_of: datetime | None = None) -> AccountSnapshot:
        realized = Decimal(0)
        unrealized = Decimal(0)
        commission = Decimal(0)
        open_count = 0

        for position in self.db.positions.all():
            realized += position.realized_pnl
            if not position.is_flat:
                open_count += 1
                last = self._last_price.get(position.symbol)
                if last is not None:
                    unrealized += get_instrument(position.symbol).pnl(
                        position.average_price, last, position.abs_quantity,
                        is_long=position.is_long,
                    )

        for fill in self.db.fills.all():
            commission += fill.commission

        return AccountSnapshot(
            balance=self.starting_balance + realized - commission,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            commission_paid=commission,
            open_positions=open_count,
            as_of=as_of or datetime.now(tz=None).astimezone(),
        )

    # ── convenience ──────────────────────────────────────────────────────────

    def submit_bracket(
        self,
        *,
        symbol: str,
        side: Side,
        quantity: int,
        stop_loss: Decimal,
        take_profit: Decimal,
        at: datetime,
        entry_type: OrderType = OrderType.MARKET,
        entry_price: Decimal | None = None,
    ) -> tuple[Order, Order, Order]:
        """Submit an entry with an attached protective stop and target.

        The two exits share an OCO group, so whichever fills first cancels the
        other. This is how a trade *should* be entered — with the exit already
        decided — and making it one call rather than three is a deliberate
        nudge toward that.
        """
        entry = Order(
            symbol=symbol, side=side, quantity=quantity,
            order_type=entry_type, limit_price=entry_price,
        )
        self.submit(entry, at=at)

        oco = uuid4()
        exit_side = side.opposite
        stop = Order(
            symbol=symbol, side=exit_side, quantity=quantity,
            order_type=OrderType.STOP, stop_price=stop_loss,
            parent_order_id=entry.id, oco_group=oco, time_in_force=TimeInForce.GTC,
        )
        target = Order(
            symbol=symbol, side=exit_side, quantity=quantity,
            order_type=OrderType.LIMIT, limit_price=take_profit,
            parent_order_id=entry.id, oco_group=oco, time_in_force=TimeInForce.GTC,
        )
        self.submit(stop, at=at)
        self.submit(target, at=at)

        # Record planned risk on the journal entry now, while the intended stop
        # is known. Recomputing it later from a stop the trader has since moved
        # would silently flatter every R multiple.
        instrument = get_instrument(symbol)
        open_trades = self.db.trades.where(
            "symbol = ? AND exit_at IS NULL", [symbol], order_by="entry_at DESC", limit=1
        )
        if open_trades and entry.average_fill_price is not None:
            trade = open_trades[0]
            trade.initial_stop = stop_loss
            trade.initial_target = take_profit
            trade.planned_risk = abs(
                instrument.pnl(entry.average_fill_price, stop_loss, quantity, is_long=side is Side.BUY)
            )
            self.db.trades.save(trade)

        return entry, stop, target
