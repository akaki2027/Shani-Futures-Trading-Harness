"""Plane B — the bridge to your TradingView Desktop application.

This is the plane that makes Shani different from a trade log. Everything else
knows what the market did; only this knows **what you were looking at** — your
symbol, your timeframe, your indicators, the actual pixels on your screen when
you decided to click buy. That context is what the journal has always thrown
away and what the learning loop needs most.

## How it works

TradingView Desktop is an Electron application, so it embeds Chromium and
therefore speaks the Chrome DevTools Protocol when launched with the standard
``--remote-debugging-port`` flag. We open a WebSocket to that port and evaluate
JavaScript in the page, reading TradingView's own internal chart widget.

    Shani ──HTTP──▶ localhost:9222/json/list      (find the page)
          ──WS────▶ Runtime.evaluate              (read the chart)

Nothing is scraped, no paywall is bypassed, and no data leaves the machine. It
talks to an instance you installed, logged into, and paid for, using a debug
flag you enabled yourself.

## The one file rule

**Every piece of knowledge about TradingView's internals lives in this module.**
The JS expressions, the ``window.tvWidget`` entry point, the Monaco language id
that identifies the Pine editor — all of it, here, and nowhere else. TradingView's
internal API is undocumented and *will* change between Desktop releases. When it
does, this is the only file that needs fixing.

That discipline, and the specific internals to target, are taken from
``pueschel88/Tradingview-MCP`` (MIT). The implementation below is an independent
Python port rather than a copy of its TypeScript; the valuable part was knowing
where to look. See ``NOTICE.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

__all__ = [
    "ChartState",
    "PineDiagnostic",
    "TradingViewDesktop",
    "TradingViewUnavailableError",
]

#: Shani timeframe → TradingView resolution string.
TIMEFRAME_TO_TV: dict[str, str] = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240",
    "1d": "D", "1w": "W", "1M": "M",
}
TV_TO_TIMEFRAME: dict[str, str] = {v: k for k, v in TIMEFRAME_TO_TV.items()}


class TradingViewUnavailableError(RuntimeError):
    """TradingView Desktop could not be reached or the chart could not be read.

    The message is deliberately instructional. This failure has exactly one
    common cause — the app was started without the debug flag — and a bare
    ``ConnectionRefused`` sends people hunting in the wrong place.
    """


@dataclass(frozen=True, slots=True)
class ChartState:
    """What the trader's chart is showing right now."""

    symbol: str
    timeframe: str
    studies: tuple[str, ...]
    last_price: Decimal | None

    @property
    def study_list(self) -> str:
        return ", ".join(self.studies) if self.studies else "none"


@dataclass(frozen=True, slots=True)
class PineDiagnostic:
    severity: str
    line: int | None
    column: int | None
    message: str


# ─── JavaScript evaluated in the TradingView page ────────────────────────────
#
# Everything TradingView-specific is in this block. Each expression is an IIFE
# returning either a result object or ``{error: "..."}`` — never throwing, since
# an exception across the CDP boundary loses its message.

# Resolving the chart widget is the one thing every expression needs, and the
# correct global depends on which TradingView surface you are attached to.
#
# Verified against TradingView Desktop 3.3.0 (Microsoft Store build) on
# tradingview.com's own chart page:
#
#   window.TradingViewApi   — the real application. What Desktop and the website
#                             both use. Checked FIRST.
#   window.tvWidget         — the embeddable Charting Library global. Present
#                             only in widget embeds, NOT on tradingview.com.
#
# Earlier versions of this file checked only `tvWidget`, which fails on every
# real Desktop install with "tvWidget not available". If a future release moves
# things again, this resolver is the first place to look.
_JS_WIDGET = """
    (window.TradingViewApi
      || window.tvWidget
      || (window.TradingView && window.TradingView.widget))
"""

# Series accessors differ between surfaces, so last price is best-effort across
# several shapes rather than assuming one. A missing price must not fail the
# whole read — symbol, timeframe, and studies are the valuable part.
_JS_LAST_PRICE = """
  (() => {
    try {
      const s = chart.getSeries();
      if (!s) return null;
      if (typeof s.lastPrice === 'function') return s.lastPrice();
      if (typeof s.lastValue === 'function') return s.lastValue();
      if (typeof s.data === 'function') {
        const d = s.data();
        const bars = (d && typeof d.bars === 'function') ? d.bars() : d;
        if (Array.isArray(bars) && bars.length) {
          const b = bars[bars.length - 1];
          return b.close != null ? b.close : (Array.isArray(b.value) ? b.value[4] : null);
        }
      }
      if (typeof s.priceScale === 'function') {
        const ps = s.priceScale();
        if (ps && typeof ps.lastPrice === 'function') return ps.lastPrice();
      }
    } catch (e) {}
    return null;
  })()
"""

