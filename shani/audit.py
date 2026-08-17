"""Append-only decision log.

If Shani places an order, this table explains why. That matters for debugging,
and it matters far more the day a trader needs to reconstruct what happened
during a session that went badly — which is exactly when memory is least
reliable and the stakes for guessing are highest.

Everything on the path from signal to fill writes here: the signal arriving, the
agent's proposal and what it cited, every risk-gate decision including the
refusals, and the order itself. Refusals are logged at least as carefully as
approvals; "why did it *not* trade" is a real question with a real answer.

Append-only by convention rather than by trigger — nothing in the codebase
updates or deletes these rows. Keeping it enforceable-by-inspection is worth
more here than a database constraint nobody can read.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from shani.db import Database
from shani.models import AuditEvent

__all__ = ["AuditLog", "EventType"]


class EventType:
    """Event type constants.

    A plain namespace rather than an ``Enum`` so that adding a type never
    invalidates rows already written by an older version — the log outlives the
    code that wrote it.
    """

    SIGNAL_RECEIVED = "signal.received"
    SIGNAL_REJECTED = "signal.rejected"

    PROPOSAL_CREATED = "proposal.created"
    PROPOSAL_ACCEPTED = "proposal.accepted"
    PROPOSAL_REJECTED = "proposal.rejected"

    RISK_APPROVED = "risk.approved"
    RISK_REJECTED = "risk.rejected"
    KILL_SWITCH_TRIPPED = "risk.kill_switch"
    DAILY_LOSS_LIMIT_HIT = "risk.daily_loss_limit"

    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_REJECTED = "order.rejected"

    TRADE_OPENED = "trade.opened"
    TRADE_CLOSED = "trade.closed"
    TRADE_IMPORTED = "trade.imported"
    FILL_OBSERVED = "fill.observed"
    """A fill seen live at the venue, as opposed to one Shani placed itself."""

    INTERVIEW_STARTED = "interview.started"
    INTERVIEW_COMPLETED = "interview.completed"
    SETUP_CARD_CREATED = "setup.created"
    SETUP_CARD_REVISED = "setup.revised"

    LIVE_TRADING_BLOCKED = "safety.live_blocked"
    CONFIG_CHANGED = "config.changed"


class AuditLog:
    """Writes to the decision log."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        event_type: str,
        summary: str,
        *,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
        signal_id: UUID | None = None,
        proposal_id: UUID | None = None,
        order_id: UUID | None = None,
        trade_id: UUID | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_type=event_type,
            summary=summary,
            severity=severity,
            payload=payload or {},
            signal_id=signal_id,
            proposal_id=proposal_id,
            order_id=order_id,
            trade_id=trade_id,
        )
        self.db.audit.save(event)
        return event

    def warn(self, event_type: str, summary: str, **kwargs: Any) -> AuditEvent:
        return self.record(event_type, summary, severity="warning", **kwargs)

    def error(self, event_type: str, summary: str, **kwargs: Any) -> AuditEvent:
        return self.record(event_type, summary, severity="error", **kwargs)

    def recent(self, limit: int = 100) -> list[AuditEvent]:
        return self.db.audit.all(limit=limit)

    def for_trade(self, trade_id: UUID) -> list[AuditEvent]:
        """Everything logged about one trade, oldest first.

        This is the reconstruction view: what arrived, what the agent said, what
        the gate decided, what filled.
        """
        return self.db.audit.where(
            "trade_id = ?", [str(trade_id)], order_by="created_at ASC"
        )

    def refusals(self, limit: int = 50) -> list[AuditEvent]:
        """Recent risk-gate refusals — "why did it not trade"."""
        return self.db.audit.where(
            "event_type IN (?, ?, ?)",
            [EventType.RISK_REJECTED, EventType.KILL_SWITCH_TRIPPED,
             EventType.DAILY_LOSS_LIMIT_HIT],
            order_by="created_at DESC",
            limit=limit,
        )
