"""Session classification and time-of-day bucketing tests.

Every timestamp here is explicitly Eastern. Tests that construct naive
datetimes and hope the machine is in New York pass on the author's laptop and
fail in CI, which is precisely the bug class this module exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from shani.instruments import INSTRUMENTS
from shani.sessions import (
    EASTERN,
    Session,
    TimeOfDay,
    classify_session,
    is_market_open,
    next_open,
    session_date,
    time_of_day,
    to_eastern,
)

ES = INSTRUMENTS["ES"]
GC = INSTRUMENTS["GC"]


def et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN)


class TestTimeOfDay:
    @pytest.mark.parametrize(
        ("hour", "minute", "expected"),
        [
            (19, 0, TimeOfDay.ASIA),
            (23, 30, TimeOfDay.ASIA),
            (1, 0, TimeOfDay.ASIA),          # after midnight, still Asia
            (3, 0, TimeOfDay.LONDON),
            (7, 59, TimeOfDay.LONDON),
            (8, 0, TimeOfDay.PREMARKET),
            (9, 29, TimeOfDay.PREMARKET),
            (9, 30, TimeOfDay.OPENING_DRIVE),  # the bell
            (10, 29, TimeOfDay.OPENING_DRIVE),
            (10, 30, TimeOfDay.LATE_MORNING),
            (12, 0, TimeOfDay.LUNCH),
            (13, 29, TimeOfDay.LUNCH),
            (13, 30, TimeOfDay.AFTERNOON),
            (15, 0, TimeOfDay.CLOSING_HOUR),
            (16, 0, TimeOfDay.POST_CLOSE),
            (17, 30, TimeOfDay.POST_CLOSE),
        ],
    )
    def test_bucketing(self, hour: int, minute: int, expected: TimeOfDay) -> None:
        assert time_of_day(et(2026, 3, 10, hour, minute)) == expected

    def test_asia_spans_midnight(self) -> None:
        """23:00 and 01:00 are the same session despite different calendar days."""
        assert time_of_day(et(2026, 3, 10, 23, 0)) == TimeOfDay.ASIA
        assert time_of_day(et(2026, 3, 11, 1, 0)) == TimeOfDay.ASIA

    def test_every_bucket_has_a_label(self) -> None:
        for bucket in TimeOfDay:
            assert bucket.label


class TestTimezoneHandling:
    def test_utc_input_converts_to_eastern(self) -> None:
        """14:30 UTC in March is 10:30 ET — late morning, not the opening drive."""
        from datetime import UTC

        utc_noon = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)
        assert time_of_day(utc_noon) == TimeOfDay.LATE_MORNING

    def test_naive_input_is_treated_as_eastern_not_utc(self) -> None:
        """Guessing UTC here would shift every timestamp by 4-5 hours."""
        naive = datetime(2026, 3, 10, 9, 45)
        assert time_of_day(naive) == TimeOfDay.OPENING_DRIVE

    def test_dst_transition_keeps_the_open_at_0930_local(self) -> None:
        """The bell is 09:30 ET year-round; its UTC offset changes, not the bell."""
        before = et(2026, 3, 5, 9, 45)   # EST
        after = et(2026, 3, 12, 9, 45)   # EDT
        assert time_of_day(before) == time_of_day(after) == TimeOfDay.OPENING_DRIVE
        assert to_eastern(before).utcoffset() != to_eastern(after).utcoffset()


class TestSessionClassification:
    def test_rth_midday_tuesday(self) -> None:
        assert classify_session(et(2026, 3, 10, 11, 0), ES) is Session.RTH

    def test_overnight_is_not_rth(self) -> None:
        assert classify_session(et(2026, 3, 10, 22, 0), ES) is Session.OVERNIGHT

    def test_daily_maintenance_halt_is_closed(self) -> None:
        assert classify_session(et(2026, 3, 10, 17, 30), ES) is Session.CLOSED

    def test_saturday_is_closed(self) -> None:
        assert classify_session(et(2026, 3, 14, 12, 0), ES) is Session.CLOSED

    def test_friday_evening_is_closed(self) -> None:
        """The week ends at 17:00 Friday and does not reopen until Sunday."""
        assert classify_session(et(2026, 3, 13, 18, 30), ES) is Session.CLOSED

    def test_sunday_reopen_at_1800(self) -> None:
        assert classify_session(et(2026, 3, 15, 17, 0), ES) is Session.CLOSED
        assert classify_session(et(2026, 3, 15, 18, 30), ES) is Session.OVERNIGHT

    def test_holiday_is_closed(self) -> None:
        """Christmas Day 2026."""
        assert classify_session(et(2026, 12, 25, 11, 0), ES) is Session.CLOSED

    def test_early_close_shuts_the_afternoon(self) -> None:
        """Christmas Eve 2026 closes at 13:00 ET."""
        assert classify_session(et(2026, 12, 24, 11, 0), ES) is Session.RTH
        assert classify_session(et(2026, 12, 24, 14, 0), ES) is Session.CLOSED

    def test_metals_rth_starts_before_equities(self) -> None:
        """08:30 ET is RTH for gold and pre-market for the S&P."""
        assert classify_session(et(2026, 3, 10, 8, 30), GC) is Session.RTH
        assert classify_session(et(2026, 3, 10, 8, 30), ES) is Session.OVERNIGHT

    def test_is_market_open_agrees_with_classification(self) -> None:
        for moment in (et(2026, 3, 10, 11, 0), et(2026, 3, 10, 22, 0),
                       et(2026, 3, 14, 12, 0), et(2026, 3, 10, 17, 30)):
            expected = classify_session(moment, ES) is not Session.CLOSED
            assert is_market_open(moment, ES) is expected


class TestSessionDate:
    """The trading day a moment belongs to, which is not the calendar day."""

    def test_daytime_belongs_to_its_own_date(self) -> None:
        assert session_date(et(2026, 3, 10, 11, 0)) == et(2026, 3, 10, 11, 0).date()

    def test_after_1800_belongs_to_the_next_day(self) -> None:
        """Sunday 20:00 is Monday's session."""
        assert session_date(et(2026, 3, 15, 20, 0)).isoformat() == "2026-03-16"

    def test_after_midnight_belongs_to_that_calendar_day(self) -> None:
        assert session_date(et(2026, 3, 16, 2, 0)).isoformat() == "2026-03-16"

    def test_evening_and_following_morning_share_a_session_date(self) -> None:
        """Otherwise a third of overnight trades land in the wrong day, and the
        max-daily-loss limit -- a risk control -- silently uses the wrong set."""
        assert session_date(et(2026, 3, 10, 20, 0)) == session_date(et(2026, 3, 11, 10, 0))


