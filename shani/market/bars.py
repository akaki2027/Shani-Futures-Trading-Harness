"""OHLCV bars for charting.

Neither of the other market sources can draw a price chart for an arbitrary
symbol, which is why this module exists:

- **Plane A** (``screener.py``) returns a *snapshot* — last, change, high, low.
  One row per instrument, no history. Fine for a watchlist, useless for a chart.
- **Plane B** (``tradingview_cdp.py``) returns the bars of whatever chart the
  trader currently has open. Charting a different symbol would mean *changing
  their chart*, which is unacceptable: the portal must never reach over and move
  the chart someone is trading from.

So bars come from Yahoo Finance's public chart endpoint, which serves continuous
futures under its own ``=F`` symbology and needs no key. The approach — and the
endpoint — follow ``atilaahmettaner/tradingview-mcp``, which uses the same
source for its backtester.

**These bars are for looking at, not for settling P&L.** They are delayed,
occasionally gappy, and Yahoo's continuous contract is not identical to the
dated contract you trade. Every dollar figure in Shani comes from fills and
:mod:`shani.instruments`, never from here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from shani.instruments import get_instrument

__all__ = ["Bar", "BarsProvider", "BarsUnavailableError"]

_YAHOO: Final = "https://query1.finance.yahoo.com/v8/finance/chart"

# Yahoo lists continuous futures with a `=F` suffix. Micros are deliberately
# mapped to their parent: the price series is the same, only the multiplier
# differs, and the parent's series is far better populated.
YAHOO_SYMBOLS: Final[dict[str, str]] = {
    "ES": "ES=F", "MES": "ES=F",
    "NQ": "NQ=F", "MNQ": "NQ=F",
    "RTY": "RTY=F", "M2K": "RTY=F",
    "YM": "YM=F", "MYM": "YM=F",
    "CL": "CL=F", "MCL": "CL=F",
    "NG": "NG=F",
    "GC": "GC=F", "MGC": "GC=F",
    "SI": "SI=F",
}

#: Chart timeframe → (Yahoo interval, Yahoo range). Ranges are chosen to give a
#: readable number of bars: enough context to see structure, not so many that
#: the chart becomes a smear.
INTERVALS: Final[dict[str, tuple[str, str]]] = {
    "5m": ("5m", "5d"),
    "15m": ("15m", "1mo"),
    "1h": ("1h", "3mo"),
    "4h": ("1h", "6mo"),
    "1d": ("1d", "2y"),
}


class BarsUnavailableError(RuntimeError):
    """Bars could not be fetched. Raised rather than returning an empty list,
    because "no data" and "market is quiet" must not look the same."""


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV candle. ``time`` is a UNIX timestamp in seconds, UTC."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class BarsProvider:
    """Fetches OHLCV candles for charting, with a short TTL cache."""

    cache_seconds: int = 60
    _cache: dict[str, tuple[float, list[Bar]]] | None = None

    def __post_init__(self) -> None:
        if self._cache is None:
            self._cache = {}

    def bars(self, symbol: str, interval: str = "15m") -> list[Bar]:
        """Candles for a contract root at a chart timeframe."""
        instrument = get_instrument(symbol)
        if interval not in INTERVALS:
            raise ValueError(
                f"Unsupported interval {interval!r}. Use one of {list(INTERVALS)}."
            )
        yahoo = YAHOO_SYMBOLS.get(instrument.root)
        if yahoo is None:
            raise BarsUnavailableError(
                f"No chart data source mapped for {instrument.root}. Add it to "
                f"YAHOO_SYMBOLS in shani/market/bars.py."
            )

        key = f"{instrument.root}:{interval}"
        assert self._cache is not None
        hit = self._cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]

        yf_interval, yf_range = INTERVALS[interval]
        try:
            response = httpx.get(
                f"{_YAHOO}/{yahoo}",
                params={"interval": yf_interval, "range": yf_range},
                # Yahoo returns 429 to requests without a browser-ish agent.
                headers={"User-Agent": "shani/0.1 (charting)"},
                timeout=15.0,
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise BarsUnavailableError(
                f"Could not fetch {interval} bars for {instrument.root}: {exc}"
            ) from exc

        bars = _parse(payload)
        if interval == "4h":
            # Yahoo has no 4h interval, so build it from hourly candles rather
            # than silently serving a different timeframe than was requested.
            bars = _resample(bars, 4)
        self._cache[key] = (time.monotonic() + self.cache_seconds, bars)
        return bars

    def clear_cache(self) -> None:
        assert self._cache is not None
        self._cache.clear()


def _parse(payload: dict[str, Any]) -> list[Bar]:
    try:
        result = payload["chart"]["result"][0]
        stamps: list[int] = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        error = (payload.get("chart") or {}).get("error")
        raise BarsUnavailableError(
            f"Unexpected response shape from the chart endpoint: {error or exc}"
        ) from exc

    opens, highs = quote.get("open", []), quote.get("high", [])
    lows, closes = quote.get("low", []), quote.get("close", [])
    volumes = quote.get("volume", []) or [0] * len(stamps)

    bars: list[Bar] = []
    for i, stamp in enumerate(stamps):
        o, h, low, c = opens[i], highs[i], lows[i], closes[i]
        # Yahoo emits nulls for halted or missing periods. Dropping them is
        # correct — a candle with a null close cannot be drawn, and
        # interpolating would invent price action that never happened.
        if None in (o, h, low, c):
            continue
        bars.append(
            Bar(time=int(stamp), open=float(o), high=float(h), low=float(low),
                close=float(c), volume=float(volumes[i] or 0))
        )
    return bars


def _resample(bars: list[Bar], factor: int) -> list[Bar]:
    """Aggregate consecutive candles into larger ones."""
    out: list[Bar] = []
    for i in range(0, len(bars), factor):
        chunk = bars[i : i + factor]
        if not chunk:
            continue
        out.append(
            Bar(
                time=chunk[0].time,
                open=chunk[0].open,
                high=max(b.high for b in chunk),
                low=min(b.low for b in chunk),
                close=chunk[-1].close,
                volume=sum(b.volume for b in chunk),
            )
        )
    return out
