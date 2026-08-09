"""Futures contract specifications.

Every dollar figure in Shani — P&L, risk limits, position sizing, the equity
curve, the statistics the agent reasons over — is derived from the numbers in
this file. If a tick value here is wrong, nothing downstream is right, and
nothing downstream will tell you it is wrong. It will simply be confidently
incorrect by a constant factor.

Two deliberate choices:

**Decimal, never float.** Futures prices are exact multiples of a tick, and
ticks are decimal fractions. ``0.1 + 0.2 != 0.3`` in binary floating point, and
an ES price of ``5000.25`` accumulated over a few hundred fills drifts. Prices
and money are ``Decimal`` throughout; the only floats in Shani are ratios and
statistics where a rounding error in the twelfth decimal place is harmless.

**Tick value is derived, not stored.** ``tick_value = tick_size * multiplier``
always holds — ES: ``0.25 * 50 = $12.50``, CL: ``0.01 * 1000 = $10.00``. Storing
both invites them to disagree after an edit. We store the two independent facts
and compute the third, and ``tests/test_instruments.py`` asserts the derived
value against an independently transcribed table from the exchange specs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Final

__all__ = [
    "INSTRUMENTS",
    "MONTH_CODES",
    "AssetClass",
    "Instrument",
    "SessionWindow",
    "front_month_code",
    "get_instrument",
    "parse_contract",
    "root_of",
]


class AssetClass(str, Enum):
    EQUITY_INDEX = "equity_index"
    ENERGY = "energy"
    METALS = "metals"
    RATES = "rates"


#: CME month codes. ``H`` (March) through ``Z`` (December) are the ones that
#: matter for equity index quarterlies; the full set is needed to parse CL,
#: which lists every calendar month.
MONTH_CODES: Final[dict[str, int]] = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
CODE_BY_MONTH: Final[dict[int, str]] = {v: k for k, v in MONTH_CODES.items()}

#: Quarterly cycle used by all four equity index complexes.
QUARTERLY: Final[tuple[str, ...]] = ("H", "M", "U", "Z")
#: COMEX gold's active delivery months.
GOLD_MONTHS: Final[tuple[str, ...]] = ("G", "J", "M", "Q", "V", "Z")
#: NYMEX crude lists every calendar month.
ALL_MONTHS: Final[tuple[str, ...]] = tuple(MONTH_CODES)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """A daily trading window in US Eastern time.

    Futures sessions cross midnight — Globex opens at 18:00 ET and runs to 17:00
    ET the next day — so ``start > end`` is normal and means "wraps midnight".
    :meth:`contains` handles both cases, which is the entire reason this is a
    type rather than a pair of loose ``time`` fields.
    """

    start: time
    end: time
    label: str

    @property
    def wraps_midnight(self) -> bool:
        return self.start > self.end

    def contains(self, t: time) -> bool:
        """Is ``t`` inside this window? Start-inclusive, end-exclusive."""
        if self.wraps_midnight:
            return t >= self.start or t < self.end
        return self.start <= t < self.end


# ─── Session windows ─────────────────────────────────────────────────────────
#
# All times US Eastern. Globex runs Sunday 18:00 → Friday 17:00 with a daily
# 60-minute maintenance halt at 17:00. The weekly boundary is handled in
# sessions.py, which knows about weekdays and holidays; these windows describe
# only the shape of a single day.

GLOBEX_EQUITY = SessionWindow(time(18, 0), time(17, 0), "globex")
RTH_EQUITY = SessionWindow(time(9, 30), time(16, 0), "rth")

GLOBEX_ENERGY = SessionWindow(time(18, 0), time(17, 0), "globex")
RTH_ENERGY = SessionWindow(time(9, 0), time(14, 30), "rth")

GLOBEX_METALS = SessionWindow(time(18, 0), time(17, 0), "globex")
RTH_METALS = SessionWindow(time(8, 20), time(13, 30), "rth")


@dataclass(frozen=True, slots=True)
class Instrument:
    """Specification for one futures contract root.

    ``root`` is the exchange root without a contract month (``ES``, not
    ``ESZ5``). ``tv_symbol`` is the TradingView continuous-contract symbol used
    for charting and screening (``CME:ES1!``) — note that this is *not* what you
    trade. See :mod:`shani.rollover` for why that distinction matters.
    """

    root: str
    name: str
    exchange: str
    tv_symbol: str
    asset_class: AssetClass
    tick_size: Decimal
    #: Dollars per one full point of price movement, per contract.
    multiplier: Decimal
    currency: str
    contract_months: tuple[str, ...]
    rth: SessionWindow
    globex: SessionWindow
    #: Typical retail commission per side, per contract. Configurable — this is
    #: only the default so the paper broker isn't unrealistically free.
    commission_per_side: Decimal
    #: Micro contracts are 1/10th of their parent; recorded so the UI can offer
    #: "size down" and so risk checks can suggest an alternative.
    micro_of: str | None = None

    @property
    def tick_value(self) -> Decimal:
        """Dollars gained or lost per contract per one-tick move."""
        return self.tick_size * self.multiplier

    @property
    def is_micro(self) -> bool:
        return self.micro_of is not None

    # ── price arithmetic ─────────────────────────────────────────────────────

    def round_to_tick(self, price: Decimal) -> Decimal:
        """Snap a price to the nearest valid tick.

        Used on any price that came from outside Shani — a webhook payload, an
        LLM proposal, a user-typed limit. Exchanges reject off-tick orders, and
        an LLM will absolutely propose an ES limit at ``5001.13``.
        """
        ticks = (price / self.tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return ticks * self.tick_size

    def is_on_tick(self, price: Decimal) -> bool:
        return price % self.tick_size == 0

    def ticks_between(self, a: Decimal, b: Decimal) -> Decimal:
        """Signed tick distance from ``a`` to ``b``."""
        return (b - a) / self.tick_size

    def pnl(self, entry: Decimal, exit_: Decimal, quantity: int, is_long: bool) -> Decimal:
        """Gross P&L in dollars, excluding commission.

        ``quantity`` is contracts and is always positive; direction comes from
        ``is_long``. Keeping sign in one place means a short's P&L cannot be
        accidentally negated twice, which is the classic version of this bug.
        """
        direction = Decimal(1) if is_long else Decimal(-1)
        return (exit_ - entry) * direction * self.multiplier * Decimal(quantity)

    def commission(self, quantity: int, sides: int = 2) -> Decimal:
        """Round-turn commission by default (``sides=2``: entry and exit)."""
        return self.commission_per_side * Decimal(quantity) * Decimal(sides)

    def ticks_to_dollars(self, ticks: Decimal, quantity: int = 1) -> Decimal:
        return ticks * self.tick_value * Decimal(quantity)

    def dollars_to_ticks(self, dollars: Decimal, quantity: int = 1) -> Decimal:
        return dollars / (self.tick_value * Decimal(quantity))


def _spec(
    root: str,
    name: str,
    exchange: str,
    asset_class: AssetClass,
    tick_size: str,
    multiplier: str,
    contract_months: tuple[str, ...],
    rth: SessionWindow,
    globex: SessionWindow,
    commission: str = "2.50",
    micro_of: str | None = None,
) -> Instrument:
    return Instrument(
        root=root,
        name=name,
        exchange=exchange,
        tv_symbol=f"{exchange}:{root}1!",
        asset_class=asset_class,
        tick_size=Decimal(tick_size),
        multiplier=Decimal(multiplier),
        currency="USD",
        contract_months=contract_months,
        rth=rth,
        globex=globex,
        commission_per_side=Decimal(commission),
        micro_of=micro_of,
    )


#: The instruments Shani ships knowing about.
#:
#: Scoped deliberately to the four complexes the project targets (ES/NQ/CL/GC)
#: plus their micros, plus the handful of index and metals contracts a futures
#: trader is likely to have on the same watchlist. Adding a contract is a
#: five-line entry here plus a row in the test table — and the test table must
#: be transcribed from the exchange spec independently, not copied from here,
#: or it verifies nothing.
INSTRUMENTS: Final[dict[str, Instrument]] = {
    # ── Equity index ─────────────────────────────────────────────────────────
    "ES": _spec("ES", "E-mini S&P 500", "CME", AssetClass.EQUITY_INDEX,
                "0.25", "50", QUARTERLY, RTH_EQUITY, GLOBEX_EQUITY),
    "MES": _spec("MES", "Micro E-mini S&P 500", "CME", AssetClass.EQUITY_INDEX,
                 "0.25", "5", QUARTERLY, RTH_EQUITY, GLOBEX_EQUITY,
                 commission="0.50", micro_of="ES"),
    "NQ": _spec("NQ", "E-mini Nasdaq-100", "CME", AssetClass.EQUITY_INDEX,
                "0.25", "20", QUARTERLY, RTH_EQUITY, GLOBEX_EQUITY),
    "MNQ": _spec("MNQ", "Micro E-mini Nasdaq-100", "CME", AssetClass.EQUITY_INDEX,
                 "0.25", "2", QUARTERLY, RTH_EQUITY, GLOBEX_EQUITY,
                 commission="0.50", micro_of="NQ"),
    "RTY": _spec("RTY", "E-mini Russell 2000", "CME", AssetClass.EQUITY_INDEX,
                 "0.10", "50", QUARTERLY, RTH_EQUITY, GLOBEX_EQUITY),
    "M2K": _spec("M2K", "Micro E-mini Russell 2000", "CME", AssetClass.EQUITY_INDEX,
                 "0.10", "5", QUARTERLY, RTH_EQUITY, GLOBEX_EQUITY,
                 commission="0.50", micro_of="RTY"),
    "YM": _spec("YM", "E-mini Dow", "CBOT", AssetClass.EQUITY_INDEX,
                "1", "5", QUARTERLY, RTH_EQUITY, GLOBEX_EQUITY),
    "MYM": _spec("MYM", "Micro E-mini Dow", "CBOT", AssetClass.EQUITY_INDEX,
                 "1", "0.50", QUARTERLY, RTH_EQUITY, GLOBEX_EQUITY,
                 commission="0.50", micro_of="YM"),

    # ── Energy ───────────────────────────────────────────────────────────────
    "CL": _spec("CL", "Crude Oil (WTI)", "NYMEX", AssetClass.ENERGY,
                "0.01", "1000", ALL_MONTHS, RTH_ENERGY, GLOBEX_ENERGY),
    "MCL": _spec("MCL", "Micro Crude Oil", "NYMEX", AssetClass.ENERGY,
                 "0.01", "100", ALL_MONTHS, RTH_ENERGY, GLOBEX_ENERGY,
                 commission="0.50", micro_of="CL"),
    "NG": _spec("NG", "Natural Gas", "NYMEX", AssetClass.ENERGY,
                "0.001", "10000", ALL_MONTHS, RTH_ENERGY, GLOBEX_ENERGY),

    # ── Metals ───────────────────────────────────────────────────────────────
    "GC": _spec("GC", "Gold", "COMEX", AssetClass.METALS,
                "0.10", "100", GOLD_MONTHS, RTH_METALS, GLOBEX_METALS),
    "MGC": _spec("MGC", "Micro Gold", "COMEX", AssetClass.METALS,
                 "0.10", "10", GOLD_MONTHS, RTH_METALS, GLOBEX_METALS,
                 commission="0.50", micro_of="GC"),
    "SI": _spec("SI", "Silver", "COMEX", AssetClass.METALS,
                "0.005", "5000", ("H", "K", "N", "U", "Z"), RTH_METALS, GLOBEX_METALS),
}

#: Default watchlist for a fresh install — the four the project targets.
DEFAULT_WATCHLIST: Final[tuple[str, ...]] = ("ES", "NQ", "CL", "GC")


class UnknownInstrumentError(KeyError):
    """Raised for a root Shani has no specification for.

    Deliberately fatal rather than falling back to a guessed tick size. A wrong
    tick value produces plausible-looking dollar figures that are silently off
    by a constant factor, which is far worse than a crash.
    """

    def __init__(self, symbol: str) -> None:
        known = ", ".join(sorted(INSTRUMENTS))
        super().__init__(
            f"No contract specification for {symbol!r}. Known roots: {known}. "
            f"Add it to shani/instruments.py — Shani will not guess a tick size."
        )


def root_of(symbol: str) -> str:
    """Extract the contract root from any symbol form Shani might see.

    Handles ``ES``, ``ESZ5``, ``ESZ25``, ``ES1!``, ``CME:ES1!``, ``CME_MINI:ES1!``.
    All of these appear in practice: TradingView uses the continuous form, the
    broker uses the dated form, and users type the bare root.
    """
    s = symbol.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    if s.endswith("!"):
        s = s.rstrip("!").rstrip("0123456789")
        return s
    # Dated contract: strip a trailing 1-2 digit year and its month code, but
    # only when what remains is a root we recognise. This avoids mangling roots
    # that legitimately end in a month-code letter.
    for width in (2, 1):
        if len(s) > width + 1 and s[-width:].isdigit() and s[-(width + 1)] in MONTH_CODES:
            candidate = s[: -(width + 1)]
            if candidate in INSTRUMENTS:
                return candidate
    return s


def get_instrument(symbol: str) -> Instrument:
    """Look up a contract spec by any symbol form. Raises if unknown."""
    root = root_of(symbol)
    try:
        return INSTRUMENTS[root]
    except KeyError:
        raise UnknownInstrumentError(symbol) from None


def parse_contract(symbol: str) -> tuple[str, int, int] | None:
    """Parse a dated contract into ``(root, month, year)``.

    ``"ESZ5"`` → ``("ES", 12, 2025)``; ``"ESZ25"`` → ``("ES", 12, 2025)``.

    Single-digit years are resolved into the current decade, which is what the
    exchanges mean by them. Returns ``None`` for continuous or bare symbols,
    which are not dated contracts and have no expiry.
    """
    s = symbol.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    if s.endswith("!") or not s:
        return None

    for width in (2, 1):
        if len(s) <= width + 1:
            continue
        digits, code = s[-width:], s[-(width + 1)]
        if not digits.isdigit() or code not in MONTH_CODES:
            continue
        root = s[: -(width + 1)]
        if root not in INSTRUMENTS:
            continue
        year = int(digits)
        if width == 1:
            current = date.today().year
            decade = current - (current % 10)
            year = decade + year
            # A single-digit year more than three years behind means the next
            # decade — in 2029, "ESZ1" is December 2031, not 2021.
            if year < current - 3:
                year += 10
        else:
            year += 2000
        return root, MONTH_CODES[code], year
    return None


def front_month_code(instrument: Instrument, on: date) -> str:
    """The contract month code that is front month on a given date.

    Naive by design: it returns the first listed month at or after ``on``, with
    no roll offset. Real roll timing (volume migrates roughly eight days before
    expiry) lives in :mod:`shani.rollover`, which needs the expiry calendar.
    """
    listed = sorted(MONTH_CODES[c] for c in instrument.contract_months)
    for month in listed:
        if month >= on.month:
            return CODE_BY_MONTH[month]
    return CODE_BY_MONTH[listed[0]]
