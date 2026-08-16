"""News aggregation.

Pulls every enabled connector, deduplicates, classifies, and summarises into a
single directional read for the desk.

**Partial failure is normal and must stay visible.** One connector rate-limited
should not empty the feed, but it also must not fail silently — the response
carries a per-connector status so the UI can say "Reddit is down" instead of
quietly showing less news than the trader thinks they are seeing. That
distinction is the whole difference between a feed you can trust and one you
cannot.

**Classification is cached with the item.** Refreshing re-fetches headlines but
does not re-pay a model to read the same story twice.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from shani.agent.llm import LLM
from shani.news.base import Lean, NewsItem, NewsProvider, ProviderError
from shani.news.providers import ALL_PROVIDERS
from shani.news.sentiment import classify

__all__ = ["Digest", "NewsService"]


@dataclass(frozen=True, slots=True)
class Digest:
    """The desk-level read across everything fetched."""

    lean: Lean
    score: float
    bullish: int
    bearish: int
    neutral: int
    unrated: int
    headline: str

    def to_json(self) -> dict[str, Any]:
        return {
            "lean": self.lean.value,
            "lean_label": self.lean.label,
            "score": round(self.score, 2),
            "bullish": self.bullish,
            "bearish": self.bearish,
            "neutral": self.neutral,
            "unrated": self.unrated,
            "headline": self.headline,
        }


@dataclass
class NewsService:
    """Fetches, dedupes, classifies, and caches."""

    cache_seconds: int = 300
    providers: list[NewsProvider] = field(default_factory=lambda: list(ALL_PROVIDERS))
    #: Classified items keyed by id, so a refresh does not reclassify.
    _seen: dict[str, NewsItem] = field(default_factory=dict)
    _cache: tuple[float, dict[str, Any]] | None = None

    def invalidate(self) -> None:
        """Drop the cache — called after a connector is configured, so a new
        credential takes effect immediately rather than up to five minutes later."""
        self._cache = None

    # ── connectors ───────────────────────────────────────────────────────────

    def connectors(self) -> list[dict[str, Any]]:
        """Every connector and whether it can currently run."""
        out = []
        for provider in self.providers:
            info = provider.info
            out.append({
                "id": info.id,
                "name": info.name,
                "description": info.description,
                "requires_key": info.requires_key,
                "key_env_var": info.key_env_var,
                "signup_url": info.signup_url,
                "available": provider.available(),
            })
        return out

    # ── fetching ─────────────────────────────────────────────────────────────

    def fetch(
        self,
        symbols: list[str],
        llm: LLM,
        *,
        limit: int = 40,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if not refresh and self._cache and self._cache[0] > time.monotonic():
            return self._cache[1]

        collected: list[NewsItem] = []
        statuses: list[dict[str, Any]] = []

        for provider in self.providers:
            info = provider.info
            if not provider.available():
                statuses.append({
                    "id": info.id, "name": info.name, "ok": False,
                    "count": 0, "detail": "not configured",
                })
                continue
            try:
                items = provider.fetch(symbols, limit)
            except ProviderError as exc:
                statuses.append({
                    "id": info.id, "name": info.name, "ok": False,
                    "count": 0, "detail": str(exc)[:160],
                })
                continue
            except Exception as exc:  # a connector must never take down the feed
                statuses.append({
                    "id": info.id, "name": info.name, "ok": False,
                    "count": 0, "detail": f"{type(exc).__name__}: {exc}"[:160],
                })
                continue

            collected.extend(items)
            statuses.append({
                "id": info.id, "name": info.name, "ok": True,
                "count": len(items), "detail": None,
            })

        deduped = self._dedupe(collected)
        deduped.sort(key=lambda i: i.published_at, reverse=True)
        deduped = deduped[:limit]

        # Reuse any classification we already paid for — including the
        # per-market reads. Omitting market_leans here meant a cached item kept
        # its overall lean but lost its per-market breakdown forever, so every
        # market chip read "no news" no matter how much was in the feed.
        for item in deduped:
            cached = self._seen.get(item.id)
            if cached is not None and cached.lean is not Lean.UNRATED:
                item.lean = cached.lean
                item.confidence = cached.confidence
                item.rationale = cached.rationale
                item.market_leans = dict(cached.market_leans)
                if cached.symbols:
                    item.symbols = list(cached.symbols)

        classify(deduped, llm)
        for item in deduped:
            self._seen[item.id] = item
        if len(self._seen) > 800:
            # Bounded — this runs for weeks at a time.
            for key in list(self._seen)[:400]:
                del self._seen[key]

        payload = {
            "items": [item.to_json() for item in deduped],
            "digest": self._digest(deduped).to_json(),
            # One read per market the trader actually watches. This is the
            # answer to "what is the news saying about ES specifically", which
            # a single blended digest cannot give.
            "markets": [
                {"symbol": symbol, **self._digest(deduped, symbol).to_json()}
                for symbol in symbols
            ],
            "connectors": statuses,
            "classified": llm.enabled,
        }
        self._cache = (time.monotonic() + self.cache_seconds, payload)
        return payload

    # ── helpers ──────────────────────────────────────────────────────────────

    def _dedupe(self, items: list[NewsItem]) -> list[NewsItem]:
        """Drop the same story arriving from several sources.

        Matched on id first, then on a normalised title — wires syndicate the
        same copy under different bylines, and three cards of one story would
        overweight it in the digest.
        """
        by_id: dict[str, NewsItem] = {}
        titles: set[str] = set()
        for item in items:
            if item.id in by_id:
                continue
            fingerprint = "".join(c for c in item.title.lower() if c.isalnum())[:60]
            if fingerprint and fingerprint in titles:
                continue
            titles.add(fingerprint)
            by_id[item.id] = item
        return list(by_id.values())

    def _digest(self, items: list[NewsItem], symbol: str | None = None) -> Digest:
        """A directional read — for the whole feed, or for one market.

        Weighted by confidence and by recency — a strong item from four hours
        ago should not outweigh a fresh one — and unrated items are excluded
        from the average rather than counted as neutral, which would drag every
        reading toward zero and make the digest look calmer than the tape.

        With ``symbol`` set, only items the classifier judged to bear on that
        market count, using *that market's* direction. A story can be bullish
        crude and bearish equities, and averaging those together would produce a
        confident-looking zero that describes neither.
        """
        if symbol is None:
            considered = [(i, i.lean) for i in items]
        else:
            considered = [
                (i, i.market_leans[symbol]) for i in items if symbol in i.market_leans
            ]

        rated = [(i, lean) for i, lean in considered if lean is not Lean.UNRATED]
        bullish = sum(1 for _, lean in rated if lean.score > 0)
        bearish = sum(1 for _, lean in rated if lean.score < 0)
        neutral = sum(1 for _, lean in rated if lean.score == 0)
        unrated = len(considered) - len(rated)

        if not rated:
            return Digest(
                lean=Lean.UNRATED, score=0.0, bullish=0, bearish=0,
                neutral=0, unrated=unrated,
                headline=(
                    f"Nothing in the feed bears on {symbol} right now."
                    if symbol
                    else "Nothing classified yet — configure a model to read the tape."
                ),
            )

        total_weight = 0.0
        weighted = 0.0
        for item, lean in rated:
            recency = 1.0 if item.age_minutes() < 120 else 0.5
            weight = max(0.1, item.confidence) * recency
            weighted += lean.score * weight
            total_weight += weight

        score = weighted / total_weight if total_weight else 0.0

        if score <= -1.0:
            lean = Lean.STRONG_BEARISH
        elif score < -0.25:
            lean = Lean.BEARISH
        elif score <= 0.25:
            lean = Lean.NEUTRAL
        elif score < 1.0:
            lean = Lean.BULLISH
        else:
            lean = Lean.STRONG_BULLISH

        subject = symbol or "Tape"
        if lean is Lean.NEUTRAL:
            headline = (
                f"{subject} reads balanced — {bullish} up, {bearish} down, "
                f"{neutral} neutral."
            )
        else:
            direction = "higher" if score > 0 else "lower"
            headline = (
                f"{subject} leans {direction}: {bullish} bullish vs {bearish} bearish "
                f"across {len(rated)} item{'s' if len(rated) != 1 else ''}."
            )

        return Digest(
            lean=lean, score=score, bullish=bullish, bearish=bearish,
            neutral=neutral, unrated=unrated, headline=headline,
        )
