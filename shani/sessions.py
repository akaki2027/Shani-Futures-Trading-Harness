"""Trading sessions, market hours, and time-of-day classification.

For a futures trader, *when* is very nearly as informative as *what*. The 09:30
opening drive and the 12:15 lunch chop are different markets that happen to
print the same symbol, and a journal that pools them together will show you a
mediocre average instead of one good habit and one expensive one.

So this module exists to answer three questions precisely:

1. Is the market open right now, and in which session?
2. Which part of the trading day did this trade happen in?
3. Was this trade taken during a session the trader actually performs well in?

Everything is anchored to ``America/New_York``. Not UTC, and emphatically not
the machine's local timezone — CME's daily boundaries are defined in Eastern
time and shift against UTC twice a year with US daylight saving. A trader in
London still has their day shaped by the New York open.

The holiday calendar here covers CME's full-holiday closures. It is deliberately
explicit rather than computed from a rules engine, and it needs extending each
year — see :data:`CME_HOLIDAYS`. Early closes (the half-days around
Thanksgiving, Christmas Eve, and July 3rd) are handled separately in
:data:`CME_EARLY_CLOSES` because they change the close time rather than removing
the day.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Final
from zoneinfo import ZoneInfo

from shani.instruments import Instrument

__all__ = [
    "EASTERN",
    "Session",
    "TimeOfDay",
    "classify_session",
    "is_market_open",
    "next_open",
    "session_date",
    "time_of_day",
    "to_eastern",
]

EASTERN: Final = ZoneInfo("America/New_York")

#: CME full-closure holidays. Extend annually.
#:
#: Dates are the *observed* dates, which is what matters — when July 4th falls
#: on a Saturday the market closes on Friday the 3rd, and hardcoding the
#: nominal date would mark a closed day as open.
CME_HOLIDAYS: Final[frozenset[date]] = frozenset({
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
})

#: Days the market closes early, mapped to the RTH close time in Eastern.
CME_EARLY_CLOSES: Final[dict[date, time]] = {
    date(2025, 7, 3): time(13, 0),
    date(2025, 11, 28): time(13, 0),
    date(2025, 12, 24): time(13, 0),
    date(2026, 11, 27): time(13, 0),
    date(2026, 12, 24): time(13, 0),
}

#: Globex daily maintenance halt: 17:00–18:00 ET, Monday through Thursday.
HALT_START: Final = time(17, 0)
HALT_END: Final = time(18, 0)

#: The weekly session opens Sunday 18:00 ET and closes Friday 17:00 ET.
WEEK_OPEN_WEEKDAY: Final = 6   # Sunday
WEEK_CLOSE_WEEKDAY: Final = 4  # Friday


class Session(str, Enum):
    """Which market session a moment falls in."""

    RTH = "rth"
    """Regular trading hours — the cash session. Where most volume lives."""

    OVERNIGHT = "overnight"
    """Globex outside RTH. Thinner, gappier, different character entirely."""

    CLOSED = "closed"
    """Weekend, holiday, or the daily maintenance halt."""


class TimeOfDay(str, Enum):
    """Named parts of the futures trading day.

    These buckets are not arbitrary clock slices — each is a period with its own
    recognisable behaviour, which is what makes "your win rate by time of day"
    an actionable statistic rather than a noisy one.
    """

    ASIA = "asia"                   # 18:00–03:00 — overnight, thin
    LONDON = "london"               # 03:00–08:00 — European session
    PREMARKET = "premarket"         # 08:00–09:30 — positioning before the cash open
    OPENING_DRIVE = "opening_drive" # 09:30–10:30 — highest volume of the day
    LATE_MORNING = "late_morning"   # 10:30–12:00 — trend continuation or fade
    LUNCH = "lunch"                 # 12:00–13:30 — thin, choppy, chews up accounts
    AFTERNOON = "afternoon"         # 13:30–15:00 — the afternoon trend
    CLOSING_HOUR = "closing_hour"   # 15:00–16:00 — MOC imbalance, position squaring
    POST_CLOSE = "post_close"       # 16:00–17:00 — winding down

    @property
    def label(self) -> str:
        return {
            TimeOfDay.ASIA: "Asia",
            TimeOfDay.LONDON: "London",
            TimeOfDay.PREMARKET: "Pre-market",
            TimeOfDay.OPENING_DRIVE: "Opening drive",
            TimeOfDay.LATE_MORNING: "Late morning",
            TimeOfDay.LUNCH: "Lunch",
            TimeOfDay.AFTERNOON: "Afternoon",
            TimeOfDay.CLOSING_HOUR: "Closing hour",
            TimeOfDay.POST_CLOSE: "Post-close",
        }[self]


#: Ordered bucket boundaries, walked in order by :func:`time_of_day`.
#: Chronological from the 18:00 Globex open so the display order matches the
#: order a trader actually experiences the day.
_TOD_BOUNDARIES: Final[tuple[tuple[time, TimeOfDay], ...]] = (
    (time(18, 0), TimeOfDay.ASIA),
    (time(3, 0), TimeOfDay.LONDON),
    (time(8, 0), TimeOfDay.PREMARKET),
    (time(9, 30), TimeOfDay.OPENING_DRIVE),
    (time(10, 30), TimeOfDay.LATE_MORNING),
    (time(12, 0), TimeOfDay.LUNCH),
    (time(13, 30), TimeOfDay.AFTERNOON),
    (time(15, 0), TimeOfDay.CLOSING_HOUR),
    (time(16, 0), TimeOfDay.POST_CLOSE),
)

#: Display order for reports and the portal heatmap.
TIME_OF_DAY_ORDER: Final[tuple[TimeOfDay, ...]] = tuple(b for _, b in _TOD_BOUNDARIES)

#: The same boundaries sorted by wall-clock time, for lookup.
#:
#: This is deliberately *not* the same ordering as above, and the distinction is
#: easy to get wrong: the trading day starts at 18:00, so ASIA comes first in
#: display order but last on the clock. Walking the display order to classify a
#: timestamp misfiles every evening hour — 19:00 matches the 16:00 POST_CLOSE
#: boundary before it ever reaches ASIA.
_TOD_LOOKUP: Final[tuple[tuple[time, TimeOfDay], ...]] = tuple(
    sorted(_TOD_BOUNDARIES, key=lambda pair: pair[0])
)


def to_eastern(dt: datetime) -> datetime:
    """Convert any datetime to Eastern.

    A naive datetime is *assumed to already be Eastern* rather than silently
    treated as UTC. Guessing UTC here would shift every timestamp by four or
    five hours and quietly relabel the opening drive as pre-market.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=EASTERN)
    return dt.astimezone(EASTERN)


