"""Turning headlines into a directional read.

The colour on each card comes from here, so this module's honesty determines
whether the news section is useful or actively misleading.

Three decisions shape it:

**Batched, not one call per headline.** Forty headlines at one call each is
forty times the cost and latency for no gain. They go in one request with
stable indices.

**Classified for *futures*, not for a company.** "Apple beats earnings" is
strongly bullish for AAPL and roughly noise for the S&P. The prompt is explicit
that the subject is index and commodity futures, because a generic
sentiment model reads company news as market news and gets this wrong constantly.

**Unrated is a real answer.** When no model is configured, or a call fails, or
an item cannot be parsed, it stays :attr:`Lean.UNRATED` and renders grey. It
does not become NEUTRAL — "we did not read this" and "this is balanced" are
different claims, and only one of them is honest.

Headlines are attacker-adjacent text: anyone can publish a page containing
instructions aimed at a model. They are fenced before they reach the prompt. The
structural protection is stronger than the fencing, though — this classifier can
only tint a card. It cannot place an order.
"""

from __future__ import annotations

import logging

from shani.agent.llm import LLM, LLMError, fence
from shani.news.base import Lean, NewsItem

__all__ = ["classify"]

log = logging.getLogger(__name__)

SYSTEM = """\
You read financial headlines and judge, for each one, whether it gives INDEX AND \
COMMODITY FUTURES (S&P 500, Nasdaq, crude oil, gold) a reason to move up or down.

You are not judging whether the news is good or bad in general. You are judging \
directional pressure on futures prices.

Key distinctions:
- Company-specific news is usually NEUTRAL for index futures unless the company \
is large enough to move the index, or it signals something sector-wide.
- Rates, inflation, central bank language, employment, and geopolitics are what \
actually move index futures.
- Supply, inventories, OPEC and conflict move crude. Real yields, the dollar and \
haven demand move gold.
- Opinion pieces, previews and "what to watch" articles are NEUTRAL. They \
contain no new information.
- Social media chatter is sentiment, not fact. Rate it, but keep confidence low.

For each item return:
  lean       overall read for risk assets: strong_bearish, bearish, neutral, \
bullish, strong_bullish
  markets    per-market direction, only for markets this genuinely bears on,
             e.g. {"CL": "bullish", "ES": "bearish"}
  confidence 0.0-1.0 — how strongly this actually argues its direction
  rationale  at most 12 words, concrete

`markets` is NOT the overall lean repeated. Direction really does differ by
market: an oil supply disruption is bullish for CL and bearish for ES in the
same sentence.

Valid market codes: ES (S&P 500), NQ (Nasdaq 100), CL (crude oil), GC (gold).

Populate `markets` whenever an item plausibly bears on one, including when the
direction is `neutral` for that market — a market you have considered and judged
flat is useful information, and is different from one you did not consider.

Typical mappings, to be applied with judgement rather than mechanically:
- Rates, inflation, Fed language, jobs, growth  → ES and NQ. Gold too, via real
  yields, usually in the opposite direction to rates.
- Oil supply, OPEC, inventories, Middle East    → CL. Often ES as well, since a
  sustained oil move feeds inflation and risk appetite.
- Tech earnings, AI capex, semiconductors       → NQ, and ES more weakly.
- Haven demand, dollar strength, geopolitics    → GC, and usually ES inversely.
- Company news with no sector read              → leave `markets` empty.

If a headline is genuinely irrelevant to all four — celebrity news, personal
finance advice, a regional market close — leave `markets` empty and mark the
overall lean neutral.

Use `neutral` freely. Most headlines are noise, and marking noise as directional \
is the failure mode that makes a tool like this worthless.

Reserve confidence above 0.7 for genuine market-moving information: a rate \
decision, a surprise print, a supply shock.

Respond with JSON: {"items": [{"i": <index>, "lean": ..., "markets": {...}, \
"confidence": ..., "rationale": ...}]}\
"""

#: Markets the per-item read may name. Anything else is discarded rather than
#: displayed — a model inventing "SPY" or "BTC" must not create a column the
#: rest of the application has no contract specification for.
KNOWN_MARKETS = frozenset({"ES", "NQ", "CL", "GC"})


def classify(items: list[NewsItem], llm: LLM, *, batch_size: int = 25) -> list[NewsItem]:
    """Assign a lean to each item, in place.

    Items already carrying a lean are skipped, so a refresh does not pay to
    reclassify what has not changed.
    """
    if not llm.enabled:
        return items

    pending = [item for item in items if item.lean is Lean.UNRATED]
    if not pending:
        return items

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        try:
            _classify_batch(batch, llm)
        except LLMError as exc:
            # Leave the batch UNRATED and carry on. A failed classification must
            # not empty the news section — grey cards with real headlines beat
            # no headlines.
            log.warning("news classification failed for a batch of %d: %s", len(batch), exc)
    return items


def _classify_batch(batch: list[NewsItem], llm: LLM) -> None:
    lines = []
    for index, item in enumerate(batch):
        tagged = f" [{', '.join(item.symbols)}]" if item.symbols else ""
        body = f"{item.title}"
        if item.summary:
            body += f" — {item.summary[:200]}"
        lines.append(f"{index}. ({item.source}){tagged} {body}")

    prompt = (
        f"Judge these {len(batch)} items.\n\n"
        + fence("\n".join(lines), "headlines")
        + "\n\nReturn one entry per index, including neutral ones."
    )

    result = llm.complete_json(SYSTEM, prompt, tier="triage", max_tokens=2500)

    for entry in result.get("items", []):
        try:
            index = int(entry.get("i", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(batch):
            continue
        item = batch[index]
        item.lean = Lean.from_text(entry.get("lean"))
        item.rationale = str(entry.get("rationale", ""))[:120]

        markets = entry.get("markets")
        if isinstance(markets, dict):
            resolved: dict[str, Lean] = {}
            for code, direction in markets.items():
                symbol = str(code).strip().upper()
                if symbol not in KNOWN_MARKETS:
                    continue
                lean = Lean.from_text(direction)
                if lean is not Lean.UNRATED:
                    resolved[symbol] = lean
            item.market_leans = resolved
            # Keep the keyword-guessed tags in step with what the model
            # actually judged, so the UI does not show a symbol chip with no
            # corresponding read behind it.
            if resolved:
                item.symbols = sorted(resolved)
        try:
            item.confidence = max(0.0, min(1.0, float(entry.get("confidence", 0))))
        except (TypeError, ValueError):
            item.confidence = 0.0
        # A rated item with no stated confidence is a guess. Say so rather than
        # rendering it at full strength.
        if item.lean is not Lean.NEUTRAL and item.confidence == 0.0:
            item.confidence = 0.25
