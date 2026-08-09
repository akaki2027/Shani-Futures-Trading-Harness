"""The agent: proposing, interviewing, and extracting.

Three steps, one loop.

**Propose** — a signal arrives; the agent describes what it would do and why,
with the trader's own matching history in the prompt. Proposals cite the setup
cards they leaned on, and a proposal that cites nothing is marked ungrounded so
the portal can render it differently. That distinction matters: the difference
between "you have done this 40 times and it works" and "this looks plausible to
a language model" is the difference between the product and a chatbot.

**Interview** — a trade closes; within seconds the trader is asked why they took
it. Freshness is the entire game. An answer given four hours later is a
reconstruction, and reconstructions are tidy, flattering, and useless. The
questions are deliberately concrete and answerable in a sentence, because a
questionnaire that feels like homework does not get filled in, and an unanswered
interview teaches nothing.

**Extract** — answers plus trade context become a setup card. This runs on the
strong model tier: a badly-extracted card poisons the playbook for months, and
it is the one call per trade where cost is irrelevant.

Untrusted text — webhook payloads, alert messages — is fenced by
:func:`~shani.agent.llm.fence` before it reaches any prompt. The real protection
is structural, though: nothing here executes anything. The agent produces a
proposal, the risk gate evaluates it, and a human confirms it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from shani.agent.llm import LLM, LLMError, fence
from shani.audit import AuditLog, EventType
from shani.db import Database
from shani.instruments import Instrument, get_instrument
from shani.memory.playbook import Playbook, Recall, slugify
from shani.models import (
    InterviewAnswer,
    OrderType,
    Proposal,
    SetupCard,
    Side,
    Signal,
    Trade,
)

__all__ = ["DEFAULT_QUESTIONS", "Agent"]

#: Asked after every closed trade.
#:
#: Short on purpose. Five questions answerable in a sentence each get answered;
#: fifteen thorough ones get skipped, and a skipped interview teaches nothing.
#: The last two matter most — the plan-versus-execution gap is where the
#: repeatable mistakes live, and they are the questions traders least want to
#: answer honestly, which is exactly why they are asked while it is fresh.
DEFAULT_QUESTIONS: tuple[str, ...] = (
    "What did you see that made you take this trade?",
    "What would have told you the idea was wrong?",
    "Where did you plan to exit, and did you actually exit there?",
    "Would you take this again tomorrow in the same conditions?",
    "Was there anything about how you were feeling that affected this one?",
)

PROPOSE_SYSTEM = """\
You are a trading assistant for a futures trader. You do not place orders and \
you do not predict the market. Your job is to describe what the trader's own \
recorded history says about a situation like the one in front of them.

Rules you must follow:
- Ground every claim in the supplied history. If the history is thin or absent, \
say so plainly rather than filling the gap with generic technical-analysis prose.
- Never state or imply that a setup will work. Report what happened before.
- Quantify with the sample size attached. "4 wins in 7 trades" is useful; \
"usually works" is not.
- If the trader's history shows a pattern in the losses (a time of day, a \
session, a condition), say it directly. That is the most valuable thing you can \
contribute.
- Keep reasoning under 150 words.

Respond with JSON:
{"side": "buy"|"sell", "quantity": int, "stop_loss": number|null,
 "take_profit": number|null, "reasoning": str, "confidence": 0.0-1.0,
 "cited_setups": [str]}

`confidence` reflects how well the trader's history covers this situation, not \
how likely the trade is to win. No history means low confidence.\
"""

EXTRACT_SYSTEM = """\
You turn a trader's post-trade interview into a reusable setup card.

Write in the trader's own language and concepts. Do not substitute textbook \
terminology for what they actually said — if they call it "the failed push", \
that is the name. The card has to be recognisable to them at 09:31 next Tuesday.

Be specific and observable. "Wait for confirmation" is useless. "Price rejects \
the opening range high and the next pullback holds VWAP" is a setup.

If the interview is too vague to produce a real setup, say so by returning a \
low `confidence`. A vague card is worse than no card: it will match everything \
and mean nothing.

Respond with JSON:
{"name": str, "description": str, "trigger": str, "context": str,
 "invalidation": str, "management": str, "timeframes": [str],
 "confidence": 0.0-1.0}\
"""


