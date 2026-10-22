from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database

ADMIN = {"X-Admin-Key": "test-admin-key"}
NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'timezones.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _evening_series(client: TestClient) -> tuple[dict, datetime]:
    """A weekly 9 PM New York series: Wednesday morning in Tokyo."""
    today = datetime.now(NEW_YORK).date()
    first = today + timedelta(days=(1 - today.weekday()) % 7 + 7)  # a Tuesday
    starts_at = datetime.combine(first, time(21), NEW_YORK)
    created = client.post(
        "/api/v1/admin/events",
        headers=ADMIN,
        json={
            "title": "Evening choir",
            "description": "",
            "is_all_day": False,
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
            "timezone": "America/New_York",
            "recurrence_rule": "FREQ=WEEKLY;COUNT=3",
            "recurrence_dates": [],
            "group_ids": [],
            "submitter": {"name": "Robin", "channel": "email"},
        },
    )
    assert created.status_code == 201, created.text
    return created.json(), starts_at


def _calendar(client: TestClient, day, timezone: str) -> list[dict]:
    response = client.get(
        f"/api/v1/calendar?start={day.isoformat()}"
        f"&end={(day + timedelta(days=1)).isoformat()}&timezone={timezone}"
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_each_viewer_sees_the_event_on_their_own_local_day(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created, starts_at = _evening_series(client)
        new_york_day = starts_at.date()
        tokyo_day = starts_at.astimezone(TOKYO).date()
        assert tokyo_day == new_york_day + timedelta(days=1)

        assert [item["title"] for item in _calendar(client, new_york_day, "America/New_York")] == [
            "Evening choir"
        ]
        assert _calendar(client, new_york_day, "Asia/Tokyo") == []
        (tokyo_item,) = _calendar(client, tokyo_day, "Asia/Tokyo")

        # The instant is absolute; the event keeps the timezone it was
        # submitted in, whoever reads it.
        assert datetime.fromisoformat(tokyo_item["starts_at"]) == starts_at
        assert tokyo_item["timezone"] == "America/New_York"
        detail = client.get(f"/api/v1/events/{created['event_id']}").json()
        assert detail["timezone"] == "America/New_York"
        assert datetime.fromisoformat(detail["starts_at"]).astimezone(NEW_YORK) == starts_at
        assert all(
            datetime.fromisoformat(item["starts_at"]).astimezone(NEW_YORK).time() == time(21)
            for item in detail["series"]["upcoming"]
        )


def _javascript(tmp_path: Path) -> str:
    with _client(tmp_path) as client:
        response = client.get("/static/app.js")
    assert response.status_code == 200
    return response.text


def test_frontend_tells_event_time_in_the_submitted_timezone(tmp_path: Path) -> None:
    javascript = _javascript(tmp_path)
    for symbol in (
        "const viewerTimezone",
        "function todayDate",
        "function eventTimezoneRows",
        'metaRow("Event time"',
        "function seriesStartKey",
        "function formatLocalDates(items, event)",
        "function showCalendarDate",
        'timeZoneName: "short"',
    ):
        assert symbol in javascript
    # "Today" is read from the clock in the viewer's timezone when needed,
    # never frozen at page load.
    assert "const now = new Date()" not in javascript
    # The series summary counts weekdays on the event's own calendar.
    assert "describeRecurrence(event.recurrence_rule, seriesStartKey(event)" in javascript

    with _client(tmp_path) as client:
        shell = client.get("/").text
    assert 'id="calendar-timezone"' in shell
