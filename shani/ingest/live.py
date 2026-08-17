"""Real-time fill capture — the thing that closes the learning loop.

`shani import` reads the trade history whenever you ask it to. That is enough to
compute statistics, and not enough to learn from, because the part worth learning
from decays. An hour after a trade the answer to "what did you see?" is a
reconstruction; the chart has moved, and so has the memory. The journal is only
as good as how fresh the answer is — :attr:`InterviewAnswer.latency_seconds`
exists precisely so the extraction step can discount a late one.

So this module watches for fills as they happen and, the moment one lands,
captures what the trader was looking at and asks while it is still true.

## Why a fill re-runs the whole import

The obvious design is to keep a running position here and emit a trade when it
goes flat. That means two implementations of the pairing — one for the batch
import and one for the live path — and the day they disagree, the trade table
contains both answers and neither is marked wrong.

Instead a fill is treated purely as a *trigger*. The full history is re-read and
re-paired through exactly the code :mod:`shani.ingest.tradingview` uses, so live
capture and `shani import` cannot produce different trades: they are the same
function of the same data. Re-reading costs one CDP call for an account with
fifty-odd fills, which is nothing next to a class of bug that silently corrupts
the corpus.

The screenshot is the one thing the batch path genuinely cannot reconstruct, and
it is why this runs at all.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from shani.agent.reasoning import Agent
from shani.audit import AuditLog, EventType
from shani.db import Database
from shani.ingest.tradingview import build_trades, save_trades
from shani.market.tradingview_cdp import (
    TradingViewDesktop,
    TradingViewExecution,
    TradingViewUnavailableError,
)
from shani.models import Trade
from shani.notify import Notifier

__all__ = ["LiveCapture", "watch"]

log = logging.getLogger(__name__)

#: How far a trade's entry may sit from a fill's timestamp and still be taken as
#: the trade that fill opened. Both come from the same TradingView clock, so this
#: only absorbs the rounding between an execution's `time` and the entry time
#: derived from it.
ENTRY_MATCH_WINDOW = timedelta(seconds=5)

#: How long to wait before reconnecting after the stream drops. TradingView being
#: closed for the evening is the common case, and retrying in a tight loop would
#: burn a core all night for nothing.
RECONNECT_SECONDS = 15.0


@dataclass(slots=True)
class LiveCapture:
    """Turns fills into journalled trades with the chart attached.

    ``interview`` and ``screenshots`` are separable because they fail
    differently: a screenshot needs TradingView to still be on screen, while the
    interview only needs the trade. Losing one must not cost the other.
    """

    db: Database
    desktop: TradingViewDesktop
    audit: AuditLog
    agent: Agent | None = None
    notifier: Notifier | None = None
    screenshot_dir: Path | None = None
    capture_screenshots: bool = True

    #: Trades already closed when the watch began. They are somebody else's
    #: history, not something that just happened, and must not trigger a
    #: notification asking about a trade from three weeks ago.
    _known_closed: set[str] = field(default_factory=set)
    #: Screenshot taken at each fill, keyed by the fill's timestamp, waiting for
    #: the re-import to reveal which trade it belongs to.
    _pending_shots: dict[datetime, Path] = field(default_factory=dict)

    async def prime(self) -> None:
        """Import what has already happened, and mark all of it as history.

        Reading the database alone is not enough, and getting this wrong is
        loud. On a fresh database nothing is closed, so the first fill of the
        session re-reads the account, finds every historical round trip missing
        from the "already seen" set, and announces all of them as having just
        closed — a burst of interviews and desktop notifications about trades
        from weeks ago, on the very first run.

        So priming imports the venue's history first and takes the result as the
        starting line. Whatever had already happened when the watch began is
        history, whether or not this database had heard of it.
        """
        await self._reconcile()
        self._known_closed = {
            str(t.id) for t in self.db.trades.all() if not t.is_open
        }

    async def on_fill(self, fill: TradingViewExecution) -> list[Trade]:
        """Handle one fill. Returns the trades that closed because of it."""
        self.audit.record(
            EventType.FILL_OBSERVED,
            f"{fill.symbol} {'bought' if fill.is_buy else 'sold'} "
            f"{fill.quantity} at {fill.price}",
            payload={
                "execution_id": fill.id,
                "symbol": fill.symbol,
                "price": str(fill.price),
                "quantity": fill.quantity,
                "side": "buy" if fill.is_buy else "sell",
            },
        )

        # First, before anything slow. The chart is what the trader was looking
        # at *now*, and every second spent elsewhere is a second of drift.
        shot = await self._capture(fill) if self.capture_screenshots else None
        if shot is not None:
            self._pending_shots[fill.time] = shot

        closed = await self._reconcile()

        for trade in closed:
            if self.agent is not None:
                self.agent.start_interview(trade)
            if self.notifier is not None:
                await self.notifier.trade_closed(trade)
            self.audit.record(
                EventType.TRADE_CLOSED,
                f"{trade.symbol} closed for {trade.net_pnl:+,.2f}",
                trade_id=trade.id,
            )
        return closed

    async def _capture(self, fill: TradingViewExecution) -> Path | None:
        """Screenshot the chart. Never fatal — a lost image is not a lost trade."""
        if self.screenshot_dir is None:
            return None
        try:
            image = await self.desktop.screenshot()
        except TradingViewUnavailableError as exc:
            log.warning("Screenshot failed for fill %s: %s", fill.id, exc)
            return None

        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshot_dir / f"fill-{fill.id}-{fill.time:%Y%m%dT%H%M%S}.png"
        path.write_bytes(image)
        return path

    async def _reconcile(self) -> list[Trade]:
        """Re-read the account, re-pair it, and report what newly closed."""
        report = build_trades(
            await self.desktop.executions(),
            await self.desktop.order_history(),
            account=await self.desktop.account_id(),
        )

        # Attach each pending screenshot to the trade whose entry it captured.
        for trade in report.trades:
            if trade.entry_screenshot:
                continue
            for taken_at, path in self._pending_shots.items():
                if abs(trade.entry_at - taken_at) <= ENTRY_MATCH_WINDOW:
                    trade.entry_screenshot = str(path)
                    break

        # Chart context, for the trade this fill just opened.
        try:
            state = await self.desktop.chart_state()
        except TradingViewUnavailableError:
            state = None
        if state is not None:
            for trade in report.trades:
                if trade.chart_timeframe is None and any(
                    abs(trade.entry_at - t) <= ENTRY_MATCH_WINDOW
                    for t in self._pending_shots
                ):
                    trade.chart_timeframe = state.timeframe
                    trade.chart_studies = list(state.studies)

        save_trades(self.db, report)

        closed = []
        for trade in report.trades:
            if trade.is_open or str(trade.id) in self._known_closed:
                continue
            self._known_closed.add(str(trade.id))
            # Re-read rather than trusting the in-memory copy: save_trades merges
            # against what is already stored, so the persisted row is the truth.
            closed.append(self.db.trades.get(trade.id) or trade)

        # A screenshot whose trade has closed has been claimed or missed; either
        # way it is no longer pending, and the dict must not grow all session.
        if closed:
            self._pending_shots = {
                t: p
                for t, p in self._pending_shots.items()
                if not any(abs(c.entry_at - t) <= ENTRY_MATCH_WINDOW for c in closed)
            }
        return closed


async def watch(
    capture: LiveCapture,
    *,
    reconnect: bool = True,
    on_event: Callable[[TradingViewExecution, list[Trade]], None] | None = None,
) -> None:
    """Watch for fills until cancelled, reconnecting when TradingView goes away.

    TradingView being closed is normal — the trader shuts it at the end of the
    session — so a dropped stream is not an error worth dying on. It reconnects
    quietly and says so once, rather than either crashing or filling the log with
    one line per retry.
    """
    await capture.prime()
    announced_down = False

    while True:
        try:
            async for fill in capture.desktop.watch_executions():
                if announced_down:
                    log.info("Reconnected to TradingView.")
                    announced_down = False
                closed = await capture.on_fill(fill)
                if on_event is not None:
                    on_event(fill, closed)
        except asyncio.CancelledError:
            raise
        except TradingViewUnavailableError as exc:
            if not reconnect:
                raise
            if not announced_down:
                log.warning("TradingView unreachable (%s). Retrying quietly.", exc)
                announced_down = True
            await asyncio.sleep(RECONNECT_SECONDS)
