"""Domain models.

Two structural decisions are worth understanding before editing anything here,
because both are cheap now and expensive later.

**Every persisted record carries a UUID, an ``updated_at``, and a soft-delete
tombstone** (:class:`SyncRecord`). Shani is single-device today. A companion
phone app is not built, but it is intended, and the moment two devices can both
create rows offline, integer autoincrement keys collide and hard deletes become
un-syncable — a deleted row is indistinguishable from a row the other device
hasn't seen yet. Retrofitting this onto a populated database is genuinely
painful. Adopting it on day one costs nothing.

**Money and prices are ``Decimal``.** See the rationale in
:mod:`shani.instruments`. Statistics and ratios may be ``float``; anything that
represents dollars or a price may not.

The central entity is :class:`Trade` — a completed round trip, with not just the
numbers but the *context*: which chart, which session, what the trader said they
were thinking. That context is the corpus the learning loop trains on, and it is
the thing an ordinary trade log throws away.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from shani.sessions import Session, TimeOfDay

__all__ = [
    "AuditEvent",
    "Fill",
    "InterviewAnswer",
    "Order",
    "OrderStatus",
    "OrderType",
    "Position",
    "Proposal",
    "SetupCard",
    "Side",
    "Signal",
    "SyncRecord",
    "TimeInForce",
    "Trade",
    "TradeOutcome",
]

Money = Annotated[Decimal, Field(description="USD, exact")]
Price = Annotated[Decimal, Field(description="Instrument price, must sit on a tick")]


def _now() -> datetime:
    return datetime.now(UTC)


class SyncRecord(BaseModel):
    """Base for every persisted record.

    Provides the three fields a delta-sync protocol needs: a globally unique id
    that two offline devices cannot collide on, a monotonic ``updated_at`` to
    drive ``GET /changes?since=…``, and a tombstone so deletions propagate
    instead of resurrecting.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    deleted_at: datetime | None = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def touch(self) -> None:
        """Mark modified. Call on every mutation or sync will miss the change."""
        self.updated_at = _now()

    def soft_delete(self) -> None:
        self.deleted_at = _now()
        self.touch()


# ─── Enumerations ────────────────────────────────────────────────────────────


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 for buy, -1 for sell. Signed quantity lives on this."""
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    """Accepted by Shani, not yet working at the venue."""
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}

    @property
    def is_active(self) -> bool:
        return self in {OrderStatus.PENDING, OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED}


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class TradeOutcome(str, Enum):
    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"
    OPEN = "open"


class SignalSource(str, Enum):
    PINE_WEBHOOK = "pine_webhook"
    """Plane C — a TradingView alert from the user's own script."""
    SCREENER = "screener"
    """Plane A — a scan matched."""
    MANUAL = "manual"
    """The trader initiated it in the portal."""
    AGENT = "agent"


