"""Real-time fill capture.

The failure this guards against is not a crash. A fill arrives, the handler runs
twice, and the trade table gains a second copy of a round trip that only
happened once — after which every statistic computed from it is wrong and
nothing in the logs says so.

Two independent things have to hold:

* The **page side** must not subscribe twice. TradingView's delegate calls every
  registered listener, so a second subscription reports each fill twice at
  source. That is asserted against the injected JavaScript in
  ``tests/test_integration.py``.
* The **database side** must land a re-processed fill on the same row. That is
  what these tests cover, by feeding the same history through twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from shani.audit import AuditLog
from shani.db import Database
from shani.ingest.live import ENTRY_MATCH_WINDOW, LiveCapture
from shani.market.tradingview_cdp import TradingViewExecution, TradingViewOrder

BASE = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)


def ex(offset: int, side: int, qty: int, price: str, ident: str) -> TradingViewExecution:
    return TradingViewExecution(
        id=ident,
        symbol="CME_MINI:MESU2026",
        side=side,
        quantity=qty,
        price=Decimal(price),
        time=BASE + timedelta(seconds=offset),
    )


class FakeDesktop:
    """A TradingView that returns whatever history the test has set."""

    def __init__(self) -> None:
        self.fills: list[TradingViewExecution] = []
        self.orders: list[TradingViewOrder] = []
        self.screenshots_taken = 0
        self.chart_reads = 0

    async def executions(self) -> list[TradingViewExecution]:
        return list(self.fills)

    async def order_history(self) -> list[TradingViewOrder]:
        return list(self.orders)

    async def account_id(self) -> str:
        return "34862113"

    async def screenshot(self) -> bytes:
        self.screenshots_taken += 1
        # A one-pixel PNG. Enough to prove bytes reached the disk.
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDAT"
            b"x\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    async def chart_state(self) -> Any:
        self.chart_reads += 1

        class State:
            symbol = "CME_MINI_DL:MESU2026"
            timeframe = "3m"
            studies = ("VWAP", "EMA 20")

        return State()


@pytest.fixture
def setup(tmp_path: Path) -> tuple[LiveCapture, FakeDesktop, Database]:
    db = Database(tmp_path / "live.db")
    desktop = FakeDesktop()
    capture = LiveCapture(
        db=db,
        desktop=desktop,  # type: ignore[arg-type]
        audit=AuditLog(db),
        screenshot_dir=tmp_path / "shots",
    )
    # Not primed here: the account is empty at this point, and each test sets
    # the history it wants. Tests that care about priming do it explicitly.
    return capture, desktop, db


@pytest.mark.asyncio
async def test_a_closing_fill_produces_one_trade(setup: Any) -> None:
    capture, desktop, db = setup
    entry, exit_ = ex(0, 1, 10, "7794.75", "e1"), ex(600, -1, 10, "7801.25", "e2")

    desktop.fills = [entry]
    assert await capture.on_fill(entry) == []          # opening a position closes nothing
    assert db.trades.count() == 1                       # but it is journalled as open
    assert db.trades.all()[0].is_open

    desktop.fills = [entry, exit_]
    closed = await capture.on_fill(exit_)
    assert len(closed) == 1
    assert db.trades.count() == 1, "the round trip was recorded twice"
    assert not closed[0].is_open
    assert closed[0].net_pnl == Decimal("325.00")       # 6.5pt * 10 * $5
    db.close()


@pytest.mark.asyncio
async def test_the_same_fill_delivered_twice_does_not_double_the_trade(setup: Any) -> None:
    """The core idempotency guarantee, from the database side.

    A reconnect can replay a fill, and TradingView can deliver one twice. Neither
    may produce a second round trip.
    """
    capture, desktop, db = setup
    entry, exit_ = ex(0, 1, 10, "7794.75", "e1"), ex(600, -1, 10, "7801.25", "e2")
    desktop.fills = [entry, exit_]

    first = await capture.on_fill(exit_)
    second = await capture.on_fill(exit_)

    assert len(first) == 1
    assert second == [], "the same close was reported as newly closed twice"
    assert db.trades.count() == 1
    db.close()


@pytest.mark.asyncio
async def test_priming_on_a_fresh_database_does_not_replay_all_of_history(
    tmp_path: Path,
) -> None:
    """The bug that a live run found and no unit test had.

    Priming from the database alone is not enough. On a *fresh* database nothing
    is closed, so the first fill re-reads the account, finds every historical
    round trip absent from the seen-set, and reports all of them as having just
    closed — an interview and a desktop notification for each, about trades from
    weeks ago, on the very first run. Observed against the real account: 26
    trades announced at once.

    Priming must therefore import the venue's history and take that as the
    starting line.
    """
    db = Database(tmp_path / "live.db")
    desktop = FakeDesktop()
    history = [
        ex(0, 1, 10, "7794.75", "e1"), ex(600, -1, 10, "7801.25", "e2"),
        ex(1200, -1, 10, "7805", "e3"), ex(1800, 1, 10, "7800", "e4"),
    ]
    desktop.fills = list(history)

    capture = LiveCapture(db=db, desktop=desktop, audit=AuditLog(db))  # type: ignore[arg-type]
    await capture.prime()

    assert db.trades.count() == 2, "priming should have imported the history"

    # A new fill arrives. Only what it closes may be announced.
    new_entry = ex(3600, 1, 10, "7810", "e5")
    desktop.fills = [*history, new_entry]
    assert await capture.on_fill(new_entry) == [], (
        "historical trades were announced as newly closed"
    )

    new_exit = ex(4200, -1, 10, "7815", "e6")
    desktop.fills = [*history, new_entry, new_exit]
    closed = await capture.on_fill(new_exit)
    assert len(closed) == 1, "the genuinely new round trip was not announced"
    assert closed[0].net_pnl == Decimal("250.00")
    db.close()


@pytest.mark.asyncio
async def test_the_chart_is_captured_at_the_fill(setup: Any) -> None:
    """The screenshot is the only thing a later import cannot reconstruct."""
    capture, desktop, db = setup
    entry = ex(0, 1, 10, "7794.75", "e1")
    desktop.fills = [entry]
    await capture.on_fill(entry)

    trade = db.trades.all()[0]
    assert desktop.screenshots_taken == 1
    assert trade.entry_screenshot is not None
    saved = Path(trade.entry_screenshot)
    assert saved.exists() and saved.stat().st_size > 0
    assert trade.chart_timeframe == "3m"
    assert trade.chart_studies == ["VWAP", "EMA 20"]
    db.close()


@pytest.mark.asyncio
async def test_a_failed_screenshot_does_not_lose_the_trade(setup: Any) -> None:
    """A screenshot needs TradingView on screen; the trade does not.

    These fail independently and must not be coupled — losing the picture is a
    pity, losing the trade is a corrupted journal.
    """
    from shani.market.tradingview_cdp import TradingViewUnavailableError

    capture, desktop, db = setup

    async def broken() -> bytes:
        raise TradingViewUnavailableError("window is minimised")

    desktop.screenshot = broken  # type: ignore[method-assign]
    entry, exit_ = ex(0, 1, 10, "7794.75", "e1"), ex(600, -1, 10, "7801.25", "e2")
    desktop.fills = [entry, exit_]

    closed = await capture.on_fill(exit_)
    assert len(closed) == 1
    assert closed[0].net_pnl == Decimal("325.00")
    assert closed[0].entry_screenshot is None
    db.close()


@pytest.mark.asyncio
async def test_live_and_import_agree_on_the_trade(setup: Any) -> None:
    """Live capture and `shani import` must not produce different rows.

    They share the pairing deliberately; this pins that they also share the
    identity, so importing after a watch session updates rather than inserts.
    """
    from shani.ingest.tradingview import build_trades

    capture, desktop, db = setup
    entry, exit_ = ex(0, 1, 10, "7794.75", "e1"), ex(600, -1, 10, "7801.25", "e2")
    desktop.fills = [entry, exit_]
    live = (await capture.on_fill(exit_))[0]

    imported = build_trades([entry, exit_], account="34862113").trades[0]
    assert imported.id == live.id
    assert imported.external_id == live.external_id
    db.close()


@pytest.mark.asyncio
async def test_a_fill_is_recorded_in_the_audit_log(setup: Any) -> None:
    capture, desktop, db = setup
    entry = ex(0, 1, 10, "7794.75", "e1")
    desktop.fills = [entry]
    await capture.on_fill(entry)

    kinds = [e.event_type for e in db.audit.all()]
    assert "fill.observed" in kinds
    db.close()


def test_entry_match_window_is_tight() -> None:
    """Both timestamps come from TradingView's own clock.

    A loose window would attach a screenshot to the wrong trade during a fast
    sequence of scalps, which is worse than attaching none.
    """
    assert ENTRY_MATCH_WINDOW.total_seconds() <= 10
