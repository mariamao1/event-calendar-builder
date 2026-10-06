from datetime import UTC, date, datetime

import pytest

from calendar_api.recurrence import expand_revision


def test_daily_recurrence_keeps_wall_time_across_dst() -> None:
    revision = {
        "is_all_day": False,
        "starts_at": datetime(2026, 3, 7, 14, tzinfo=UTC),  # 09:00 EST
        "ends_at": datetime(2026, 3, 7, 15, tzinfo=UTC),
        "start_date": None,
        "end_date": None,
        "timezone": "America/New_York",
        "recurrence_rule": "FREQ=DAILY;COUNT=3",
    }

    occurrences = expand_revision(revision, [], date(2026, 3, 7), date(2026, 3, 11))

    assert [item.recurrence_id.hour for item in occurrences] == [9, 9, 9]
    assert [item.starts_at.hour for item in occurrences] == [14, 13, 13]


def test_rdate_and_exdate_are_applied_with_exdate_precedence() -> None:
    revision = {
        "is_all_day": True,
        "starts_at": None,
        "ends_at": None,
        "start_date": date(2026, 5, 1),
        "end_date": date(2026, 5, 2),
        "timezone": "America/New_York",
        "recurrence_rule": "FREQ=DAILY;COUNT=3",
    }
    recurrence_dates = [
        {"local_start": datetime(2026, 5, 2), "kind": "exclude"},  # noqa: DTZ001
        {"local_start": datetime(2026, 5, 4), "kind": "include"},  # noqa: DTZ001
    ]

    occurrences = expand_revision(
        revision, recurrence_dates, date(2026, 5, 1), date(2026, 5, 6)
    )

    assert [item.start_date for item in occurrences] == [
        date(2026, 5, 1),
        date(2026, 5, 3),
        date(2026, 5, 4),
    ]
    assert occurrences[-1].is_exception is True


def test_non_recurring_event_is_materialized_even_outside_rolling_window() -> None:
    revision = {
        "is_all_day": True,
        "starts_at": None,
        "ends_at": None,
        "start_date": date(2030, 1, 1),
        "end_date": date(2030, 1, 2),
        "timezone": "UTC",
        "recurrence_rule": None,
    }

    occurrences = expand_revision(revision, [], date(2026, 1, 1), date(2027, 1, 1))

    assert len(occurrences) == 1
    assert occurrences[0].start_date == date(2030, 1, 1)


def _all_day(start: date, rule: str) -> dict:
    return {
        "is_all_day": True,
        "starts_at": None,
        "ends_at": None,
        "start_date": start,
        "end_date": date.fromordinal(start.toordinal() + 1),
        "timezone": "America/New_York",
        "recurrence_rule": rule,
    }


@pytest.mark.parametrize(
    ("start", "rule", "expected"),
    [
        # Every other week on Tuesday and Thursday, four times.
        (
            date(2026, 10, 6),
            "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;COUNT=4",
            ["2026-10-06", "2026-10-08", "2026-10-20", "2026-10-22"],
        ),
        # Last Friday of every month.
        (
            date(2026, 10, 30),
            "FREQ=MONTHLY;BYDAY=-1FR;COUNT=3",
            ["2026-10-30", "2026-11-27", "2026-12-25"],
        ),
        # Day 31 skips months without one; the last-day pattern does not.
        (
            date(2026, 10, 31),
            "FREQ=MONTHLY;BYMONTHDAY=31;COUNT=3",
            ["2026-10-31", "2026-12-31", "2027-01-31"],
        ),
        (
            date(2026, 10, 31),
            "FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=3",
            ["2026-10-31", "2026-11-30", "2026-12-31"],
        ),
        # Fourth Thursday of November, every year.
        (
            date(2026, 11, 26),
            "FREQ=YEARLY;BYMONTH=11;BYDAY=4TH;COUNT=3",
            ["2026-11-26", "2027-11-25", "2028-11-23"],
        ),
        # Ends on a date (inclusive).
        (
            date(2026, 10, 6),
            "FREQ=DAILY;INTERVAL=3;UNTIL=20261015",
            ["2026-10-06", "2026-10-09", "2026-10-12", "2026-10-15"],
        ),
    ],
)
def test_supported_patterns_expand_to_expected_dates(
    start: date, rule: str, expected: list[str]
) -> None:
    occurrences = expand_revision(
        _all_day(start, rule), [], date(2026, 9, 1), date(2029, 1, 1)
    )
    assert [item.start_date.isoformat() for item in occurrences] == expected


def test_timed_until_includes_the_whole_last_local_day() -> None:
    revision = {
        "is_all_day": False,
        "starts_at": datetime(2026, 10, 6, 22, tzinfo=UTC),  # 18:00 EDT
        "ends_at": datetime(2026, 10, 6, 23, tzinfo=UTC),
        "start_date": None,
        "end_date": None,
        "timezone": "America/New_York",
        # The form sends 23:59 local on the chosen end day, in UTC.
        "recurrence_rule": "FREQ=DAILY;UNTIL=20261009T035900Z",
    }

    occurrences = expand_revision(revision, [], date(2026, 10, 1), date(2026, 11, 1))

    assert [item.recurrence_id.day for item in occurrences] == [6, 7, 8]