class ProposalDecision(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ─── Execution ───────────────────────────────────────────────────────────────


class Fill(SyncRecord):
    """A single execution against an order. Orders may fill in several pieces."""

    order_id: UUID
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    price: Price
    commission: Money = Decimal(0)
    filled_at: datetime = Field(default_factory=_now)
    #: True when the simulator, not a venue, produced this fill.
    simulated: bool = True


class Order(SyncRecord):
    """An order, from submission to terminal state."""

    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    order_type: OrderType
    limit_price: Price | None = None
    stop_price: Price | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    status: OrderStatus = OrderStatus.PENDING
    broker: str = "paper"

    filled_quantity: int = 0
    average_fill_price: Price | None = None
    reject_reason: str | None = None

    #: Bracket linkage. A protective stop and target both point at the entry
    #: order that created them; filling one cancels its sibling (OCO).
    parent_order_id: UUID | None = None
    oco_group: UUID | None = None

    #: Provenance — which signal and which proposal led here. This is what makes
    #: the audit trail reconstructable after the fact.
    signal_id: UUID | None = None
    proposal_id: UUID | None = None

    @field_validator("limit_price")
    @classmethod
    def _limit_required(cls, v: Price | None, info: Any) -> Price | None:
        if info.data.get("order_type") in {OrderType.LIMIT, OrderType.STOP_LIMIT} and v is None:
            raise ValueError(f"{info.data['order_type'].value} order requires a limit_price")
        return v

    @field_validator("stop_price")
    @classmethod
    def _stop_required(cls, v: Price | None, info: Any) -> Price | None:
        if info.data.get("order_type") in {OrderType.STOP, OrderType.STOP_LIMIT} and v is None:
            raise ValueError(f"{info.data['order_type'].value} order requires a stop_price")
        return v

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity


class Position(SyncRecord):
    """An open position in one instrument.

    ``quantity`` is signed: positive is long, negative is short, zero is flat.
    One signed field removes an entire class of bug in which direction and size
    disagree.
    """

    symbol: str
    quantity: int = 0
    average_price: Price = Decimal(0)
    realized_pnl: Money = Decimal(0)
    opened_at: datetime | None = None

    #: Running excursion extremes, in dollars, for the *current* position.
    #: Maximum Adverse Excursion is how far underwater you went before it
    #: worked — the number that tells you whether your stop is in the right
    #: place, which no P&L figure can.
    max_adverse_excursion: Money = Decimal(0)
    max_favorable_excursion: Money = Decimal(0)

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def abs_quantity(self) -> int:
        return abs(self.quantity)


# ─── Signals and proposals ───────────────────────────────────────────────────


class Signal(SyncRecord):
    """Something worth looking at happened.

    ``raw_payload`` preserves the webhook body verbatim. It is **untrusted
    input** — it arrives over the internet and flows toward a language model
    that proposes trades. It is fenced as data before it ever reaches a prompt;
    see :mod:`shani.agent.propose`.
    """

    source: SignalSource
    symbol: str
    side: Side | None = None
    price: Price | None = None
    timeframe: str | None = None
    strategy_name: str | None = None
    message: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=_now)

    #: Plane B context captured at signal time: what the trader's chart actually
    #: showed. Null when TradingView Desktop was unreachable.
    chart_symbol: str | None = None
    chart_timeframe: str | None = None
    chart_studies: list[str] = Field(default_factory=list)


class Proposal(SyncRecord):
    """The agent's suggested action for a signal, with its reasoning shown.

    ``cited_setup_ids`` is the honesty mechanism: it records exactly which past
    trades the agent leaned on. A proposal that cites nothing is the agent
    guessing, and the portal renders it differently so the trader can tell the
    difference at a glance.
    """

    signal_id: UUID
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    entry_price: Price | None = None
    stop_loss: Price | None = None
    take_profit: Price | None = None

    reasoning: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    cited_setup_ids: list[UUID] = Field(default_factory=list)
    risk_dollars: Money | None = None
    reward_risk_ratio: float | None = None

    decision: ProposalDecision = ProposalDecision.PENDING
    decided_at: datetime | None = None
    model: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_grounded(self) -> bool:
        """Did this proposal cite the trader's actual history?"""
        return bool(self.cited_setup_ids)


# ─── The journal ─────────────────────────────────────────────────────────────


class InterviewAnswer(BaseModel):
    """One question and its answer from the post-trade interview."""

    question: str
    answer: str
    asked_at: datetime = Field(default_factory=_now)
    answered_at: datetime | None = None

    @property
    def latency_seconds(self) -> float | None:
        """How long the trader took to answer.

        Tracked because it is diagnostic: an answer given four hours later is a
        reconstruction, not a recollection, and the extraction step weights it
        accordingly.
        """
        if self.answered_at is None:
            return None
        return (self.answered_at - self.asked_at).total_seconds()