def time_of_day(dt: datetime) -> TimeOfDay:
    """Which named part of the trading day a moment falls in."""
    t = to_eastern(dt).time()
    # Walk clock-sorted boundaries from latest to earliest and take the first
    # one at or before ``t``. Anything earlier than the 03:00 London boundary
    # falls through to ASIA, which owns both the 18:00→24:00 and 00:00→03:00
    # legs of the overnight session.
    for start, bucket in reversed(_TOD_LOOKUP):
        if t >= start:
            return bucket
    return TimeOfDay.ASIA


def is_holiday(d: date) -> bool:
    return d in CME_HOLIDAYS


def session_date(dt: datetime) -> date:
    """The *trading day* a moment belongs to.

    Globex opens at 18:00 ET for the **next** calendar day's session, so a trade
    at 20:00 on Sunday belongs to Monday's session, and 22:00 Tuesday belongs to
    Wednesday. Getting this wrong shifts a third of all overnight trades into
    the wrong day and corrupts every daily statistic — including the max daily
    loss limit, which is a risk control, not just a number on a chart.
    """
    et = to_eastern(dt)
    if et.time() >= HALT_END:
        return (et + timedelta(days=1)).date()
    return et.date()


def classify_session(dt: datetime, instrument: Instrument) -> Session:
    """Classify a moment as RTH, overnight, or closed."""
    et = to_eastern(dt)
    t, d, weekday = et.time(), et.date(), et.weekday()

    if is_holiday(d):
        return Session.CLOSED

    # Weekend gap: Friday 17:00 → Sunday 18:00.
    if weekday == WEEK_CLOSE_WEEKDAY and t >= HALT_START:
        return Session.CLOSED
    if weekday == 5:  # Saturday
        return Session.CLOSED
    if weekday == WEEK_OPEN_WEEKDAY and t < HALT_END:
        return Session.CLOSED

    # Daily maintenance halt, Monday–Thursday.
    if HALT_START <= t < HALT_END:
        return Session.CLOSED

    # An early close ends RTH sooner, and the rest of that day stays shut.
    early = CME_EARLY_CLOSES.get(d)
    if early is not None and t >= early:
        return Session.CLOSED

    if instrument.rth.contains(t) and weekday <= WEEK_CLOSE_WEEKDAY:
        return Session.RTH
    return Session.OVERNIGHT


def is_market_open(dt: datetime, instrument: Instrument) -> bool:
    """Can an order be filled at this moment?

    The paper broker consults this before filling. Simulating a fill in a closed
    market produces backtest results that cannot be reproduced with real money —
    which is the single most common way a paper strategy flatters itself.
    """
    return classify_session(dt, instrument) is not Session.CLOSED


def next_open(dt: datetime, instrument: Instrument) -> datetime:
    """The next moment the market opens after ``dt``.

    Used to tell a user *when* their rejected after-hours order could actually
    have been filled, which is more useful than "market closed".

    Steps one minute at a time from a truncated start, so the answer lands on
    the real boundary. An earlier version stepped in fifteen-minute increments
    from the current time, which meant a query at 15:24 on a Sunday reported the
    reopen as 18:09 rather than 18:00 — a plausible-looking number that is
    simply wrong, and the kind a trader would set an alarm by.

    Gives up after two weeks rather than looping forever if the holiday table
    ever swallows a whole fortnight.
    """
    et = to_eastern(dt)
    limit = et + timedelta(days=14)
    probe = et.replace(second=0, microsecond=0)
    while probe < limit:
        probe += timedelta(minutes=1)
        if is_market_open(probe, instrument):
            return probe
    raise RuntimeError(
        f"No market open found within 14 days of {et.isoformat()} for "
        f"{instrument.root}. The holiday calendar in shani/sessions.py is "
        f"probably wrong."
    )
