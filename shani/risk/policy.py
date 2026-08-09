"""The risk gate.

Every order passes through :meth:`RiskPolicy.evaluate` before it reaches a
broker. There is no bypass, and that is the point.

Two principles shape this module:

**A limit that warns is not a limit.** Every check here returns a refusal, not
advice. The moment a trader most wants to override a daily loss limit is the
moment it is doing the most good — three losers deep, certain the next one comes
back. Encoding that judgement while calm is the entire value.

**Refusals explain themselves.** A rejection carries the rule, the number that
breached it, and the current state, because "rejected" with no reason trains a
trader to disable the gate.

The daily loss limit is evaluated against the **trading day** (18:00 ET
boundary), not the calendar day. An overnight session that starts Sunday evening
and ends Monday afternoon is one trading day, and a calendar-day limit would
silently reset in the middle of it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from shani.audit import AuditLog, EventType
from shani.brokers.base import AccountSnapshot
from shani.config import RiskConfig
from shani.db import Database
from shani.instruments import get_instrument
from shani.models import Order, Side
from shani.sessions import session_date

__all__ = ["RiskDecision", "RiskPolicy"]


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The gate's verdict on one order."""

    approved: bool
    reasons: tuple[str, ...] = ()
    rule: str | None = None

    @property
    def message(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "approved"

    @classmethod
    def allow(cls) -> RiskDecision:
        return cls(approved=True)

    @classmethod
    def refuse(cls, rule: str, *reasons: str) -> RiskDecision:
        return cls(approved=False, reasons=reasons, rule=rule)


@dataclass
class RiskPolicy:
    """Evaluates orders against the configured limits."""

    config: RiskConfig
    db: Database
    audit: AuditLog
    #: Submission timestamps for rate limiting. Bounded so a long-running
    #: process cannot grow it without limit.
    _recent_orders: deque[datetime] = field(default_factory=lambda: deque(maxlen=512))

    def evaluate(
        self,
        order: Order,
        account: AccountSnapshot,
        *,
        at: datetime | None = None,
        planned_risk: Decimal | None = None,
        has_stop: bool = False,
    ) -> RiskDecision:
        """Approve or refuse an order, logging the decision either way."""
        now = at or datetime.now(UTC)

        # Evaluated in order, and the first refusal wins. Ordering is
        # deliberate: the cheapest and most absolute checks come first, so a
        # tripped kill switch never bothers to query the database.
        checks: tuple[Callable[[], RiskDecision], ...] = (
            self._kill_switch,
            lambda: self._daily_loss(now),
            lambda: self._rate_limit(now),
            lambda: self._position_size(order),
            lambda: self._open_positions(order, account),
            lambda: self._trade_risk(planned_risk),
            lambda: self._stop_required(order, has_stop),
        )
        for check in checks:
            decision = check()
            if not decision.approved:
                self.audit.warn(
                    EventType.RISK_REJECTED,
                    f"Refused {order.side.value} {order.quantity} {order.symbol}: "
                    f"{decision.message}",
                    payload={
                        "rule": decision.rule,
                        "symbol": order.symbol,
                        "quantity": order.quantity,
                        "reasons": list(decision.reasons),
                    },
                    order_id=order.id,
                )
                return decision

        self._recent_orders.append(now)
        self.audit.record(
            EventType.RISK_APPROVED,
            f"Approved {order.side.value} {order.quantity} {order.symbol}",
            payload={"symbol": order.symbol, "quantity": order.quantity},
            order_id=order.id,
        )
        return RiskDecision.allow()

    # ── individual checks ────────────────────────────────────────────────────

    def _kill_switch(self) -> RiskDecision:
        if self.config.kill_switch:
            return RiskDecision.refuse(
                "kill_switch",
                "Kill switch is engaged — no orders will be accepted. "
                "Disable it in config to resume trading.",
            )
        return RiskDecision.allow()

    def _daily_loss(self, now: datetime) -> RiskDecision:
        """Stop for the session once the loss limit is reached."""
        realized = self.realized_pnl_today(now)
        limit = -abs(self.config.max_daily_loss)
        if realized <= limit:
            return RiskDecision.refuse(
                "max_daily_loss",
                f"Down ${abs(realized):,.2f} on the session, at or beyond the "
                f"${abs(limit):,.2f} daily loss limit. Trading is halted until the "
                f"next session.",
            )
        return RiskDecision.allow()

    def _rate_limit(self, now: datetime) -> RiskDecision:
        """Catch runaway loops before they catch the account."""
        cutoff = now - timedelta(minutes=1)
        recent = sum(1 for t in self._recent_orders if t > cutoff)
        if recent >= self.config.max_orders_per_minute:
            return RiskDecision.refuse(
                "max_orders_per_minute",
                f"{recent} orders in the last minute, at the limit of "
                f"{self.config.max_orders_per_minute}. This usually means a loop, "
                f"not a strategy.",
            )
        return RiskDecision.allow()

    def _position_size(self, order: Order) -> RiskDecision:
        """Resulting position size, not order size.

        Checking the order alone would let five separate one-lots build a
        position the limit was written to prevent.
        """
        existing = self.db.positions.where("symbol = ?", [order.symbol], limit=1)
        current = existing[0].quantity if existing else 0
        resulting = abs(current + order.quantity * order.side.sign)
        if resulting > self.config.max_position_contracts:
            return RiskDecision.refuse(
                "max_position_contracts",
                f"Would leave {resulting} contracts in {order.symbol}, over the limit "
                f"of {self.config.max_position_contracts} (currently {abs(current)}).",
            )
        return RiskDecision.allow()

    def _open_positions(self, order: Order, account: AccountSnapshot) -> RiskDecision:
        """Cap concurrent instruments — but never block closing a position."""
        existing = self.db.positions.where("symbol = ?", [order.symbol], limit=1)
        already_open = bool(existing and not existing[0].is_flat)
        if already_open:
            return RiskDecision.allow()
        if account.open_positions >= self.config.max_open_positions:
            return RiskDecision.refuse(
                "max_open_positions",
                f"{account.open_positions} positions already open, at the limit of "
                f"{self.config.max_open_positions}. Close something before opening "
                f"{order.symbol}.",
            )
        return RiskDecision.allow()

    def _trade_risk(self, planned_risk: Decimal | None) -> RiskDecision:
        if planned_risk is None:
            return RiskDecision.allow()
        if abs(planned_risk) > self.config.max_risk_per_trade:
            return RiskDecision.refuse(
                "max_risk_per_trade",
                f"Planned risk of ${abs(planned_risk):,.2f} exceeds the per-trade "
                f"limit of ${self.config.max_risk_per_trade:,.2f}. Size down or move "
                f"the stop closer.",
            )
        return RiskDecision.allow()

    def _stop_required(self, order: Order, has_stop: bool) -> RiskDecision:
        """Entries need a protective stop; exits obviously do not."""
        if not self.config.require_stop_loss or has_stop:
            return RiskDecision.allow()
        if self._is_reducing(order):
            return RiskDecision.allow()
        return RiskDecision.refuse(
            "require_stop_loss",
            "Entry has no protective stop attached. Submit it as a bracket, or "
            "disable require_stop_loss if you genuinely manage exits manually.",
        )

    def _is_reducing(self, order: Order) -> bool:
        existing = self.db.positions.where("symbol = ?", [order.symbol], limit=1)
        if not existing or existing[0].is_flat:
            return False
        current = existing[0].quantity
        return (current > 0 and order.side is Side.SELL) or (
            current < 0 and order.side is Side.BUY
        )

    # ── session accounting ───────────────────────────────────────────────────

    def realized_pnl_today(self, now: datetime | None = None) -> Decimal:
        """Realized P&L for the current *trading* day.

        Uses the 18:00 ET session boundary, so an overnight session counts as
        one day. A calendar-day limit would reset at midnight, in the middle of
        the Asian session, and quietly grant a fresh loss allowance.
        """
        moment = now or datetime.now(UTC)
        today = session_date(moment)
        total = Decimal(0)
        for trade in self.db.trades.where("exit_at IS NOT NULL"):
            if trade.exit_at and session_date(trade.exit_at) == today:
                total += trade.net_pnl
        return total

    def remaining_daily_loss(self, now: datetime | None = None) -> Decimal:
        """How much more can be lost before trading halts. Never negative."""
        realized = self.realized_pnl_today(now)
        limit = abs(self.config.max_daily_loss)
        return max(Decimal(0), limit + min(realized, Decimal(0)))

    def position_size_for_risk(
        self, symbol: str, entry: Decimal, stop: Decimal, risk_budget: Decimal | None = None
    ) -> int:
        """Largest contract count keeping entry-to-stop risk within budget.

        Rounds down, and returns zero when even one contract is too much — which
        is a real answer meaning "this stop is too wide for your account", not a
        failure.
        """
        instrument = get_instrument(symbol)
        budget = risk_budget or self.config.max_risk_per_trade
        per_contract = abs(instrument.pnl(entry, stop, 1, is_long=True))
        if per_contract == 0:
            return 0
        return int(budget / per_contract)
