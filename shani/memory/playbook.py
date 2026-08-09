"""The playbook — learned setups and their retrieval.

This is Shani's analogue of Hermes' skill generation, narrowed to trading. A
:class:`~shani.models.SetupCard` is a piece of procedure the trader demonstrated
and then explained: what triggers it, in what context, what invalidates it, how
it gets managed. Written by the extraction step from interview answers, revised
as evidence accumulates, and retrieved when a matching signal appears.

**Retrieval is the whole point.** A playbook nobody reads at the decision moment
is a diary. The value is in :meth:`Playbook.recall` firing on the *next* signal
and saying: you have taken this seven times, here is what happened, and here is
the pattern in the losses.

**Cards are versioned, never mutated.** Guidance changes as the sample grows,
and seeing *how* it changed is itself informative — a card that has been revised
four times toward "only before 11:00" is telling the trader something a single
current-state row would hide.

**Statistics are presented with their sample size, always.** A card built from
eleven trades showing a 73% win rate is noise, and reporting it bare is how a
tool talks someone into over-sizing. :meth:`Playbook.stats_for` returns the
count alongside every figure, and callers are expected to show it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from shani.db import Database
from shani.models import SetupCard, Signal, Trade, TradeOutcome

__all__ = ["Playbook", "Recall", "SetupStats"]

#: Below this, statistics are labelled provisional everywhere they appear.
MEANINGFUL_SAMPLE = 30


@dataclass(frozen=True, slots=True)
class SetupStats:
    """Measured performance of one setup, always carrying its sample size."""

    setup_id: UUID
    name: str
    sample_size: int
    wins: int
    losses: int
    breakeven: int
    net_pnl: Decimal
    avg_r: float | None
    best_r: float | None
    worst_r: float | None
    #: Time-of-day buckets sorted by net P&L, worst first. For futures this is
    #: usually where the actionable finding is.
    by_time_of_day: tuple[tuple[str, int, Decimal], ...] = ()

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return self.wins / decided if decided else None

    @property
    def is_provisional(self) -> bool:
        return self.sample_size < MEANINGFUL_SAMPLE

    def summary(self) -> str:
        """One line, phrased so the sample size cannot be skipped past."""
        if self.sample_size == 0:
            return f"{self.name}: no trades recorded yet."
        rate = f"{self.win_rate:.0%}" if self.win_rate is not None else "n/a"
        avg = f"{self.avg_r:+.2f}R" if self.avg_r is not None else "n/a"
        line = (
            f"{self.name}: {self.sample_size} trades, {self.wins}W/{self.losses}L "
            f"({rate}), avg {avg}, net ${self.net_pnl:,.2f}"
        )
        if self.is_provisional:
            line += f" — provisional, under {MEANINGFUL_SAMPLE} trades"
        return line


@dataclass(frozen=True, slots=True)
class Recall:
    """What the playbook remembers about a situation."""

    setups: tuple[SetupCard, ...]
    stats: tuple[SetupStats, ...]
    similar_trades: tuple[Trade, ...]

    @property
    def is_empty(self) -> bool:
        return not self.setups and not self.similar_trades

    def brief(self) -> str:
        """Compact prose for a prompt or the portal.

        Explicitly says when it knows nothing. An agent that receives silence
        will invent context; one that receives "no history" will say so.
        """
        if self.is_empty:
            return "No matching history. This looks new."
        lines = [s.summary() for s in self.stats]
        if self.similar_trades:
            lines.append(f"{len(self.similar_trades)} similar past trades on file.")
        return "\n".join(lines)


class Playbook:
    """Reads and writes learned setups."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── writing ──────────────────────────────────────────────────────────────

    def create(self, card: SetupCard) -> SetupCard:
        card.slug = card.slug or slugify(card.name)
        self.db.setups.save(card)
        return card

    def revise(self, existing: SetupCard, **changes: object) -> SetupCard:
        """Write a new version rather than mutating the old one.

        The history matters: a card revised repeatedly toward a narrower time
        window is evidence in its own right, and overwriting would erase it.
        """
        data = existing.model_dump(
            exclude={"id", "created_at", "updated_at", "deleted_at", "version", "supersedes"}
        )
        data.update(changes)
        revised = SetupCard(**data, version=existing.version + 1, supersedes=existing.id)
        self.db.setups.save(revised)
        return revised

    def attach_trade(self, card: SetupCard, trade: Trade) -> SetupCard:
        """Record a trade as an instance of this setup."""
        if trade.id not in card.trade_ids:
            card.trade_ids.append(trade.id)
            self.db.setups.save(card)
        trade.setup_card_id = card.id
        trade.followed_playbook = True
        self.db.trades.save(trade)
        return card

    # ── reading ──────────────────────────────────────────────────────────────

    def current(self) -> list[SetupCard]:
        """The latest version of every setup.

        Superseded versions stay in the database for history but must never
        appear in retrieval, or the agent cites guidance the trader has already
        moved past.
        """
        superseded = {
            c.supersedes for c in self.db.setups.all() if c.supersedes is not None
        }
        return [c for c in self.db.setups.all() if c.id not in superseded]

    def by_slug(self, slug: str) -> SetupCard | None:
        matches = self.db.setups.where(
            "slug = ?", [slug], order_by="version DESC", limit=1
        )
        return matches[0] if matches else None

    def trades_for(self, card: SetupCard) -> list[Trade]:
        return self.db.trades.where(
            "setup_card_id = ? AND exit_at IS NOT NULL", [str(card.id)], order_by="entry_at"
        )

    # ── statistics ───────────────────────────────────────────────────────────

    def stats_for(self, card: SetupCard) -> SetupStats:
        """Measure a setup against the trader's actual results."""
        trades = self.trades_for(card)
        wins = sum(1 for t in trades if t.outcome is TradeOutcome.WIN)
        losses = sum(1 for t in trades if t.outcome is TradeOutcome.LOSS)
        breakeven = sum(1 for t in trades if t.outcome is TradeOutcome.BREAKEVEN)
        net = sum((t.net_pnl for t in trades), start=Decimal(0))

        r_values = [t.r_multiple for t in trades if t.r_multiple is not None]

        buckets: dict[str, list[Trade]] = {}
        for trade in trades:
            if trade.time_of_day is not None:
                buckets.setdefault(trade.time_of_day.label, []).append(trade)
        by_tod = tuple(
            sorted(
                (
                    (label, len(group), sum((t.net_pnl for t in group), start=Decimal(0)))
                    for label, group in buckets.items()
                ),
                key=lambda row: row[2],
            )
        )

        return SetupStats(
            setup_id=card.id,
            name=card.name,
            sample_size=len(trades),
            wins=wins,
            losses=losses,
            breakeven=breakeven,
            net_pnl=net,
            avg_r=sum(r_values) / len(r_values) if r_values else None,
            best_r=max(r_values) if r_values else None,
            worst_r=min(r_values) if r_values else None,
            by_time_of_day=by_tod,
        )

    # ── retrieval ────────────────────────────────────────────────────────────

    def recall(self, signal: Signal, *, limit: int = 3) -> Recall:
        """What do we know about a situation like this one?

        Called when a signal arrives, and the result goes into the agent's
        prompt. Matching is deliberately layered from most to least specific:
        an explicit strategy name beats instrument overlap, which beats
        free-text similarity. Cheap, transparent, and debuggable — an embedding
        model would rank better and be far harder to explain when it ranked
        something absurd.
        """
        cards = self.current()
        scored: list[tuple[int, SetupCard]] = []

        for card in cards:
            score = 0
            if signal.strategy_name:
                needle = slugify(signal.strategy_name)
                if needle == card.slug:
                    score += 100
                elif needle in card.slug or card.slug in needle:
                    score += 50
            if signal.symbol in card.instruments:
                score += 20
            if signal.timeframe and signal.timeframe in card.timeframes:
                score += 10
            if score:
                scored.append((score, card))

        # Fall back to full-text search over the card corpus when nothing
        # matched structurally.
        if not scored and (signal.strategy_name or signal.message):
            query = _fts_query(f"{signal.strategy_name or ''} {signal.message}")
            if query:
                for card in self.db.search_setups(query, limit=limit):
                    scored.append((5, card))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        matched = tuple(card for _, card in scored[:limit])

        similar: list[Trade] = []
        if signal.message or signal.strategy_name:
            query = _fts_query(f"{signal.strategy_name or ''} {signal.message}")
            if query:
                similar = self.db.search_trades(query, limit=5)
        if not similar:
            similar = self.db.trades.where(
                "symbol = ? AND exit_at IS NOT NULL", [signal.symbol],
                order_by="entry_at DESC", limit=5,
            )

        return Recall(
            setups=matched,
            stats=tuple(self.stats_for(c) for c in matched),
            similar_trades=tuple(similar),
        )


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _fts_query(text: str) -> str:
    """Build a safe FTS5 OR query from free text.

    FTS5 has its own query syntax, and passing raw user or webhook text straight
    into MATCH raises on stray quotes and operators. This text originates from
    the internet, so the query is rebuilt from scratch rather than sanitised.

    Stripping punctuation is not sufficient on its own: ``AND``, ``OR``, ``NOT``
    and ``NEAR`` survive as bare alphanumeric tokens and FTS5 then parses them
    as operators, so an alert message containing the word "and" produces a
    syntax error. Each token is therefore quoted, which makes it a literal
    phrase and neutralises every keyword.
    """
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", text) if len(t) > 2][:8]
    return " OR ".join(f'"{t}"' for t in tokens)