class TestNextOpen:
    def test_from_the_maintenance_halt_returns_the_1800_reopen(self) -> None:
        result = next_open(et(2026, 3, 10, 17, 15), ES)
        assert result.hour == 18
        assert result.date().isoformat() == "2026-03-10"

    def test_from_saturday_returns_sunday_evening(self) -> None:
        result = next_open(et(2026, 3, 14, 12, 0), ES)
        assert result.date().isoformat() == "2026-03-15"
        assert result.hour == 18

    def test_lands_exactly_on_the_boundary_not_a_stepping_grid(self) -> None:
        """The answer must be the real reopen time, to the minute.

        An earlier implementation stepped forward in fifteen-minute increments
        from the current time, so a query at 15:24 on a Sunday reported the
        reopen as 18:09. Plausible-looking, wrong, and precisely the sort of
        number someone sets an alarm by.
        """
        result = next_open(et(2026, 8, 16, 15, 24), ES)
        assert (result.hour, result.minute) == (18, 0)
        assert result.date().isoformat() == "2026-08-16"

    def test_boundary_is_exact_from_any_starting_minute(self) -> None:
        for minute in (1, 7, 23, 38, 59):
            result = next_open(et(2026, 8, 16, 15, minute), ES)
            assert (result.hour, result.minute) == (18, 0), f"wrong from :{minute:02d}"

    def test_result_is_actually_open(self) -> None:
        for start in (et(2026, 3, 14, 12, 0), et(2026, 3, 10, 17, 30),
                      et(2026, 12, 25, 10, 0)):
            assert is_market_open(next_open(start, ES), ES)

    def test_returns_a_moment_in_the_future(self) -> None:
        start = et(2026, 3, 14, 12, 0)
        assert next_open(start, ES) > start

    def test_skips_a_holiday_that_abuts_a_weekend(self) -> None:
        """Christmas 2026 falls on a Friday, so the next open is Sunday."""
        result = next_open(et(2026, 12, 25, 10, 0), ES)
        assert result > et(2026, 12, 25, 10, 0)
        assert is_market_open(result, ES)
        assert result - et(2026, 12, 25, 10, 0) < timedelta(days=4)
