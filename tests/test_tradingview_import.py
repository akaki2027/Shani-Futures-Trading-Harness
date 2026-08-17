"""Pairing TradingView fills into round trips.

The two failures worth guarding against here are not crashes.

**Pooling symbols.** The first implementation ran one position across every
symbol. Because the account held MES near 7,700, MNQ near 29,000 and SPY near
765, positions never closed against their own instrument, and it produced round
trips with a P&L of -$346,879 on an account that had made $4,722.78. It threw no
exception and every other test stayed green.

**Double counting.** An import re-reads the whole history, so importing twice
must leave 25 trades and not 50. That one is in ``tests/test_integration.py``,
where the seam tests live.

The reconciliation case at the bottom is a real slice of the owner's account,
kept verbatim, with the P&L TradingView itself reported for those fills.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shani.ingest.tradingview import (
    build_trades,
    pair_executions,
    realized_pnl,
    trade_uuid,
)
from shani.market.tradingview_cdp import TradingViewExecution, TradingViewOrder
from shani.models import Side

BASE = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)


def ex(
    offset: int,
    side: int,
    qty: int,
    price: str,
    symbol: str = "CME_MINI:MESU2026",
    ident: str | None = None,
) -> TradingViewExecution:
    return TradingViewExecution(
        id=ident or f"e{offset}",
        symbol=symbol,
        side=side,
        quantity=qty,
        price=Decimal(price),
        time=BASE + timedelta(seconds=offset),
    )


# ─── pairing ─────────────────────────────────────────────────────────────────


def test_simple_long_round_trip() -> None:
    trips = pair_executions([ex(0, 1, 10, "7764"), ex(60, -1, 10, "7772.25")])
    assert len(trips) == 1
    trip = trips[0]
    assert trip.side is Side.BUY
    assert trip.quantity == 10
    assert trip.entry_price == Decimal("7764")
    assert trip.exit_price == Decimal("7772.25")
    assert not trip.is_open


def test_short_round_trip_keeps_its_direction() -> None:
    """A short opens on a sell. Getting this backwards negates every short's P&L."""
    trips = pair_executions([ex(0, -1, 10, "7807"), ex(60, 1, 10, "7801.75")])
    assert trips[0].side is Side.SELL
    assert trips[0].entry_price == Decimal("7807")
    assert trips[0].exit_price == Decimal("7801.75")


def test_scale_in_averages_entry_by_size() -> None:
    """Two entries, 10 at 7700 and 30 at 7800, average to 7775 — not 7750."""
    trips = pair_executions(
        [ex(0, 1, 10, "7700"), ex(30, 1, 30, "7800"), ex(60, -1, 40, "7810")]
    )
    assert len(trips) == 1
    assert trips[0].quantity == 40
    assert trips[0].entry_price == Decimal("7775")


def test_scale_out_is_one_trade_not_two() -> None:
    trips = pair_executions(
        [ex(0, 1, 10, "7700"), ex(30, -1, 4, "7710"), ex(60, -1, 6, "7720")]
    )
    assert len(trips) == 1
    assert trips[0].quantity == 10
    assert trips[0].exit_price == Decimal("7716")  # (4*7710 + 6*7720) / 10


def test_reversal_splits_into_two_trades() -> None:
    """Long 10, then sell 30: the first 10 close the long, 20 open a short."""
    trips = pair_executions(
        [ex(0, 1, 10, "7700"), ex(30, -1, 30, "7750"), ex(60, 1, 20, "7740")]
    )
    assert len(trips) == 2
    assert trips[0].side is Side.BUY
    assert trips[0].quantity == 10
    assert trips[0].exit_price == Decimal("7750")
    assert trips[1].side is Side.SELL
    assert trips[1].quantity == 20
    assert trips[1].entry_price == Decimal("7750")


def test_position_still_open_is_reported_not_dropped() -> None:
    trips = pair_executions([ex(0, 1, 10, "7700"), ex(30, -1, 4, "7710")])
    assert len(trips) == 1
    assert trips[0].is_open
    assert trips[0].exit_at is None
    assert trips[0].quantity == 10


