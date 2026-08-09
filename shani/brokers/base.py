"""The broker interface every execution venue implements.

One interface, several venues: the paper simulator today, NinjaTrader and
others later. Code above this line — the risk gate, the agent, the portal —
never learns which one it is talking to.

That indirection is load-bearing for safety rather than merely tidy. Because
every venue looks identical from above, the registry can decline to construct
live venues at all while ``allow_live`` is false, and no calling code needs a
special case for it. See :mod:`shani.brokers.registry`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable
from uuid import UUID

from shani.models import Fill, Order, Position

__all__ = [
    "AccountSnapshot",
    "Broker",
    "BrokerError",
    "InsufficientMarginError",
    "MarketClosedError",
    "OrderRejectedError",
]


class BrokerError(Exception):
    """Base for broker failures."""


class OrderRejectedError(BrokerError):
    """The order was refused before it ever reached the book."""


class MarketClosedError(OrderRejectedError):
    """The instrument is not trading right now.

    Distinct from a generic rejection so the portal can offer the genuinely
    useful reply — *when* the market next opens — instead of just refusing.
    """

    def __init__(self, symbol: str, at: datetime, next_open: datetime | None = None) -> None:
        msg = f"{symbol} is not trading at {at.isoformat()}"
        if next_open is not None:
            msg += f"; next open is {next_open.isoformat()}"
        super().__init__(msg)
        self.symbol = symbol
        self.at = at
        self.next_open = next_open


class InsufficientMarginError(OrderRejectedError):
    """Not enough account equity to support the position."""


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Account state at a moment in time.

    ``balance`` is settled cash; ``equity`` marks open positions to market.
    Risk limits are evaluated against ``equity``, because a trader who is down
    four thousand dollars on an open position has already lost it in every
    sense that matters for deciding whether to add another contract.
    """

    balance: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    commission_paid: Decimal
    open_positions: int
    as_of: datetime

    @property
    def equity(self) -> Decimal:
        return self.balance + self.unrealized_pnl


@runtime_checkable
class Broker(Protocol):
    """What every execution venue must provide."""

    name: str
    is_live: bool

    def submit(self, order: Order) -> Order:
        """Send an order. Returns it with venue state applied.

        Raises :class:`OrderRejectedError` when the order cannot be accepted.
        Implementations must reject rather than silently adjust — a quantity
        quietly clamped to fit margin is a bug that only surfaces as a
        confusing fill.
        """
        ...

    def cancel(self, order_id: UUID) -> Order:
        """Cancel a working order."""
        ...

    def position(self, symbol: str) -> Position:
        """Current position. Returns a flat position when there is none."""
        ...

    def positions(self) -> list[Position]:
        """All non-flat positions."""
        ...

    def open_orders(self) -> list[Order]:
        """Orders still capable of filling."""
        ...

    def account(self) -> AccountSnapshot:
        """Current account state."""
        ...

    def on_price(self, symbol: str, price: Decimal, at: datetime) -> list[Fill]:
        """Advance the venue's clock with a new price.

        Live venues ignore this — their fills arrive over the wire. The
        simulator uses it to drive resting orders, which is what makes the
        paper broker deterministic and therefore testable.
        """
        ...