class Agent:
    """Proposes, interviews, and extracts."""

    def __init__(self, db: Database, llm: LLM, audit: AuditLog) -> None:
        self.db = db
        self.llm = llm
        self.audit = audit
        self.playbook = Playbook(db)

    # ── propose ──────────────────────────────────────────────────────────────

    def propose(self, signal: Signal, *, max_quantity: int = 1) -> Proposal | None:
        """Turn a signal into a proposal, grounded in the trader's history."""
        recall = self.playbook.recall(signal)
        instrument = get_instrument(signal.symbol)

        try:
            result = self.llm.complete_json(
                PROPOSE_SYSTEM,
                self._propose_prompt(signal, recall, max_quantity),
                tier="triage",
            )
        except LLMError as exc:
            self.audit.warn(
                EventType.SIGNAL_REJECTED,
                f"Could not produce a proposal for {signal.symbol}: {exc}",
                signal_id=signal.id,
            )
            return None

        side = _side(result.get("side")) or signal.side
        if side is None:
            return None

        quantity = max(1, min(int(result.get("quantity") or 1), max_quantity))
        stop = _price(result.get("stop_loss"), instrument)
        target = _price(result.get("take_profit"), instrument)

        cited = [
            card.id
            for card in recall.setups
            if card.slug in {slugify(str(s)) for s in result.get("cited_setups") or []}
        ]

        risk = None
        if stop is not None and signal.price is not None:
            risk = abs(instrument.pnl(signal.price, stop, quantity, is_long=side is Side.BUY))

        reward_risk = None
        if stop and target and signal.price:
            risk_pts = abs(signal.price - stop)
            if risk_pts > 0:
                reward_risk = float(abs(target - signal.price) / risk_pts)

        proposal = Proposal(
            signal_id=signal.id,
            symbol=signal.symbol,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            entry_price=signal.price,
            stop_loss=stop,
            take_profit=target,
            reasoning=str(result.get("reasoning", ""))[:2000],
            confidence=_confidence(result.get("confidence")),
            cited_setup_ids=cited,
            risk_dollars=risk,
            reward_risk_ratio=reward_risk,
            model=self.llm.model_for("triage"),
        )
        self.db.proposals.save(proposal)
        self.audit.record(
            EventType.PROPOSAL_CREATED,
            f"Proposed {side.value} {quantity} {signal.symbol}"
            + ("" if cited else " (ungrounded — no matching history)"),
            payload={"grounded": bool(cited), "confidence": proposal.confidence},
            signal_id=signal.id,
            proposal_id=proposal.id,
        )
        return proposal

    def _propose_prompt(self, signal: Signal, recall: Recall, max_quantity: int) -> str:
        parts = [
            f"Instrument: {signal.symbol} ({get_instrument(signal.symbol).name})",
            f"Signal side: {signal.side.value if signal.side else 'unspecified'}",
            f"Price: {signal.price if signal.price is not None else 'unknown'}",
            f"Timeframe: {signal.timeframe or 'unspecified'}",
            f"Maximum position permitted by risk settings: {max_quantity} contracts",
            "",
            "=== The trader's own history for situations like this ===",
            recall.brief(),
        ]

        if recall.setups:
            parts.append("\n=== Matching setup cards ===")
            for card, stats in zip(recall.setups, recall.stats, strict=False):
                parts.append(
                    f"\n[{card.slug}] {card.name}\n"
                    f"  Trigger: {card.trigger or 'not recorded'}\n"
                    f"  Invalidation: {card.invalidation or 'not recorded'}\n"
                    f"  Record: {stats.summary()}"
                )
                if stats.by_time_of_day:
                    worst = stats.by_time_of_day[0]
                    parts.append(
                        f"  Worst time of day: {worst[0]} "
                        f"({worst[1]} trades, ${worst[2]:,.2f})"
                    )

        if recall.similar_trades:
            parts.append("\n=== Recent similar trades ===")
            for trade in recall.similar_trades[:5]:
                r = f"{trade.r_multiple:+.2f}R" if trade.r_multiple is not None else "n/a"
                when = trade.time_of_day.label if trade.time_of_day else "unknown time"
                parts.append(
                    f"  {trade.entry_at:%Y-%m-%d} {when}: {trade.side.value} "
                    f"{trade.symbol} → ${trade.net_pnl:,.2f} ({r})"
                )

        # The alert body is attacker-controllable: it arrives over the internet.
        if signal.message or signal.strategy_name:
            parts.append("\n" + fence(
                f"strategy: {signal.strategy_name or 'unnamed'}\nmessage: {signal.message}",
                "alert payload",
            ))

        return "\n".join(parts)

    # ── interview ────────────────────────────────────────────────────────────

    def start_interview(self, trade: Trade) -> Trade:
        """Attach the questions to a freshly closed trade."""
        if trade.interview:
            return trade
        trade.interview = [InterviewAnswer(question=q, answer="") for q in DEFAULT_QUESTIONS]
        self.db.trades.save(trade)
        self.audit.record(
            EventType.INTERVIEW_STARTED,
            f"Interview opened for {trade.symbol} ({trade.net_pnl:+,.2f})",
            trade_id=trade.id,
        )
        return trade

    def record_answer(self, trade: Trade, index: int, answer: str) -> Trade:
        from datetime import UTC, datetime

        if not 0 <= index < len(trade.interview):
            raise IndexError(f"No question at index {index}")
        trade.interview[index].answer = answer.strip()
        trade.interview[index].answered_at = datetime.now(UTC)
        self.db.trades.save(trade)
        return trade

    # ── extract ──────────────────────────────────────────────────────────────

    def extract_setup(
        self, trade: Trade, *, screenshot: bytes | None = None
    ) -> SetupCard | None:
        """Distil an answered interview into a setup card.

        Runs on the strong tier. If a card with the same slug already exists,
        the trade is attached to it and the card revised, rather than creating a
        near-duplicate — a playbook of forty variations on one idea is not a
        playbook.
        """
        answered = [a for a in trade.interview if a.answer.strip()]
        if not answered:
            return None

        prompt_parts = [
            f"Instrument: {trade.symbol}",
            f"Direction: {trade.side.value}",
            f"Session: {trade.session.value if trade.session else 'unknown'}",
            f"Time of day: {trade.time_of_day.label if trade.time_of_day else 'unknown'}",
            f"Timeframe on chart: {trade.chart_timeframe or 'unknown'}",
            f"Indicators on chart: {', '.join(trade.chart_studies) or 'none recorded'}",
            f"Result: ${trade.net_pnl:,.2f}"
            + (f" ({trade.r_multiple:+.2f}R)" if trade.r_multiple is not None else ""),
            "",
            "=== Interview ===",
        ]
        for a in answered:
            prompt_parts.append(f"Q: {a.question}\nA: {a.answer}\n")

        try:
            result = self.llm.complete_json(
                EXTRACT_SYSTEM, "\n".join(prompt_parts), tier="reasoning"
            )
        except LLMError as exc:
            self.audit.warn(
                EventType.INTERVIEW_COMPLETED,
                f"Could not extract a setup from {trade.symbol}: {exc}",
                trade_id=trade.id,
            )
            return None

        name = str(result.get("name") or "").strip()
        if not name or _confidence(result.get("confidence")) < 0.4:
            # A vague card matches everything and means nothing. Better none.
            self.audit.record(
                EventType.INTERVIEW_COMPLETED,
                "Interview too vague to produce a setup card — no card written.",
                trade_id=trade.id,
            )
            return None

        slug = slugify(name)
        fields: dict[str, Any] = {
            "name": name,
            "slug": slug,
            "description": str(result.get("description", ""))[:1000],
            "trigger": str(result.get("trigger", ""))[:1000],
            "context": str(result.get("context", ""))[:1000],
            "invalidation": str(result.get("invalidation", ""))[:1000],
            "management": str(result.get("management", ""))[:1000],
            "instruments": [trade.symbol],
            "timeframes": [str(t)[:8] for t in (result.get("timeframes") or [])][:5],
        }

        if (existing := self.playbook.by_slug(slug)) is not None:
            fields["instruments"] = sorted(set(existing.instruments) | {trade.symbol})
            fields["trade_ids"] = existing.trade_ids
            card = self.playbook.revise(existing, **fields)
            event, verb = EventType.SETUP_CARD_REVISED, "Revised"
        else:
            card = self.playbook.create(SetupCard(**fields))
            event, verb = EventType.SETUP_CARD_CREATED, "Learned"

        self.playbook.attach_trade(card, trade)
        self.audit.record(
            event,
            f"{verb} setup '{card.name}' (v{card.version}, {card.sample_size} trades)",
            payload={"slug": card.slug, "version": card.version},
            trade_id=trade.id,
        )
        return card


def _side(value: Any) -> Side | None:
    text = str(value or "").strip().lower()
    if text in {"buy", "long"}:
        return Side.BUY
    if text in {"sell", "short"}:
        return Side.SELL
    return None


def _price(value: Any, instrument: Instrument) -> Decimal | None:
    """Coerce a model-supplied price onto a valid tick.

    A model will confidently propose an ES stop at 4991.13. Snapping is right
    here — refusing would discard an otherwise sound proposal over a rounding
    artefact, and the exchange would reject the raw value outright.
    """
    if value is None:
        return None
    try:
        rounded: Decimal = instrument.round_to_tick(Decimal(str(value)))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return rounded


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
