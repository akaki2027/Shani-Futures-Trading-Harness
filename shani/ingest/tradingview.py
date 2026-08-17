"""Turn TradingView fills into Shani round trips.

TradingView records **executions** — one row per fill. Shani's unit of learning
is the **round trip**: got in here, got out there, this is what it cost and this
is what you were thinking. Everything in the playbook, every "you have taken
this setup seven times" claim, is computed off that table, so this module is the
one with the highest blast radius in the codebase. A quiet double-count here
does not crash anything; it just makes the trader's statistics wrong.

Nothing in here knows how to talk to TradingView. Reading is
:mod:`shani.market.tradingview_cdp`'s job; this module takes the resulting list
of :class:`~shani.market.tradingview_cdp.TradingViewExecution` and is otherwise
a pure function of it. That split is what lets the pairing be tested exhaustively
without a running copy of TradingView.

## Pairing: flat to flat, per symbol

A round trip runs from the moment a position opens off flat to the moment it
returns to flat. Scale-ins and scale-outs collapse into one trade with
size-weighted average entry and exit prices, which is how the trade is actually
remembered and the only way an interview about it makes sense.

**Pairing is per symbol, and getting that wrong is not a subtle failure.** The
first implementation pooled every fill into a single running position. An account
holding MES near 7,700, MNQ near 29,000 and SPY near 765 then never closes a
position against its own instrument, and the result was round trips wrong by
orders of magnitude — a six-figure phantom loss on an account that was up four
figures — with no exception raised anywhere. Prices from two instruments must
never meet in the same subtraction.

## Why this is trustworthy

The algorithm was not accepted because it looked right. It was run against a
live account and its total realized P&L reproduced the figure the broker reports
for that account, to the cent, across every round trip in five symbols.
:func:`realized_pnl` exists so that check stays runnable against your own
account — it is the one worth repeating after any change to the pairing.

## Reconciliation

An import re-reads the entire history every time, so the second run sees the
same 25 round trips as the first. Each one gets a deterministic UUID derived
from a stable external id (venue, account, symbol, opening fill id), so a
re-import overwrites the same rows rather than inserting a second copy. The
duplicate is impossible by construction rather than prevented by a check that a
future caller can forget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from shani.audit import AuditLog, EventType
from shani.db import Database
from shani.instruments import (
    Instrument,
    UnknownInstrumentError,
    get_instrument,
    parse_contract,
    root_of,
)
from shani.market.tradingview_cdp import (
    TradingViewDesktop,
    TradingViewExecution,
    TradingViewOrder,
)
from shani.models import Side, Trade
from shani.sessions import classify_session, time_of_day

__all__ = [
    "ImportReport",
    "RoundTrip",
    "build_trades",
    "import_from_desktop",
    "pair_executions",
    "realized_pnl",
    "save_trades",
    "trade_uuid",
]

#: Fields on an existing trade that an import must never overwrite. These are
#: the ones that did not come from the venue: what the trader said, what the
#: extraction learned, what the chart showed. A re-import that clobbered an
#: interview would destroy the only part of the record that cannot be
#: regenerated — the venue can always be re-read, the trader's answer cannot.
PRESERVED_ON_REIMPORT: tuple[str, ...] = (
    "interview",
    "notes",
    "tags",
    "setup_card_id",
    "followed_playbook",
    "entry_screenshot",
    "chart_timeframe",
    "chart_studies",
    "signal_id",
    "proposal_id",
    "max_adverse_excursion",
    "max_favorable_excursion",
)

#: Namespace for deterministic trade ids. Fixed forever — changing it would make
#: every previously imported trade re-import as a brand new row.
_TRADE_NAMESPACE = uuid5(NAMESPACE_URL, "https://shani.local/trade/external")

#: How far from the entry order a protective order may have been placed and still
#: be considered part of that entry's bracket. Brackets are submitted with the
#: entry, so this is generous; it exists only to tolerate clock jitter.
BRACKET_WINDOW = timedelta(seconds=90)

#: How far an order's recorded closing time may sit from the fill it produced.
#: Not zero: TradingView finalises an order after the fill, measured at 11
#: seconds on a live limit entry.
FILL_MATCH_WINDOW = timedelta(seconds=120)


def trade_uuid(external_id: str) -> UUID:
    """The deterministic id for an imported round trip.

    Same external id, same UUID, on every machine and every run — which is what
    turns ``Repository.save`` (an ``INSERT OR REPLACE`` keyed on id) into an
    idempotent upsert.
    """
    return uuid5(_TRADE_NAMESPACE, external_id)


@dataclass(slots=True)
class RoundTrip:
    """One position, from flat to flat, in a single symbol.

    Prices are size-weighted averages over the fills on each leg. That is exact
    for total P&L: summing ``(avg_exit - avg_entry) * total_qty`` gives the same
    dollars as pairing lot by lot, because both legs cover the same quantity.
    """

    symbol: str
    side: Side
    quantity: int
    entry_price: Decimal
    entry_at: datetime
    #: Id of the fill that opened this position — the stable half of its
    #: external id. Chosen over the closing fill so a position that is still
    #: open keeps the same identity once it eventually closes.
    entry_execution_id: str
    exit_price: Decimal | None = None
    exit_at: datetime | None = None
    commission: Decimal | None = None
    execution_ids: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.exit_at is None

    @property
    def is_long(self) -> bool:
        return self.side is Side.BUY


def _weighted(legs: list[tuple[Decimal, int]]) -> tuple[Decimal, int]:
    """Size-weighted average price and total quantity."""
    total = sum(q for _, q in legs)
    if total == 0:
        return Decimal(0), 0
    return sum(p * q for p, q in legs) / Decimal(total), total


def pair_executions(executions: list[TradingViewExecution]) -> list[RoundTrip]:
    """Collapse a flat list of fills into round trips, per symbol.

    Fills are grouped by symbol and walked oldest-first. A position that reverses
    in a single fill (long 10 sold 20) is split: the first 10 close the long, the
    remaining 10 open a short. A position still open at the end is returned as an
    open round trip rather than dropped — an open trade is a real thing the
    trader may want journaled.
    """
    by_symbol: dict[str, list[TradingViewExecution]] = {}
    for e in executions:
        by_symbol.setdefault(e.symbol, []).append(e)

    trips: list[RoundTrip] = []
    for symbol, fills in by_symbol.items():
        trips.extend(_pair_one_symbol(symbol, sorted(fills, key=lambda e: (e.time, e.id))))
    trips.sort(key=lambda t: t.entry_at)
    return trips


def _pair_one_symbol(
    symbol: str, fills: list[TradingViewExecution]
) -> list[RoundTrip]:
    trips: list[RoundTrip] = []
    position = 0
    entries: list[tuple[Decimal, int]] = []
    exits: list[tuple[Decimal, int]] = []
    opened_at: datetime | None = None
    opening_id = ""
    direction = 0
    fees: list[Decimal] = []
    seen_ids: list[str] = []

    def flush(closed_at: datetime | None) -> None:
        """Emit the accumulated position as a round trip."""
        nonlocal entries, exits, fees, seen_ids
        entry_price, entry_qty = _weighted(entries)
        if entry_qty == 0:
            return
        exit_price, exit_qty = _weighted(exits)
        trips.append(
            RoundTrip(
                symbol=symbol,
                side=Side.BUY if direction > 0 else Side.SELL,
                quantity=entry_qty,
                entry_price=entry_price,
                entry_at=opened_at or datetime.now(UTC),
                entry_execution_id=opening_id,
                exit_price=exit_price if exit_qty else None,
                exit_at=closed_at if exit_qty else None,
                # None, not zero: a paper account charges nothing and reports
                # nothing, and that is not the same as a broker that charged $0.
                commission=sum(fees, Decimal(0)) if fees else None,
                execution_ids=list(seen_ids),
            )
        )
        entries, exits, fees, seen_ids = [], [], [], []

    for fill in fills:
        signed = fill.signed_quantity
        if fill.commission is not None:
            fees.append(fill.commission)
        seen_ids.append(fill.id)

        if position == 0:
            opened_at, opening_id, direction = fill.time, fill.id, fill.side
            entries, exits = [(fill.price, fill.quantity)], []
            position = signed
            continue

        if (position > 0) == (fill.side > 0):
            entries.append((fill.price, fill.quantity))
            position += signed
            continue

        # Reducing. Only the part that offsets the open position closes it; any
        # surplus reverses into a new position in the opposite direction.
        closing = min(fill.quantity, abs(position))
        exits.append((fill.price, closing))
        position += signed
        surplus = fill.quantity - closing

        if position == 0:
            flush(fill.time)
            opened_at, opening_id, direction = None, "", 0
        elif surplus:
            flush(fill.time)
            opened_at, opening_id, direction = fill.time, fill.id, fill.side
            entries, exits = [(fill.price, surplus)], []
            fees, seen_ids = [], [fill.id]
            position = surplus * fill.side

    if position != 0:
        flush(None)
    return trips


def _bracket_for(
    trip: RoundTrip, orders: list[TradingViewOrder]
) -> tuple[Decimal | None, Decimal | None]:
    """Best-effort protective stop and target that were working at entry.

    TradingView exposes no parent-order link through this API, so the bracket is
    matched in two steps. First find the order that *produced* the opening fill:
    a filled order on the same symbol and side whose ``closed_at`` is exactly the
    fill's timestamp. Then take the opposite-side orders placed alongside it.

    Anchoring on the entry **order's placing time** rather than the fill time is
    the whole trick. A bracket is submitted when the entry is submitted, not when
    it fills, and a resting limit entry can sit for minutes first — in this
    account the gap was routinely seven minutes, so anchoring on the fill missed
    every bracket on a limit entry while appearing to work on market entries.

    Applied **only when exactly one candidate of a kind matches**; anything
    ambiguous yields ``None``. Guessing would be worse than declining to:
    ``planned_risk`` is the denominator of R, and :attr:`Trade.r_multiple`
    returns ``None`` rather than averaging an unknown R in as a flat outcome. A
    wrong stop silently distorts every R statistic in the playbook; a missing one
    is merely absent.
    """
    want_side = -1 if trip.is_long else 1

    entry_orders = [
        o
        for o in orders
        if o.symbol == trip.symbol
        and o.side == (1 if trip.is_long else -1)
        and o.is_filled
        and o.closed_at is not None
        and abs(o.closed_at - trip.entry_at) <= FILL_MATCH_WINDOW
    ]
    # An order's closing time is when the venue finalised it, which trails its
    # own fill — measured at 11 seconds on a live limit entry, so matching the
    # two timestamps for equality finds nothing. Where several orders closed
    # near the same moment, the one whose average fill price is the price we
    # were filled at is the entry; that disambiguates all but true ties.
    if len(entry_orders) > 1:
        exact = [o for o in entry_orders if o.average_price == trip.entry_price]
        entry_orders = exact

    anchor = (
        entry_orders[0].placed_at
        if len(entry_orders) == 1 and entry_orders[0].placed_at
        else trip.entry_at
    )

    near = [
        o
        for o in orders
        if o.symbol == trip.symbol
        and o.side == want_side
        and o.placed_at is not None
        and abs(o.placed_at - anchor) <= BRACKET_WINDOW
    ]
    stops = [o.stop_price for o in near if o.order_type == "stop" and o.stop_price]
    targets = [o.limit_price for o in near if o.order_type == "limit" and o.limit_price]
    return (
        stops[0] if len(stops) == 1 else None,
        targets[0] if len(targets) == 1 else None,
    )


def realized_pnl(trips: list[RoundTrip]) -> Decimal:
    """Total gross realized P&L across closed round trips.

    The reconciliation hook: this should equal the realized P&L TradingView
    reports for the account. If it does not, the pairing is wrong and no
    statistic computed downstream can be trusted.
    """
    total = Decimal(0)
    for trip in trips:
        if trip.exit_price is None:
            continue
        try:
            instrument = get_instrument(trip.symbol)
        except UnknownInstrumentError:
            continue
        total += instrument.pnl(
            trip.entry_price, trip.exit_price, trip.quantity, trip.is_long
        )
    return total


@dataclass(slots=True)
class ImportReport:
    """What an import did, in enough detail to argue with.

    ``skipped`` is not a warning to be swallowed. Shani prices futures from
    :mod:`shani.instruments` and refuses to guess a multiplier, so an account
    that also traded equities imports its futures and says plainly which rows it
    left behind and why.
    """

    trades: list[Trade] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    open_trips: int = 0
    #: Gross realized P&L of everything imported, for reconciliation against the
    #: number TradingView shows.
    #:
    #: Note that this will fall *short* of TradingView's account figure by
    #: exactly the P&L of the skipped rows, and that shortfall is not
    #: computable here — pricing a skipped symbol is precisely what Shani has no
    #: multiplier for. When ``skipped`` is non-empty, a difference against
    #: TradingView is expected rather than a discrepancy to chase.
    imported_pnl: Decimal = Decimal(0)

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def skipped_count(self) -> int:
        return sum(self.skipped.values())


def build_trades(
    executions: list[TradingViewExecution],
    orders: list[TradingViewOrder] | None = None,
    *,
    account: str | None = None,
    venue: str = "tradingview",
) -> ImportReport:
    """Pair fills into round trips and render them as :class:`Trade` records.

    P&L, tick values and multipliers all come from :mod:`shani.instruments` —
    the one source of truth for money. TradingView's own figures are used only
    for the reconciliation check, never as the stored value, so a display
    rounding on their side cannot become a wrong number on ours.
    """
    orders = orders or []
    report = ImportReport()

    for trip in pair_executions(executions):
        try:
            instrument = get_instrument(trip.symbol)
        except UnknownInstrumentError:
            root = root_of(trip.symbol)
            report.skipped[root] = report.skipped.get(root, 0) + 1
            continue

        if trip.is_open:
            report.open_trips += 1

        report.trades.append(_to_trade(trip, instrument, orders, account, venue))

    report.imported_pnl = sum(
        (t.gross_pnl for t in report.trades), Decimal(0)
    )
    return report


def save_trades(db: Database, report: ImportReport) -> tuple[int, int]:
    """Persist an import, returning ``(inserted, updated)``.

    Because the trade's id is derived from its external id, a re-import lands on
    the same row and ``INSERT OR REPLACE`` overwrites it. That is what stops
    duplicates — and it is also the danger: a blind overwrite would replace a
    trade the trader has since been interviewed about with a fresh, empty one.
    So an existing row is *merged*, not replaced: venue facts are refreshed and
    everything in :data:`PRESERVED_ON_REIMPORT` is carried across untouched.
    """
    inserted = updated = 0
    with db.trades.transaction():
        for trade in report.trades:
            existing = db.trades.get(trade.id)
            if existing is None:
                db.trades.save(trade)
                inserted += 1
                continue
            for name in PRESERVED_ON_REIMPORT:
                setattr(trade, name, getattr(existing, name))
            # Keep the original creation time; this row is not new.
            trade.created_at = existing.created_at
            db.trades.save(trade)
            updated += 1
    return inserted, updated


async def import_from_desktop(
    db: Database,
    desktop: TradingViewDesktop,
    *,
    audit: AuditLog | None = None,
) -> ImportReport:
    """Read the connected TradingView account and import its history.

    The whole history is re-read every time. That is deliberate: an incremental
    read would have to decide what it had already seen, and getting that wrong
    means either duplicated trades or silently missing ones. Re-reading
    everything and landing on deterministic ids makes the operation idempotent,
    so running it twice is a no-op rather than a corruption.
    """
    account = await desktop.account_id()
    report = build_trades(
        await desktop.executions(),
        await desktop.order_history(),
        account=account,
    )
    inserted, updated = save_trades(db, report)

    if audit is not None:
        audit.record(
            EventType.TRADE_IMPORTED,
            f"Imported {report.count} round trips from TradingView account "
            f"{account} ({inserted} new, {updated} updated)",
            payload={
                "account": account,
                "inserted": inserted,
                "updated": updated,
                "open": report.open_trips,
                "skipped": report.skipped,
                "gross_pnl": str(report.imported_pnl),
            },
        )
    return report


def _to_trade(
    trip: RoundTrip,
    instrument: Instrument,
    orders: list[TradingViewOrder],
    account: str | None,
    venue: str,
) -> Trade:
    external_id = ":".join(
        [venue, account or "unknown", trip.symbol, trip.entry_execution_id]
    )

    gross = Decimal(0)
    if trip.exit_price is not None:
        gross = instrument.pnl(
            trip.entry_price, trip.exit_price, trip.quantity, trip.is_long
        )

    stop, target = _bracket_for(trip, orders)
    planned_risk = None
    if stop is not None:
        planned_risk = abs(trip.entry_price - stop) * instrument.multiplier * Decimal(
            trip.quantity
        )

    # The dated contract is preserved separately from the root. Statistics that
    # pool across a quarterly rollover without this are quietly wrong every
    # quarter — see Trade.contract.
    bare = trip.symbol.split(":")[-1]
    contract = bare if parse_contract(trip.symbol) else None

    return Trade(
        id=trade_uuid(external_id),
        external_id=external_id,
        symbol=instrument.root,
        contract=contract,
        side=trip.side,
        quantity=trip.quantity,
        entry_price=trip.entry_price,
        exit_price=trip.exit_price,
        entry_at=trip.entry_at,
        exit_at=trip.exit_at,
        gross_pnl=gross,
        commission=trip.commission or Decimal(0),
        planned_risk=planned_risk,
        initial_stop=stop,
        initial_target=target,
        session=classify_session(trip.entry_at, instrument),
        time_of_day=time_of_day(trip.entry_at),
        broker=venue,
    )
