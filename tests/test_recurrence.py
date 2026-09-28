from datetime import UTC, date, datetime

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