_JS_CHART_STATE = """
(() => {
  const w = %s;
  if (!w || !w.activeChart) {
    return { error: 'No TradingView chart API on this page - is a chart tab open and loaded?' };
  }
  const chart = w.activeChart();
  let studies = [];
  try { studies = (chart.getAllStudies() || []).map(s => s.name); } catch (e) {}
  return {
    symbol: chart.symbol(),
    resolution: String(chart.resolution()),
    studies: studies,
    lastPrice: %s,
  };
})()
""" % (_JS_WIDGET, _JS_LAST_PRICE)

_JS_SET_SYMBOL = ("""
(() => {
  const w = %s;
  if (!w || !w.activeChart) return { error: 'No TradingView chart API on this page' };
  const target = %%s;
  return new Promise((resolve) => {
    const done = () => resolve({ symbol: w.activeChart().symbol() });
    try {
      // The application exposes changeSymbol at the top level; the Charting
      // Library only has setSymbol on the chart. Prefer whichever exists.
      if (typeof w.changeSymbol === 'function') { w.changeSymbol(target); setTimeout(done, 400); }
      else { w.activeChart().setSymbol(target, done); }
    } catch (e) { resolve({ error: String(e && e.message || e) }); }
  });
})()
""" % _JS_WIDGET)

_JS_SET_RESOLUTION = ("""
(() => {
  const w = %s;
  if (!w || !w.activeChart) return { error: 'No TradingView chart API on this page' };
  w.activeChart().setResolution(%%s);
  return { resolution: String(w.activeChart().resolution()) };
})()
""" % _JS_WIDGET)

_JS_OHLCV = ("""
(() => {
  const w = %s;
  if (!w || !w.activeChart) return { error: 'No TradingView chart API on this page' };
  let series;
  try { series = w.activeChart().getSeries(); } catch (e) { return { error: 'no series' }; }
  if (!series || typeof series.data !== 'function') return { error: 'series data unavailable' };
  const d = series.data();
  // Some surfaces return a bar-store object rather than a plain array.
  const raw = (d && typeof d.bars === 'function') ? d.bars() : d;
  if (!Array.isArray(raw)) return { error: 'series data is not enumerable' };
  const bars = raw.slice(-%%d).map(b => {
    const v = Array.isArray(b.value) ? b.value : null;
    return {
      time: new Date((v ? v[0] : b.time) * (String(v ? v[0] : b.time).length <= 10 ? 1000 : 1)).toISOString(),
      open:   v ? v[1] : b.open,
      high:   v ? v[2] : b.high,
      low:    v ? v[3] : b.low,
      close:  v ? v[4] : b.close,
      volume: (v ? v[5] : b.volume) || 0,
    };
  });
  return { bars: bars };
})()
""" % _JS_WIDGET)

#: The Pine editor is the Monaco instance whose language id is ``pinescript``.
#: Falls back to the first editor, since the language id is not always set.
_JS_FIND_PINE_EDITOR = """
(() => {
  const m = window.monaco;
  if (!m || !m.editor || !m.editor.getEditors) return null;
  const editors = m.editor.getEditors();
  if (!editors.length) return null;
  return editors.find(e => {
    try { return e.getModel().getLanguageId() === 'pinescript'; } catch (x) { return false; }
  }) || editors[0];
})()
"""

_JS_PINE_GET = """
(() => {
  const ed = %s;
  if (!ed) return { error: 'Pine Editor not found - is it open?' };
  return { code: ed.getValue() };
})()
""" % _JS_FIND_PINE_EDITOR

_JS_PINE_SET = """
(() => {
  const ed = %s;
  if (!ed) return { error: 'Pine Editor not found - is it open?' };
  ed.setValue(%%s);
  return { ok: true };
})()
""" % _JS_FIND_PINE_EDITOR

