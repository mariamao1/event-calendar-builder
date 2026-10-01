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
    payload["recurrence_rule"] = "rrule:freq=weekly;byday=mo"
    parsed = EventRevisionInput.model_validate(payload)
    assert parsed.recurrence_rule == "FREQ=WEEKLY;BYDAY=MO"

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
