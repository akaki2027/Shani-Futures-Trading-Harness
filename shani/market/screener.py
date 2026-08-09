"""Plane A — headless market data.

Quotes, screeners, and technical ratings for the futures universe, with no
account, no browser, and no credentials. This is the plane that always works,
so everything above it degrades gracefully when the other two are unavailable.

**The version pin is load-bearing.** ``tradingview-screener`` must stay at
3.0.0. From 3.2.0 every bare ``Query()`` injects a default equity preset that
matches nothing on the futures scanner, so queries return zero rows *silently* —
no exception, no warning, just an empty watchlist. Shani is futures-first, so
that would break the core product invisibly. Credit to
``atilaahmettaner/tradingview-mcp`` for documenting the failure mode; the pin
and its reasoning live in ``pyproject.toml``.

**Caching is not an optimisation here.** The upstream endpoint is undocumented
and rate-limited, and a portal that polls a watchlist every few seconds will get
throttled. The TTL cache exists so that a page refresh does not cost an upstream
call, and so a rate-limit response degrades to slightly stale data rather than
an empty screen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from shani.instruments import INSTRUMENTS, get_instrument

__all__ = ["MarketSnapshot", "ScreenerProvider", "ScreenerUnavailableError"]


class ScreenerUnavailableError(RuntimeError):
    """Upstream data could not be fetched.

    Raised rather than returning empty results, because an empty watchlist and a
    watchlist of nothing-is-moving look identical to a caller and mean opposite
    things.
    """


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """A point-in-time quote for one instrument."""

    symbol: str
    tv_symbol: str
    name: str
    last: Decimal | None
    change: Decimal | None
    change_percent: float | None
    high: Decimal | None
    low: Decimal | None
    volume: int | None
    as_of: float

    @property
    def is_up(self) -> bool:
        return (self.change or Decimal(0)) > 0


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


@dataclass
class ScreenerProvider:
    """Fetches futures market data from TradingView's public scanner."""

    cache_seconds: int = 30
    _cache: dict[str, _CacheEntry] = field(default_factory=dict)

    # ── cache ────────────────────────────────────────────────────────────────

    def _cached(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None or entry.expires_at < time.monotonic():
            return None
        return entry.value

    def _store(self, key: str, value: Any) -> None:
        self._cache[key] = _CacheEntry(value, time.monotonic() + self.cache_seconds)

    def clear_cache(self) -> None:
        self._cache.clear()

    # ── quotes ───────────────────────────────────────────────────────────────

    def quotes(self, roots: list[str] | None = None) -> list[MarketSnapshot]:
        """Current quotes for the given contract roots.

        Defaults to the four instruments Shani targets. Unknown roots raise
        rather than being skipped — silently dropping a symbol from a watchlist
        is how a trader ends up not seeing the thing they were watching.
        """
        wanted = roots or ["ES", "NQ", "CL", "GC"]
        for root in wanted:
            get_instrument(root)  # raises UnknownInstrumentError if unknown

        key = f"quotes:{','.join(sorted(wanted))}"
        if (hit := self._cached(key)) is not None:
            return list(hit)

        rows = self._scan(wanted)
        snapshots = [self._to_snapshot(root, rows.get(root, {})) for root in wanted]
        self._store(key, snapshots)
        return snapshots

    def _scan(self, roots: list[str]) -> dict[str, dict[str, Any]]:
        """Query the futures scanner. Isolated so it can be stubbed in tests."""
        try:
            from tradingview_screener import Query
        except ImportError as exc:  # pragma: no cover - dependency is required
            raise ScreenerUnavailableError(
                "tradingview-screener is not installed. Run `uv sync`."
            ) from exc

        tv_symbols = {INSTRUMENTS[r].tv_symbol: r for r in roots}
        try:
            # set_markets("futures") rather than the futures() helper — see the
            # module docstring and the pin comment in pyproject.toml.
            _, dataframe = (
                Query()
                .set_markets("futures")
                .select("name", "description", "close", "change", "high", "low", "volume")
                .limit(500)
                .get_scanner_data()
            )
        except Exception as exc:
            raise ScreenerUnavailableError(
                f"TradingView screener request failed: {exc}. This endpoint is "
                f"undocumented and rate-limited; if this persists, back off."
            ) from exc

        out: dict[str, dict[str, Any]] = {}
        for record in dataframe.to_dict("records"):
            ticker = str(record.get("ticker", ""))
            if (root := tv_symbols.get(ticker)) is not None:
                out[root] = record
        return out

    def _to_snapshot(self, root: str, row: dict[str, Any]) -> MarketSnapshot:
        instrument = INSTRUMENTS[root]
        return MarketSnapshot(
            symbol=root,
            tv_symbol=instrument.tv_symbol,
            name=instrument.name,
            last=_decimal(row.get("close")),
            change=_decimal(row.get("change_abs")),
            change_percent=_float(row.get("change")),
            high=_decimal(row.get("high")),
            low=_decimal(row.get("low")),
            volume=_int(row.get("volume")),
            as_of=time.time(),
        )

    # ── technical ratings ────────────────────────────────────────────────────

    def analysis(self, root: str, interval: str = "15m") -> dict[str, Any]:
        """Technical summary for one instrument at one timeframe.

        Returned as plain data for the agent to reason over, deliberately not as
        a recommendation. TradingView's oscillator/moving-average consensus is
        an input, not a signal, and presenting it as the latter would be exactly
        the kind of false authority this project is meant to replace with the
        trader's own measured history.
        """
        key = f"analysis:{root}:{interval}"
        if (hit := self._cached(key)) is not None:
            return dict(hit)

        instrument = get_instrument(root)
        try:
            from tradingview_ta import Interval, TA_Handler
        except ImportError as exc:  # pragma: no cover
            raise ScreenerUnavailableError("tradingview-ta is not installed.") from exc

        intervals = {
            "1m": Interval.INTERVAL_1_MINUTE, "5m": Interval.INTERVAL_5_MINUTES,
            "15m": Interval.INTERVAL_15_MINUTES, "1h": Interval.INTERVAL_1_HOUR,
            "4h": Interval.INTERVAL_4_HOURS, "1d": Interval.INTERVAL_1_DAY,
        }
        if interval not in intervals:
            raise ValueError(f"Unsupported interval {interval!r}. Use one of {list(intervals)}.")

        try:
            handler = TA_Handler(
                symbol=f"{instrument.root}1!",
                exchange=instrument.exchange,
                screener="futures",
                interval=intervals[interval],
            )
            analysis = handler.get_analysis()
        except Exception as exc:
            raise ScreenerUnavailableError(
                f"Technical analysis for {root} at {interval} failed: {exc}"
            ) from exc

        result = {
            "symbol": root,
            "interval": interval,
            "summary": analysis.summary,
            "oscillators": analysis.oscillators.get("COMPUTE", {}),
            "moving_averages": analysis.moving_averages.get("COMPUTE", {}),
            "indicators": {
                k: v for k, v in analysis.indicators.items()
                if k in {"RSI", "MACD.macd", "MACD.signal", "ATR", "EMA20", "EMA50",
                         "SMA200", "close", "volume"}
            },
        }
        self._store(key, result)
        return result

    def multi_timeframe(self, root: str) -> dict[str, Any]:
        """Ratings across several timeframes at once.

        Timeframe agreement or disagreement is context a trader actually uses,
        and asking for it in one call keeps the agent from making six.
        """
        out: dict[str, Any] = {"symbol": root, "timeframes": {}}
        for interval in ("15m", "1h", "4h", "1d"):
            try:
                out["timeframes"][interval] = self.analysis(root, interval)["summary"]
            except ScreenerUnavailableError as exc:
                out["timeframes"][interval] = {"error": str(exc)}
        return out


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