_JS_PINE_COMPILE = """
(async () => {
  const ed = %s;
  if (!ed) return { error: 'Pine Editor not found - is it open?' };
  try { ed.getAction('editor.action.save').run(); } catch (e) {}
  await new Promise(r => setTimeout(r, 800));
  const model = ed.getModel();
  const markers = model
    ? window.monaco.editor.getModelMarkers({ resource: model.uri })
    : [];
  const diagnostics = markers.map(m => ({
    severity: m.severity === 8 ? 'error' : (m.severity === 4 ? 'warning' : 'info'),
    line: m.startLineNumber || null,
    column: m.startColumn || null,
    message: m.message || '',
  }));
  return { ok: !diagnostics.some(d => d.severity === 'error'), diagnostics: diagnostics };
})()
""" % _JS_FIND_PINE_EDITOR


class TradingViewDesktop:
    """Client for a locally running TradingView Desktop instance."""

    def __init__(
        self, host: str = "localhost", port: int = 9222, timeout: float = 10.0
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._ws_url: str | None = None
        self._message_id = 0

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ── target discovery ─────────────────────────────────────────────────────

    async def _find_target(self) -> str:
        """Locate the TradingView page's WebSocket debugger URL."""
        if self._ws_url is not None:
            return self._ws_url
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{self.endpoint}/json/list")
                response.raise_for_status()
                targets = response.json()
        except Exception as exc:
            raise TradingViewUnavailableError(
                f"Cannot reach the TradingView debug port at {self.endpoint}.\n\n"
                f"TradingView Desktop must be running and started with the debug flag:\n"
                f"  Windows: & \"$env:LOCALAPPDATA\\Programs\\TradingView\\TradingView.exe\" "
                f"--remote-debugging-port={self.port}\n"
                f"  macOS:   open -a TradingView --args --remote-debugging-port={self.port}\n"
                f"  Linux:   tradingview --remote-debugging-port={self.port}\n\n"
                f"Quit any running instance first, or the flag is ignored. "
                f"See docs/tradingview-setup.md.\n\nUnderlying error: {exc}"
            ) from exc

        pages = [
            t for t in targets
            if t.get("type") == "page" and "devtools://" not in t.get("url", "")
        ]
        if not pages:
            raise TradingViewUnavailableError(
                f"Debug port {self.endpoint} is reachable but exposes no page target. "
                f"Is this actually TradingView Desktop, and is a chart open?"
            )
        # TradingView Desktop exposes a dozen page targets — the chart, symbol
        # pages, the screener, and several internal `file://` shell windows for
        # the title bar, tooltips, and drag handling. Match the chart URL
        # specifically; a loose "chart" substring test can land on an internal
        # window and produce a baffling "no chart API" error.
        chart = next(
            (t for t in pages if "/chart/" in t.get("url", "").lower()),
            None,
        ) or next(
            (t for t in pages if "tradingview.com" in t.get("url", "").lower()),
            pages[0],
        )
        self._ws_url = str(chart["webSocketDebuggerUrl"])
        return self._ws_url

    # ── evaluation ───────────────────────────────────────────────────────────

    async def evaluate(self, expression: str) -> Any:
        """Evaluate JavaScript in the TradingView page and return the result."""
        import websockets

        ws_url = await self._find_target()
        self._message_id += 1
        payload = {
            "id": self._message_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        }

        try:
            async with websockets.connect(ws_url, open_timeout=self.timeout) as ws:
                await ws.send(json.dumps(payload))
                while True:
                    raw = await ws.recv()
                    message = json.loads(raw)
                    if message.get("id") == payload["id"]:
                        break
        except TradingViewUnavailableError:
            raise
        except Exception as exc:
            # A stale target URL is the usual cause; drop it so the next call
            # rediscovers rather than failing forever.
            self._ws_url = None
            raise TradingViewUnavailableError(
                f"CDP evaluation failed: {exc}. TradingView may have been "
                f"restarted or navigated."
            ) from exc

        if "error" in message:
            raise TradingViewUnavailableError(f"CDP error: {message['error']}")

        result = message.get("result", {})
        if result.get("exceptionDetails"):
            raise TradingViewUnavailableError(
                f"JavaScript exception in the TradingView page: "
                f"{result['exceptionDetails'].get('text', 'unknown')}"
            )

        value = result.get("result", {}).get("value")
        if isinstance(value, dict) and "error" in value:
            raise TradingViewUnavailableError(
                f"{value['error']} "
                f"(TradingView's internal API may have changed — see "
                f"shani/market/tradingview_cdp.py, the only file that needs fixing.)"
            )
        return value

    # ── chart ────────────────────────────────────────────────────────────────

    async def chart_state(self) -> ChartState:
        """Read the current chart: symbol, timeframe, indicators, last price.

        This is the call that captures *what the trader was looking at*.
        """
        raw = await self.evaluate(_JS_CHART_STATE)
        resolution = str(raw.get("resolution", ""))
        last = raw.get("lastPrice")
        return ChartState(
            symbol=str(raw.get("symbol", "")),
            # Unknown resolutions pass through rather than raising: a chart on
            # a timeframe we have no mapping for is still worth journaling.
            timeframe=TV_TO_TIMEFRAME.get(resolution, resolution),
            studies=tuple(raw.get("studies") or ()),
            last_price=Decimal(str(last)) if last is not None else None,
        )

    async def set_symbol(self, symbol: str) -> str:
        raw = await self.evaluate(_JS_SET_SYMBOL % json.dumps(symbol))
        return str(raw.get("symbol", symbol))

    async def set_timeframe(self, timeframe: str) -> str:
        if timeframe not in TIMEFRAME_TO_TV:
            raise ValueError(
                f"Unknown timeframe {timeframe!r}. Supported: {list(TIMEFRAME_TO_TV)}"
            )
        raw = await self.evaluate(_JS_SET_RESOLUTION % json.dumps(TIMEFRAME_TO_TV[timeframe]))
        return TV_TO_TIMEFRAME.get(str(raw.get("resolution", "")), timeframe)

    async def ohlcv(self, count: int = 200) -> list[dict[str, Any]]:
        """Recent bars from the active chart — the trader's own data, as shown."""
        raw = await self.evaluate(_JS_OHLCV % max(1, min(count, 5000)))
        return list(raw.get("bars") or [])

    # ── screenshots ──────────────────────────────────────────────────────────

    async def screenshot(self) -> bytes:
        """Capture the TradingView viewport as PNG bytes.

        Taken at trade entry, this is the single most valuable artefact in the
        journal: months later it is the only record of what the trader actually
        saw, and a vision-capable model can read it back.
        """
        import base64

        import websockets

        ws_url = await self._find_target()
        self._message_id += 1
        payload = {
            "id": self._message_id,
            "method": "Page.captureScreenshot",
            "params": {"format": "png"},
        }
        try:
            async with websockets.connect(ws_url, open_timeout=self.timeout) as ws:
                await ws.send(json.dumps(payload))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == payload["id"]:
                        break
        except Exception as exc:
            self._ws_url = None
            raise TradingViewUnavailableError(f"Screenshot failed: {exc}") from exc

        data = message.get("result", {}).get("data")
        if not data:
            raise TradingViewUnavailableError("Screenshot returned no image data.")
        return base64.b64decode(data)

    # ── Pine editor ──────────────────────────────────────────────────────────

    async def get_pine_source(self) -> str:
        raw = await self.evaluate(_JS_PINE_GET)
        return str(raw.get("code", ""))

    async def set_pine_source(self, code: str) -> None:
        """Replace the Pine editor contents.

        This is the write half of the closed loop: once a setup card has been
        learned from the journal, the agent can express it as Pine and push it
        into the trader's own TradingView, so the learned setup starts alerting.
        """
        await self.evaluate(_JS_PINE_SET % json.dumps(code))

    async def compile_pine(self) -> tuple[bool, list[PineDiagnostic]]:
        """Trigger a compile and read Monaco's diagnostics."""
        raw = await self.evaluate(_JS_PINE_COMPILE)
        diagnostics = [
            PineDiagnostic(
                severity=str(d.get("severity", "info")),
                line=d.get("line"),
                column=d.get("column"),
                message=str(d.get("message", "")),
            )
            for d in raw.get("diagnostics") or []
        ]
        return bool(raw.get("ok")), diagnostics

    # ── diagnostics ──────────────────────────────────────────────────────────

    async def check(self) -> tuple[bool, str]:
        """One-line health check for ``shani doctor``."""
        try:
            state = await self.chart_state()
        except TradingViewUnavailableError as exc:
            return False, str(exc).split("\n")[0]
        return True, f"connected — {state.symbol} on {state.timeframe}"
