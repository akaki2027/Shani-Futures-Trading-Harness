"""News: items, sources, and the directional read.

The point of this section is not to show headlines — every platform does that.
It is to answer one question fast: **does this give the market a reason to go up
or down?** A trader scanning at 09:15 has ninety seconds, and a wall of
undifferentiated text is worse than nothing because it costs time without
changing a decision.

So every item carries a :class:`Lean` — a five-level directional read rendered
as colour, from strong-bearish through neutral to strong-bullish.

Two rules keep this honest:

**Confidence is separate from direction.** A weak signal shows as a pale colour
rather than a confident one. Rendering a guess in the same red as a Fed decision
is how a tool talks someone into a trade.

**Nothing here is a signal.** A directional read on a headline is one input among
many, and the section says so. Shani's actual edge is the trader's own measured
history; this is context around it, and the UI is deliberately built so the news
panel never sits where a proposal would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

__all__ = ["Lean", "NewsItem", "NewsProvider", "ProviderError", "ProviderInfo"]


class ProviderError(RuntimeError):
    """A source could not be read.

    Raised rather than returning nothing, because an empty feed and a broken
    connector look identical to a reader and mean opposite things — one says the
    tape is quiet, the other says you are flying blind.
    """


class Lean(str, Enum):
    """Which way an item argues, if it argues at all."""

    STRONG_BEARISH = "strong_bearish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    BULLISH = "bullish"
    STRONG_BULLISH = "strong_bullish"
    UNRATED = "unrated"
    """Not yet classified — no model configured, or classification failed.
    Deliberately distinct from NEUTRAL: "no opinion" and "balanced" are
    different claims, and colouring an unread item yellow implies a judgement
    that was never made."""

    @property
    def score(self) -> int:
        """-2 through +2. UNRATED is 0 but must not be averaged as neutral."""
        return {
            Lean.STRONG_BEARISH: -2,
            Lean.BEARISH: -1,
            Lean.NEUTRAL: 0,
            Lean.BULLISH: 1,
            Lean.STRONG_BULLISH: 2,
            Lean.UNRATED: 0,
        }[self]

    @property
    def label(self) -> str:
        return {
            Lean.STRONG_BEARISH: "Strongly bearish",
            Lean.BEARISH: "Leans bearish",
            Lean.NEUTRAL: "Neutral",
            Lean.BULLISH: "Leans bullish",
            Lean.STRONG_BULLISH: "Strongly bullish",
            Lean.UNRATED: "Not rated",
        }[self]

    @classmethod
    def from_text(cls, value: Any) -> Lean:
        """Parse a model's answer forgivingly.

        Models return "bullish", "STRONG_BULLISH", "strongly bullish", and
        occasionally a sentence. Falling back to UNRATED rather than NEUTRAL
        matters: an unparseable answer means we do not know, and saying "neutral"
        would invent a judgement.
        """
        text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
        aliases = {
            "very_bearish": cls.STRONG_BEARISH,
            "strongly_bearish": cls.STRONG_BEARISH,
            "negative": cls.BEARISH,
            "down": cls.BEARISH,
            "flat": cls.NEUTRAL,
            "mixed": cls.NEUTRAL,
            "positive": cls.BULLISH,
            "up": cls.BULLISH,
            "very_bullish": cls.STRONG_BULLISH,
            "strongly_bullish": cls.STRONG_BULLISH,
        }
        if text in aliases:
            return aliases[text]
        try:
            return cls(text)
        except ValueError:
            return cls.UNRATED


@dataclass(slots=True)
class NewsItem:
    """One headline, with its directional read."""

    id: str
    """Stable across refreshes — usually the URL. Used to dedupe the same story
    arriving from several sources, and to avoid reclassifying what we already
    paid a model to read."""

    title: str
    source: str
    url: str
    published_at: datetime
    summary: str = ""
    #: Contract roots this plausibly bears on, e.g. ``["ES", "NQ"]``.
    symbols: list[str] = field(default_factory=list)

    lean: Lean = Lean.UNRATED
    #: 0.0–1.0. Drives colour intensity, so a weak read renders pale.
    confidence: float = 0.0
    #: One line on *why* it leans that way. Without this the colour is an
    #: unfalsifiable assertion.
    rationale: str = ""

    def age_minutes(self, now: datetime | None = None) -> float:
        reference = now or datetime.now(UTC)
        published = self.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        return max(0.0, (reference - published).total_seconds() / 60)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "url": self.url,
            "published_at": self.published_at.isoformat(),
            "summary": self.summary,
            "symbols": self.symbols,
            "lean": self.lean.value,
            "lean_label": self.lean.label,
            "score": self.lean.score,
            "confidence": round(self.confidence, 2),
            "rationale": self.rationale,
            "age_minutes": round(self.age_minutes(), 1),
        }


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """What a connector is and what it needs, for the settings UI."""

    id: str
    name: str
    description: str
    #: Environment variable holding the credential, or ``None`` if none needed.
    key_env_var: str | None = None
    #: Where to get a key, shown next to the field.
    signup_url: str | None = None
    requires_key: bool = False
    enabled_by_default: bool = False


@runtime_checkable
class NewsProvider(Protocol):
    """A source of headlines.

    Adding one means implementing this and registering it — no changes to the
    API or the portal, which is the point of the shape.
    """

    info: ProviderInfo

    def available(self) -> bool:
        """Can this run right now? False when a required key is missing."""
        ...

    def fetch(self, symbols: list[str], limit: int) -> list[NewsItem]:
        """Recent items. Raises :class:`ProviderError` on failure."""
        ...
