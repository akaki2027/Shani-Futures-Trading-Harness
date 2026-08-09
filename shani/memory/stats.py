"""Trading statistics, and an honest check on whether the learning works.

Two jobs.

**Describe the account.** Equity curve, win rate, expectancy, R-multiple
distribution, and performance grouped by time of day — which for a futures
trader is usually where the actionable finding is, because the 09:30 opening
drive and the 12:15 lunch chop are different markets that print the same symbol.

**Check whether Shani is helping.** :func:`evaluate_playbook` compares trades
taken on a playbook setup against trades taken off it. That comparison can come
back negative, and it is meant to be able to. A system that only ever reports
its own success is marketing; the point of measuring is that the measurement
could embarrass the tool.

That comparison is observational, not an experiment: the trader chooses which
trades to take, so a difference could reflect that playbook setups occur in
easier conditions rather than that the playbook helps. :attr:`Comparison.caveat`
says so, and it is displayed rather than buried.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from shani.db import Database
from shani.models import Trade, TradeOutcome
from shani.sessions import TIME_OF_DAY_ORDER, Session, TimeOfDay, session_date

__all__ = [
    "Comparison",
    "EquityPoint",
    "PerformanceSlice",
    "TradingStats",
    "compute_stats",
    "equity_curve",
    "evaluate_playbook",
]


@dataclass(frozen=True, slots=True)
class EquityPoint:
    at: datetime
    equity: Decimal
    trade_id: str


@dataclass(frozen=True, slots=True)
class PerformanceSlice:
    """Performance for one grouping — a session, an hour, an instrument."""

    label: str
    trades: int
    wins: int
    losses: int
    net_pnl: Decimal
    avg_r: float | None

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return self.wins / decided if decided else None


@dataclass(frozen=True, slots=True)
class TradingStats:
    """Overall account performance."""

    total_trades: int
    wins: int
    losses: int
    breakeven: int
    gross_pnl: Decimal
    commission: Decimal
    net_pnl: Decimal
    largest_win: Decimal
    largest_loss: Decimal
    avg_r: float | None
    expectancy: Decimal | None
    profit_factor: float | None
    max_drawdown: Decimal
    by_time_of_day: tuple[PerformanceSlice, ...] = ()
    by_session: tuple[PerformanceSlice, ...] = ()
    by_symbol: tuple[PerformanceSlice, ...] = ()

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return self.wins / decided if decided else None

    @property
    def worst_time_of_day(self) -> PerformanceSlice | None:
        """The bucket costing the most money.

        Usually the single most useful line in the whole report — most traders
        have one part of the day that quietly funds everything else.
        """
        losing = [s for s in self.by_time_of_day if s.net_pnl < 0]
        return min(losing, key=lambda s: s.net_pnl) if losing else None

    @property
    def best_time_of_day(self) -> PerformanceSlice | None:
        winning = [s for s in self.by_time_of_day if s.net_pnl > 0]
        return max(winning, key=lambda s: s.net_pnl) if winning else None


@dataclass(frozen=True, slots=True)
class Comparison:
    """Playbook-following trades versus everything else."""

    followed: PerformanceSlice
    unfollowed: PerformanceSlice
    caveat: str = field(
        default=(
            "Observational, not an experiment. You choose which trades to take, so "
            "a difference here may reflect that playbook setups occur in easier "
            "conditions rather than that the playbook itself helps."
        )
    )

    @property
    def has_enough_data(self) -> bool:
        return self.followed.trades >= 10 and self.unfollowed.trades >= 10

    def verdict(self) -> str:
        if not self.has_enough_data:
            return (
                f"Not enough data yet — {self.followed.trades} on-playbook and "
                f"{self.unfollowed.trades} off-playbook trades. Need at least 10 of each."
            )
        delta = self.followed.net_pnl - self.unfollowed.net_pnl
        direction = "better" if delta > 0 else "worse"
        return (
            f"On-playbook trades did ${abs(delta):,.2f} {direction} in net P&L "
            f"({self.followed.trades} vs {self.unfollowed.trades} trades). {self.caveat}"
        )


def _closed(db: Database) -> list[Trade]:
    return db.trades.where("exit_at IS NOT NULL", order_by="entry_at ASC")


def _slice(label: str, trades: list[Trade]) -> PerformanceSlice:
    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]
    return PerformanceSlice(
        label=label,
        trades=len(trades),
        wins=sum(1 for t in trades if t.outcome is TradeOutcome.WIN),
        losses=sum(1 for t in trades if t.outcome is TradeOutcome.LOSS),
        net_pnl=sum((t.net_pnl for t in trades), start=Decimal(0)),
        avg_r=sum(r_values) / len(r_values) if r_values else None,
    )


def compute_stats(db: Database) -> TradingStats:
    """Full performance report over all closed trades."""
    trades = _closed(db)
    if not trades:
        return TradingStats(
            total_trades=0, wins=0, losses=0, breakeven=0,
            gross_pnl=Decimal(0), commission=Decimal(0), net_pnl=Decimal(0),
            largest_win=Decimal(0), largest_loss=Decimal(0),
            avg_r=None, expectancy=None, profit_factor=None, max_drawdown=Decimal(0),
        )

    wins = [t for t in trades if t.outcome is TradeOutcome.WIN]
    losses = [t for t in trades if t.outcome is TradeOutcome.LOSS]
    net_values = [t.net_pnl for t in trades]

    gross_win = sum((t.net_pnl for t in wins), start=Decimal(0))
    gross_loss = abs(sum((t.net_pnl for t in losses), start=Decimal(0)))
    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]

    # Max drawdown from the running equity peak.
    equity = Decimal(0)
    peak = Decimal(0)
    max_dd = Decimal(0)
    for value in net_values:
        equity += value
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    tod: dict[TimeOfDay, list[Trade]] = defaultdict(list)
    sess: dict[Session, list[Trade]] = defaultdict(list)
    sym: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        if trade.time_of_day:
            tod[trade.time_of_day].append(trade)
        if trade.session:
            sess[trade.session].append(trade)
        sym[trade.symbol].append(trade)

    return TradingStats(
        total_trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        breakeven=sum(1 for t in trades if t.outcome is TradeOutcome.BREAKEVEN),
        gross_pnl=sum((t.gross_pnl for t in trades), start=Decimal(0)),
        commission=sum((t.commission for t in trades), start=Decimal(0)),
        net_pnl=sum(net_values, start=Decimal(0)),
        largest_win=max(net_values, default=Decimal(0)),
        largest_loss=min(net_values, default=Decimal(0)),
        avg_r=sum(r_values) / len(r_values) if r_values else None,
        expectancy=sum(net_values, start=Decimal(0)) / len(trades),
        # None rather than infinity when there are no losses: a profit factor of
        # inf is not a fact about the strategy, it is a fact about the sample.
        profit_factor=float(gross_win / gross_loss) if gross_loss > 0 else None,
        max_drawdown=max_dd,
        by_time_of_day=tuple(
            _slice(bucket.label, tod[bucket]) for bucket in TIME_OF_DAY_ORDER if tod[bucket]
        ),
        by_session=tuple(_slice(s.value, group) for s, group in sess.items()),
        by_symbol=tuple(
            sorted((_slice(s, group) for s, group in sym.items()),
                   key=lambda item: item.net_pnl, reverse=True)
        ),
    )


def equity_curve(db: Database, starting_balance: Decimal = Decimal(0)) -> list[EquityPoint]:
    """Cumulative net P&L after each closed trade."""
    equity = starting_balance
    points: list[EquityPoint] = []
    for trade in _closed(db):
        equity += trade.net_pnl
        points.append(
            EquityPoint(at=trade.exit_at or trade.entry_at, equity=equity, trade_id=str(trade.id))
        )
    return points


def daily_pnl(db: Database) -> dict[str, Decimal]:
    """Net P&L per trading day, keyed by ISO date.

    Grouped by the 18:00 ET session boundary, so an overnight trade lands in the
    session it belongs to rather than being split at midnight.
    """
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for trade in _closed(db):
        if trade.exit_at:
            totals[session_date(trade.exit_at).isoformat()] += trade.net_pnl
    return dict(totals)


def evaluate_playbook(db: Database) -> Comparison:
    """Does following the playbook actually do better?

    The honesty check. This can and should be able to report that it does not.
    """
    trades = _closed(db)
    return Comparison(
        followed=_slice("on playbook", [t for t in trades if t.followed_playbook]),
        unfollowed=_slice("off playbook", [t for t in trades if not t.followed_playbook]),
    )
