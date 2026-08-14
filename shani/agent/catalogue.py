"""Live model catalogue.

OpenRouter fronts several hundred models from a couple of dozen providers, and
the roster changes weekly. A hardcoded list is stale before it ships, so the
catalogue is fetched and cached.

Pricing is carried through deliberately. Shani runs two tiers with very
different call volumes — triage fires on every incoming signal, extraction runs
once per closed trade — so putting a costly model in the triage slot is an easy
and expensive mistake. Showing the per-million-token price at the moment of
choosing is the cheapest possible guard against it.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = ["ModelCatalogue", "ModelCatalogueError"]

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"


class ModelCatalogueError(RuntimeError):
    """The catalogue could not be fetched."""


@dataclass
class ModelCatalogue:
    """Fetches and caches the provider's model list."""

    ttl_seconds: int = 900
    _cached: list[dict[str, Any]] = field(default_factory=list)
    _expires: float = 0.0

    def invalidate(self) -> None:
        self._expires = 0.0

    def fetch(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if not refresh and self._cached and self._expires > time.monotonic():
            return self._cached

        try:
            # The catalogue is public — no key needed to browse models, which
            # matters because the settings panel has to be usable *before* a key
            # has been entered.
            key = os.environ.get("OPENROUTER_API_KEY", "")
            headers = {"User-Agent": "shani/0.1"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            response = httpx.get(OPENROUTER_MODELS, headers=headers, timeout=20.0)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise ModelCatalogueError(
                f"Could not fetch the OpenRouter model list: {exc}"
            ) from exc

        models = [_normalise(m) for m in payload.get("data", [])]
        models.sort(key=lambda m: str(m["name"]).lower())
        self._cached = models
        self._expires = time.monotonic() + self.ttl_seconds
        return models


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    pricing = raw.get("pricing") or {}
    return {
        "id": raw.get("id", ""),
        "name": raw.get("name") or raw.get("id", ""),
        "context_length": raw.get("context_length"),
        # OpenRouter quotes price per token as a string. Per-million is the unit
        # people actually reason in, and a string keeps the exactness.
        "prompt_per_m": _per_million(pricing.get("prompt")),
        "completion_per_m": _per_million(pricing.get("completion")),
        "is_free": _per_million(pricing.get("prompt")) == "0.00",
        "modalities": (raw.get("architecture") or {}).get("input_modalities") or [],
    }


def _per_million(value: Any) -> str | None:
    try:
        return f"{float(value) * 1_000_000:.2f}"
    except (TypeError, ValueError):
        return None
