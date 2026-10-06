from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from calendar_api.schemas import EventRevisionInput


def _base_payload() -> dict:
    return {
        "title": "Neighborhood meeting",
        "is_all_day": True,
        "start_date": "2026-10-10",
        "end_date": "2026-10-11",
        "timezone": "America/New_York",
        "group_ids": ["4ac64dab-12f5-462a-a00b-01e59df9158f"],
        "submitter": {
            "name": "Casey",
            "channel": "email",
            "contact": "CASEY@example.org",
        },
    }


def test_timing_shape_rejects_mixed_all_day_and_timed_fields() -> None:
    payload = _base_payload()
    payload["starts_at"] = "2026-10-10T14:00:00Z"
    with pytest.raises(ValidationError, match="all-day events cannot have"):
        EventRevisionInput.model_validate(payload)


def test_only_title_and_submitter_name_need_user_supplied_text() -> None:
    payload = _base_payload()
    payload["group_ids"] = []
    payload["submitter"] = {"name": "Casey", "channel": "email"}
    parsed = EventRevisionInput.model_validate(payload)
    assert parsed.group_ids == []
    assert parsed.submitter.contact == ""


def test_rrule_is_normalized_and_cannot_smuggle_dtstart() -> None:
    payload = _base_payload()
    payload["recurrence_rule"] = "rrule:freq=weekly;byday=sa"
    parsed = EventRevisionInput.model_validate(payload)
    assert parsed.recurrence_rule == "FREQ=WEEKLY;BYDAY=SA"

    payload["recurrence_rule"] = "DTSTART:20261010\nRRULE:FREQ=DAILY"
    with pytest.raises(ValidationError, match="without DTSTART"):
        EventRevisionInput.model_validate(payload)


def test_end_date_is_exclusive_and_must_follow_start() -> None:
    payload = _base_payload()
    payload["end_date"] = date(2026, 10, 10)
    with pytest.raises(ValidationError, match="exclusive"):
        EventRevisionInput.model_validate(payload)


def test_implausibly_long_durations_are_rejected() -> None:
    all_day = _base_payload()
    all_day["end_date"] = "2027-10-12"
    with pytest.raises(ValidationError, match="cannot exceed 366 days"):
        EventRevisionInput.model_validate(all_day)

    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 10, 10, 9, tzinfo=zone)
    timed = _base_payload()
    timed.update(
        {
            "is_all_day": False,
            "start_date": None,
            "end_date": None,
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(days=8)).isoformat(),
        }
    )
    with pytest.raises(ValidationError, match="cannot exceed 7 days"):
        EventRevisionInput.model_validate(timed)


def test_until_matches_dtstart_value_type() -> None:
    all_day = _base_payload()
    all_day["recurrence_rule"] = "FREQ=DAILY;UNTIL=20261012"
    assert EventRevisionInput.model_validate(all_day).recurrence_rule

    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 10, 10, 9, tzinfo=zone)
    timed = _base_payload()
    timed.update(
        {
            "is_all_day": False,
            "start_date": None,
            "end_date": None,
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
            "recurrence_rule": "FREQ=DAILY;UNTIL=20261012T140000Z",
        }
    )
    assert EventRevisionInput.model_validate(timed).recurrence_rule

    timed["recurrence_rule"] = "FREQ=DAILY;UNTIL=20261012T100000"
    with pytest.raises(ValidationError, match="UNTIL values must be specified in UTC"):
        EventRevisionInput.model_validate(timed)


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=DAILY;INTERVAL=3;COUNT=4",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,SA;UNTIL=20261231",
        "FREQ=MONTHLY;BYMONTHDAY=10",
        "FREQ=MONTHLY;BYDAY=2SA;COUNT=6",
        "FREQ=YEARLY;BYMONTH=10;BYMONTHDAY=10",
        "FREQ=YEARLY;BYMONTH=10;BYDAY=2SA",
    ],
)
def test_form_patterns_starting_on_their_first_date_are_accepted(rule: str) -> None:
    # 2026-10-10 is the second Saturday of October.
    payload = _base_payload()
    payload["recurrence_rule"] = rule
    assert EventRevisionInput.model_validate(payload).recurrence_rule == rule


def test_last_weekday_and_last_day_patterns_are_accepted() -> None:
    payload = _base_payload()
    payload.update({"start_date": "2026-10-31", "end_date": "2026-11-01"})
    for rule in ("FREQ=MONTHLY;BYDAY=-1SA", "FREQ=MONTHLY;BYMONTHDAY=-1"):
        payload["recurrence_rule"] = rule
        assert EventRevisionInput.model_validate(payload).recurrence_rule == rule


def test_start_must_be_the_first_date_of_the_schedule() -> None:
    payload = _base_payload()  # Saturday
    payload["recurrence_rule"] = "FREQ=WEEKLY;BYDAY=TU,TH;COUNT=4"
    with pytest.raises(ValidationError, match="first date of its repeating"):
        EventRevisionInput.model_validate(payload)

    payload["recurrence_rule"] = "FREQ=MONTHLY;BYDAY=1SA"
    with pytest.raises(ValidationError, match="first date of its repeating"):
        EventRevisionInput.model_validate(payload)


def test_series_cannot_end_before_it_starts() -> None:
    payload = _base_payload()
    payload["recurrence_rule"] = "FREQ=DAILY;UNTIL=20261009"
    with pytest.raises(ValidationError, match="ends before the event starts"):
        EventRevisionInput.model_validate(payload)

    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 10, 10, 18, tzinfo=zone)
    timed = _base_payload()
    timed.update(
        {
            "is_all_day": False,
            "start_date": None,
            "end_date": None,
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
            # 23:59 local on the start day is 03:59 UTC the next day.
            "recurrence_rule": "FREQ=DAILY;UNTIL=20261011T035900Z",
        }
    )
    assert EventRevisionInput.model_validate(timed).recurrence_rule
    timed["recurrence_rule"] = "FREQ=DAILY;UNTIL=20261010T120000Z"
    with pytest.raises(ValidationError, match="ends before the event starts"):
        EventRevisionInput.model_validate(timed)