def test_symbols_are_paired_independently() -> None:
    """The regression that matters most.

    Interleaved MES and MNQ fills. Pooled into one position these close against
    each other and produce a ~29,000-point 'move'; paired per symbol they are two
    ordinary round trips.
    """
    fills = [
        ex(0, 1, 10, "7700"),
        ex(10, -1, 5, "29714.25", symbol="CME_MINI:MNQ1!"),
        ex(20, -1, 10, "7710"),
        ex(30, 1, 5, "29537", symbol="CME_MINI:MNQ1!"),
    ]
    trips = pair_executions(fills)
    assert len(trips) == 2
    by_symbol = {t.symbol: t for t in trips}
    assert by_symbol["CME_MINI:MESU2026"].quantity == 10
    assert by_symbol["CME_MINI:MESU2026"].exit_price == Decimal("7710")
    assert by_symbol["CME_MINI:MNQ1!"].side is Side.SELL
    assert by_symbol["CME_MINI:MNQ1!"].exit_price == Decimal("29537")
    # Both priced in their own instrument: MES 10pt*$5 + MNQ 177.25pt*5*$2.
    assert realized_pnl(trips) == Decimal("500") + Decimal("1772.50")


def test_out_of_order_fills_are_sorted_before_pairing() -> None:
    """TradingView returns newest-first; pairing is order-dependent."""
    trips = pair_executions([ex(60, -1, 10, "7772.25"), ex(0, 1, 10, "7764")])
    assert len(trips) == 1
    assert trips[0].side is Side.BUY
    assert trips[0].entry_price == Decimal("7764")


# ─── mapping to Trade ────────────────────────────────────────────────────────


def test_dated_contract_maps_to_root_and_keeps_the_contract() -> None:
    """MESU2026 → symbol MES, contract MESU2026.

    Statistics pool on the root; without the contract kept separately they are
    quietly wrong across every quarterly rollover.
    """
    report = build_trades(
        [ex(0, 1, 10, "7764"), ex(60, -1, 10, "7772.25")], account="34862113"
    )
    assert report.count == 1
    trade = report.trades[0]
    assert trade.symbol == "MES"
    assert trade.contract == "MESU2026"
    assert trade.net_pnl == Decimal("412.50")  # 8.25pt * 10 * $5


def test_continuous_symbol_has_no_contract() -> None:
    report = build_trades(
        [
            ex(0, 1, 5, "7730.5", symbol="CME_MINI:MES1!"),
            ex(60, -1, 5, "7773", symbol="CME_MINI:MES1!"),
        ]
    )
    assert report.trades[0].symbol == "MES"
    assert report.trades[0].contract is None


def test_unknown_instruments_are_skipped_and_reported() -> None:
    """Shani prices futures and refuses to guess a multiplier for anything else.

    An equity in a futures account must not be silently dropped, and must not
    be priced with an invented multiplier either.
    """
    report = build_trades(
        [
            ex(0, 1, 10, "7764"),
            ex(30, 1, 10, "764.89", symbol="AMEX:SPY"),
            ex(60, -1, 10, "7772.25"),
            ex(90, -1, 10, "765.42", symbol="AMEX:SPY"),
        ]
    )
    assert report.count == 1
    assert report.skipped == {"SPY": 1}
    assert report.skipped_count == 1


def test_paper_commission_is_absent_not_invented() -> None:
    """The venue reports no commission; Shani must not synthesise one.

    ``instruments.commission()`` would happily produce $10 here, and the trader's
    imported P&L would then disagree with the figure TradingView shows them.
    """
    report = build_trades([ex(0, 1, 10, "7764"), ex(60, -1, 10, "7772.25")])
    assert report.trades[0].commission == Decimal(0)
    assert report.trades[0].net_pnl == report.trades[0].gross_pnl