class Trade(SyncRecord):
    """A completed round trip — the unit the learning loop trains on.

    Deliberately fatter than a trade log row. The numbers alone (entry, exit,
    P&L) are what every journal already stores and what has never been enough.
    The fields that matter here are the ones underneath: which session, what the
    chart showed, what the trader said, and which setup it turned out to be an
    instance of.
    """

    symbol: str
    #: The dated contract actually traded (``ESZ5``), as distinct from the
    #: continuous chart symbol (``CME:ES1!``). Statistics that pool across a
    #: rollover without this are quietly wrong every quarter.
    contract: str | None = None

    side: Side
    quantity: int = Field(gt=0)
    entry_price: Price
    exit_price: Price | None = None
    entry_at: datetime
    exit_at: datetime | None = None

    gross_pnl: Money = Decimal(0)
    commission: Money = Decimal(0)
    max_adverse_excursion: Money = Decimal(0)
    max_favorable_excursion: Money = Decimal(0)

    #: Planned risk at entry, in dollars — the denominator of R. Captured at
    #: entry rather than recomputed later, because the stop may be moved.
    planned_risk: Money | None = None
    initial_stop: Price | None = None
    initial_target: Price | None = None

    session: Session | None = None
    time_of_day: TimeOfDay | None = None

    #: Plane B context, captured at entry.
    chart_timeframe: str | None = None
    chart_studies: list[str] = Field(default_factory=list)
    #: Path to the chart screenshot taken at entry. The single most valuable
    #: artefact in the journal — it is what the trader actually saw.
    entry_screenshot: str | None = None

    signal_id: UUID | None = None
    proposal_id: UUID | None = None
    #: The setup card this trade was an instance of, if any. Null means the
    #: trade was off-playbook — which is itself a finding worth measuring.
    setup_card_id: UUID | None = None
    followed_playbook: bool = False

    interview: list[InterviewAnswer] = Field(default_factory=list)
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    broker: str = "paper"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.commission

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return self.exit_at is None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def outcome(self) -> TradeOutcome:
        if self.is_open:
            return TradeOutcome.OPEN
        net = self.net_pnl
        if net > 0:
            return TradeOutcome.WIN
        if net < 0:
            return TradeOutcome.LOSS
        return TradeOutcome.BREAKEVEN

    @computed_field  # type: ignore[prop-decorator]
    @property
    def r_multiple(self) -> float | None:
        """Net P&L as a multiple of planned risk.

        R is the only way to compare a 1-lot scalp against a 5-lot swing on the
        same axis. Returns ``None`` rather than zero when risk was never
        recorded — an unknown R must not be averaged in as a flat outcome.
        """
        if self.planned_risk is None or self.planned_risk == 0 or self.is_open:
            return None
        return float(self.net_pnl / abs(self.planned_risk))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_seconds(self) -> float | None:
        if self.exit_at is None:
            return None
        return (self.exit_at - self.entry_at).total_seconds()

    @property
    def has_interview(self) -> bool:
        return any(a.answered_at is not None for a in self.interview)


# ─── Learned memory ──────────────────────────────────────────────────────────


class SetupCard(SyncRecord):
    """A trading setup the agent has learned from the trader's own history.

    This is the Hermes "skill" analogue, and the artefact that makes Shani more
    than a journal. It is written by the extraction step from interview answers
    and trade context, then revised as evidence accumulates.

    Versioned rather than mutated: a card's guidance changes as the sample
    grows, and being able to see *how it changed* matters. ``supersedes`` points
    at the previous version.
    """

    name: str
    slug: str
    description: str = ""

    #: What has to be true for this setup to be present. Prose, not code —
    #: these are read by a language model and by the trader, not executed.
    trigger: str = ""
    context: str = ""
    invalidation: str = ""
    management: str = ""

    instruments: list[str] = Field(default_factory=list)
    timeframes: list[str] = Field(default_factory=list)
    preferred_sessions: list[Session] = Field(default_factory=list)
    preferred_times: list[TimeOfDay] = Field(default_factory=list)

    version: int = 1
    supersedes: UUID | None = None
    trade_ids: list[UUID] = Field(default_factory=list)

    #: Generated Pine source, if this card has been pushed to TradingView.
    pine_source: str | None = None

    #: Set only after walk-forward validation. Until then the agent may describe
    #: the setup but must not present it as a validated edge.
    validated: bool = False
    validation_note: str = ""

    @property
    def sample_size(self) -> int:
        return len(self.trade_ids)

    @property
    def is_statistically_meaningful(self) -> bool:
        """Thirty trades is a low bar and still better than the alternative.

        Below it, the portal and the agent must present the card's statistics as
        provisional. Ten trades showing a 70% win rate is noise, and a system
        that reports it without qualification is actively misleading.
        """
        return self.sample_size >= 30


class AuditEvent(SyncRecord):
    """One entry in the append-only decision log.

    Never updated, never soft-deleted in normal operation. If Shani places an
    order, this table explains why — which matters for debugging, and matters
    more the day a trader needs to reconstruct what happened during a bad
    session.
    """

    event_type: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    signal_id: UUID | None = None
    proposal_id: UUID | None = None
    order_id: UUID | None = None
    trade_id: UUID | None = None
    severity: str = "info"