def test_bracket_supplies_planned_risk_and_r_multiple() -> None:
    entry_at = BASE
    orders = [
        TradingViewOrder(
            id="1", symbol="CME_MINI:MESU2026", side=1, quantity=10,
            order_type="limit", status="filled",
            average_price=Decimal("7764"),
            placed_at=entry_at - timedelta(seconds=300),
            # Deliberately later than the fill: the venue finalises the order
            # after it fills, and matching these for equality finds nothing.
            closed_at=entry_at + timedelta(seconds=11),
        ),
        TradingViewOrder(
            id="2", symbol="CME_MINI:MESU2026", side=-1, quantity=10,
            order_type="stop", status="cancelled", stop_price=Decimal("7760.25"),
            placed_at=entry_at - timedelta(seconds=300),
        ),
    ]
    report = build_trades([ex(0, 1, 10, "7764"), ex(60, -1, 10, "7772.25")], orders)
    trade = report.trades[0]
    assert trade.initial_stop == Decimal("7760.25")
    assert trade.planned_risk == Decimal("187.50")  # 3.75pt * 10 * $5
    assert trade.r_multiple == pytest.approx(2.2)


def test_ambiguous_bracket_declines_rather_than_guessing() -> None:
    """Two candidate stops, so R stays unknown.

    An unknown R is returned as None and excluded from statistics. A guessed one
    would be averaged in as fact and silently distort the whole playbook.
    """
    orders = [
        TradingViewOrder(
            id=str(i), symbol="CME_MINI:MESU2026", side=-1, quantity=10,
            order_type="stop", status="cancelled", stop_price=Decimal(stop),
            placed_at=BASE - timedelta(seconds=10),
        )
        for i, stop in enumerate(["7760.25", "7758.5"])
    ]
    report = build_trades([ex(0, 1, 10, "7764"), ex(60, -1, 10, "7772.25")], orders)
    assert report.trades[0].initial_stop is None
    assert report.trades[0].planned_risk is None
    assert report.trades[0].r_multiple is None


# ─── identity ────────────────────────────────────────────────────────────────


def test_trade_uuid_is_stable_and_distinct() -> None:
    a = "tradingview:34862113:CME_MINI:MESU2026:2479343643"
    b = "tradingview:34862113:CME_MINI:MESU2026:2479521076"
    assert trade_uuid(a) == trade_uuid(a)
    assert trade_uuid(a) != trade_uuid(b)


def test_same_fills_produce_the_same_trade_id() -> None:
    """The property that makes re-import an overwrite instead of an insert."""
    fills = [ex(0, 1, 10, "7764"), ex(60, -1, 10, "7772.25")]
    first = build_trades(fills, account="34862113")
    second = build_trades(fills, account="34862113")
    assert first.trades[0].id == second.trades[0].id
    assert first.trades[0].external_id == second.trades[0].external_id


def test_different_accounts_do_not_collide() -> None:
    fills = [ex(0, 1, 10, "7764"), ex(60, -1, 10, "7772.25")]
    assert (
        build_trades(fills, account="A").trades[0].id
        != build_trades(fills, account="B").trades[0].id
    )


# ─── reconciliation against the real account ─────────────────────────────────


def test_reconciles_with_tradingview_reported_pnl() -> None:
    """A verbatim slice of the owner's account, checked against TradingView.

    These eight fills are the last four MES round trips in account 34862113.
    TradingView's own Order history reports them as +412.50, -87.50, +262.50 and
    +475.00 — a net of +1,062.50. If this figure ever drifts, the pairing is
    wrong and every statistic computed from imported trades is wrong with it.
    """
    fills = [
        ex(0, 1, 10, "7770.25"), ex(100, -1, 10, "7779.75"),    # +475.00
        ex(200, 1, 10, "7764"), ex(300, -1, 10, "7772.25"),     # +412.50
        ex(400, 1, 10, "7805"), ex(500, -1, 10, "7803.25"),     # -87.50
        ex(600, -1, 10, "7807"), ex(700, 1, 10, "7801.75"),     # +262.50
    ]
    trips = pair_executions(fills)
    assert len(trips) == 4
    assert realized_pnl(trips) == Decimal("1062.50")

    report = build_trades(fills, account="34862113")
    assert sum(t.net_pnl for t in report.trades) == Decimal("1062.50")
