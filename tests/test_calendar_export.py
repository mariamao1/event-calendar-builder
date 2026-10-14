from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from dateutil.rrule import rrulestr
from fastapi.testclient import TestClient

from calendar_api import ical
from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import apply_migrations, create_database


@pytest.fixture(scope="module")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    import os

    return os.environ.get("TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path_factory.mktemp('calendar-export') / 'test.db'}"
    )


@pytest.fixture(scope="module")
def client(database_url: str) -> TestClient:
    root = Path(__file__).resolve().parents[1]
    if not database_url.startswith("sqlite:///"):
        apply_migrations(database_url, root / "db" / "migrations")
    database = create_database(database_url, min_size=1, max_size=3)
    settings = Settings(database_url=database_url, admin_api_key="test-admin-key")
    app = create_app(settings, database)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def token_client(database_url: str) -> TestClient:
    database = create_database(database_url, min_size=1, max_size=3)
    settings = Settings(
        database_url=database_url,
        admin_api_key="test-admin-key",
        calendar_access_token="link-token-for-export-tests",
    )
    app = create_app(settings, database)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database(client: TestClient) -> None:
    database = client.app.state.database
    with database.transaction() as connection:
        if database.dialect == "postgresql":
            connection.execute("TRUNCATE groups, events RESTART IDENTITY CASCADE")
        else:
            connection.executescript(
                """
                UPDATE events SET current_revision_id = NULL, published_revision_id = NULL;
                DELETE FROM event_review_actions;
                DELETE FROM event_occurrence_materializations;
                DELETE FROM event_occurrences;
                DELETE FROM event_revision_recurrence_dates;
                DELETE FROM event_revision_groups;
                DELETE FROM event_revisions;
                DELETE FROM events;
                DELETE FROM groups;
                """
            )


def _headers() -> dict[str, str]:
    return {"X-Admin-Key": "test-admin-key"}


def _timed_payload(**overrides) -> dict:
    zone = ZoneInfo("America/New_York")
    starts_at = (datetime.now(zone) + timedelta(days=7)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    payload = {
        "title": "Drawing club",
        "description": "Bring a sketchbook, pencils; all levels.",
        "location_name": "Library",
        "location_address": "42 Main St",
        "event_url": "https://example.org/drawing",
        "is_all_day": False,
        "starts_at": starts_at.isoformat(),
        "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
        "timezone": "America/New_York",
        "submitter": {"name": "Alex", "channel": "email", "contact": "a@example.org"},
    }
    payload.update(overrides)
    return payload


def _create_approved(client: TestClient, payload: dict) -> dict:
    created = client.post("/api/v1/admin/events", headers=_headers(), json=payload)
    assert created.status_code == 201, created.text
    return created.json()


def _range_params() -> str:
    today = datetime.now(UTC).date()
    return (
        f"start={today.isoformat()}&end={(today + timedelta(days=20)).isoformat()}"
        "&timezone=America/New_York"
    )


def test_single_timed_event_exports_valid_ics(client: TestClient) -> None:
    identity = _create_approved(client, _timed_payload())
    response = client.get(f"/api/v1/events/{identity['event_id']}/ics")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/calendar")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith(".ics\"")
    body = response.text
    assert "BEGIN:VCALENDAR" in body
    assert "END:VCALENDAR" in body
    assert "VERSION:2.0" in body
    assert "SUMMARY:Drawing club" in body
    # Commas and semicolons are escaped per RFC 5545.
    assert "DESCRIPTION:Bring a sketchbook\\, pencils\\; all levels." in body
    assert "LOCATION:Library · 42 Main St" in body
    assert "URL:https://example.org/drawing" in body
    # Timed stamps are UTC with a Z suffix.
    assert re.search(r"DTSTART:\d{8}T\d{6}Z", body)
    assert re.search(r"DTEND:\d{8}T\d{6}Z", body)
    assert re.search(r"DTSTAMP:\d{8}T\d{6}Z", body)
    assert f"UID:{identity['event_id']}@event-calendar" in body


def test_all_day_event_uses_date_values(client: TestClient) -> None:
    today = datetime.now(UTC).date()
    payload = {
        "title": "Founders day",
        "description": "",
        "is_all_day": True,
        "start_date": (today + timedelta(days=5)).isoformat(),
        "end_date": (today + timedelta(days=7)).isoformat(),
        "timezone": "America/New_York",
        "submitter": {"name": "Alex", "channel": "email", "contact": ""},
    }
    identity = _create_approved(client, payload)
    body = client.get(f"/api/v1/events/{identity['event_id']}/ics").text
    assert (
        f"DTSTART;VALUE=DATE:{payload['start_date'].replace('-', '')}" in body
    )
    assert f"DTEND;VALUE=DATE:{payload['end_date'].replace('-', '')}" in body


def test_recurring_event_series_carries_rrule_but_occurrence_does_not(
    client: TestClient,
) -> None:
    identity = _create_approved(
        client, _timed_payload(recurrence_rule="FREQ=DAILY;COUNT=3")
    )
    series_body = client.get(f"/api/v1/events/{identity['event_id']}/ics").text
    assert "RRULE:FREQ=DAILY;COUNT=3" in series_body

    occurrences = client.get(f"/api/v1/calendar?{_range_params()}").json()["items"]
    assert len(occurrences) == 3
    first = occurrences[0]
    date_body = client.get(
        f"/api/v1/events/{identity['event_id']}/ics",
        params={"occurrence": first["occurrence_id"]},
    ).text
    assert "RRULE" not in date_body
    assert f"UID:{first['occurrence_id']}@event-calendar" in date_body

    missing = client.get(
        f"/api/v1/events/{identity['event_id']}/ics",
        params={"occurrence": "00000000-0000-0000-0000-000000000000"},
    )
    assert missing.status_code == 404


def test_cancelled_event_exports_cancelled_status(client: TestClient) -> None:
    identity = _create_approved(client, _timed_payload())
    cancelled = client.post(
        f"/api/v1/events/{identity['event_id']}/cancel",
        headers=_headers(),
        json={"actor": "Alex"},
    )
    assert cancelled.status_code == 200, cancelled.text
    body = client.get(f"/api/v1/events/{identity['event_id']}/ics").text
    assert "STATUS:CANCELLED" in body


def test_calendar_feed_matches_calendar_read(client: TestClient) -> None:
    _create_approved(client, _timed_payload(title="First"))
    _create_approved(client, _timed_payload(title="Second"))
    feed = client.get(f"/api/v1/calendar.ics?{_range_params()}")
    assert feed.status_code == 200, feed.text
    assert feed.headers["content-type"].startswith("text/calendar")
    items = client.get(f"/api/v1/calendar?{_range_params()}").json()["items"]
    assert feed.text.count("BEGIN:VEVENT") == len(items) == 2
    assert "SUMMARY:First" in feed.text
    assert "SUMMARY:Second" in feed.text


def test_exports_require_link_token(token_client: TestClient) -> None:
    identity = _create_approved(token_client, _timed_payload())
    event_id = identity["event_id"]
    assert token_client.get(f"/api/v1/events/{event_id}/ics").status_code == 401
    assert token_client.get(f"/api/v1/calendar.ics?{_range_params()}").status_code == 401
    token = "link-token-for-export-tests"
    assert (
        token_client.get(
            f"/api/v1/events/{event_id}/ics", params={"token": token}
        ).status_code
        == 200
    )
    today = datetime.now(UTC).date()
    feed_params = {
        "token": token,
        "start": today.isoformat(),
        "end": (today + timedelta(days=20)).isoformat(),
        "timezone": "America/New_York",
    }
    assert token_client.get("/api/v1/calendar.ics", params=feed_params).status_code == 200


def test_slug_filename_is_ascii_safe() -> None:
    name = ical.slug_filename("Café night, vol. 2; special", "event-id")
    assert name.endswith(".ics")
    name.encode("ascii")
    assert "caf" in name


def test_ical_escaping_and_folding() -> None:
    assert ical.escape_text("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"
    long_summary = "x" * 200
    folded = ical.fold_line(f"SUMMARY:{long_summary}")
    assert "\r\n " in folded
    for line in folded.split("\r\n"):
        assert len(line.encode("utf-8")) <= 76  # 75 + continuation space


def test_google_calendar_url_shapes() -> None:
    timed = ical.google_calendar_url(
        title="Drawing club",
        starts_at="2026-10-07T18:00:00-04:00",
        ends_at="2026-10-07T19:00:00-04:00",
        description="Bring a sketchbook.",
        location="Library",
    )
    assert timed.startswith("https://calendar.google.com/calendar/render?")
    assert "dates=20261007T220000Z%2F20261007T230000Z" in timed
    assert "text=Drawing+club" in timed

    all_day = ical.google_calendar_url(
        title="Founders day",
        is_all_day=True,
        start_date="2026-10-07",
        end_date="2026-10-09",
    )
    assert "dates=20261007%2F20261009" in all_day


NEW_YORK = ZoneInfo("America/New_York")


def _unfold(body: str) -> list[str]:
    return body.replace("\r\n ", "").split("\r\n")


def _vevents(body: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    inside = False
    for line in _unfold(body):
        if line == "BEGIN:VEVENT":
            blocks.append([])
            inside = True
        elif line == "END:VEVENT":
            inside = False
        elif inside:
            blocks[-1].append(line)
    return blocks


def _value(block: list[str], name: str) -> str | None:
    for line in block:
        key, _, value = line.partition(":")
        if key.split(";")[0] == name:
            return value
    return None


def _weekly_evening_series(client: TestClient, count: int) -> tuple[str, list[dict]]:
    """A weekly 9 PM New York series long enough to cross a DST change."""
    today = datetime.now(NEW_YORK).date()
    starts_at = datetime.combine(today + timedelta(days=3), time(21), NEW_YORK)
    identity = _create_approved(
        client,
        _timed_payload(
            title="Evening choir",
            starts_at=starts_at.isoformat(),
            ends_at=(starts_at + timedelta(hours=1)).isoformat(),
            recurrence_rule=f"FREQ=WEEKLY;COUNT={count}",
        ),
    )
    event_id = identity["event_id"]
    start = today.isoformat()
    end = (today + timedelta(days=7 * count + 7)).isoformat()
    items = client.get(
        f"/api/v1/calendar?start={start}&end={end}&timezone=America/New_York&limit=2000"
    ).json()["items"]
    return event_id, [item for item in items if item["event_id"] == event_id]


def _slot(item: dict) -> str:
    return datetime.fromisoformat(item["starts_at"]).astimezone(NEW_YORK).strftime(
        "%Y%m%dT%H%M%S"
    )


def test_series_keeps_local_time_across_daylight_saving(client: TestClient) -> None:
    # 40 weeks always spans a New York clock change.
    event_id, items = _weekly_evening_series(client, 40)
    assert len(items) == 40
    offsets = {datetime.fromisoformat(item["starts_at"]).astimezone(NEW_YORK).utcoffset() for item in items}
    assert len(offsets) == 2

    body = client.get(f"/api/v1/events/{event_id}/ics").text
    assert "BEGIN:VTIMEZONE" in body and "TZID:America/New_York" in body
    assert "BEGIN:DAYLIGHT" in body and "BEGIN:STANDARD" in body
    (master,) = _vevents(body)
    assert f"DTSTART;TZID=America/New_York:{_slot(items[0])}" in master
    assert _value(master, "RRULE") == "FREQ=WEEKLY;COUNT=40"

    # Expanding what a calendar app reads reproduces the app's own dates,
    # every one at 9 PM New York time.
    recurrence = "\n".join(
        line for line in master if line.startswith(("DTSTART", "RRULE", "EXDATE", "RDATE"))
    )
    expanded = list(rrulestr(recurrence, forceset=True, tzids={"America/New_York": NEW_YORK}))
    assert [moment.astimezone(UTC) for moment in expanded] == [
        datetime.fromisoformat(item["starts_at"]).astimezone(UTC) for item in items
    ]


def test_series_export_carries_per_date_exceptions(client: TestClient) -> None:
    event_id, items = _weekly_evening_series(client, 5)
    skipped, cancelled, edited = items[1], items[2], items[3]
    assert client.delete(
        f"/api/v1/events/{event_id}/occurrences/{skipped['occurrence_id']}",
        headers=_headers(),
    ).status_code == 200
    assert client.post(
        f"/api/v1/events/{event_id}/occurrences/{cancelled['occurrence_id']}/cancel",
        headers=_headers(),
    ).status_code == 200
    moved_start = datetime.fromisoformat(edited["starts_at"]) + timedelta(hours=1)
    response = client.post(
        f"/api/v1/events/{event_id}/occurrences/{edited['occurrence_id']}/edit",
        headers=_headers(),
        json=_timed_payload(
            title="Evening choir (dress rehearsal)",
            starts_at=moved_start.isoformat(),
            ends_at=(moved_start + timedelta(hours=1)).isoformat(),
            recurrence_rule=None,
        ),
    )
    assert response.status_code == 200, response.text

    body = client.get(f"/api/v1/events/{event_id}/ics").text
    master, *overrides = _vevents(body)
    assert f"EXDATE;TZID=America/New_York:{_slot(skipped)}" in master
    assert "STATUS:CANCELLED" not in master
    by_slot = {_value(block, "RECURRENCE-ID"): block for block in overrides}
    assert set(by_slot) == {_slot(cancelled), _slot(edited)}
    assert all(_value(block, "UID") == f"{event_id}@event-calendar" for block in overrides)
    assert "STATUS:CANCELLED" in by_slot[_slot(cancelled)]
    rehearsal = by_slot[_slot(edited)]
    assert _value(rehearsal, "SUMMARY") == "Evening choir (dress rehearsal)"
    assert _value(rehearsal, "DTSTART") == ical.format_dt_utc(moved_start)

    # The edited date exported alone carries its own content and time.
    single = client.get(
        f"/api/v1/events/{event_id}/ics", params={"occurrence": edited["occurrence_id"]}
    ).text
    (block,) = _vevents(single)
    assert _value(block, "SUMMARY") == "Evening choir (dress rehearsal)"
    assert _value(block, "DTSTART") == ical.format_dt_utc(moved_start)


def test_google_calendar_redirects(client: TestClient) -> None:
    event_id, items = _weekly_evening_series(client, 3)
    series = client.get(f"/api/v1/events/{event_id}/google", follow_redirects=False)
    assert series.status_code == 303
    location = series.headers["location"]
    assert location.startswith("https://calendar.google.com/calendar/render?")
    assert "recur=RRULE%3AFREQ%3DWEEKLY%3BCOUNT%3D3" in location
    assert "ctz=America%2FNew_York" in location
    assert f"dates={_slot(items[0])}%2F" in location

    one_date = client.get(
        f"/api/v1/events/{event_id}/google",
        params={"occurrence": items[1]["occurrence_id"]},
        follow_redirects=False,
    )
    assert one_date.status_code == 303
    assert "recur=" not in one_date.headers["location"]
    expected = ical.format_dt_utc(datetime.fromisoformat(items[1]["starts_at"]))
    assert f"dates={expected}%2F" in one_date.headers["location"]


def test_subscription_feed_defaults_and_group_filter(client: TestClient) -> None:
    group = client.post(
        "/api/v1/admin/groups",
        headers=_headers(),
        json={"slug": "arts", "name": "Arts & Crafts"},
    )
    assert group.status_code == 201, group.text
    _create_approved(client, _timed_payload(title="Tagged", group_ids=[group.json()["id"]]))
    _create_approved(client, _timed_payload(title="Untagged"))

    feed = client.get("/api/v1/calendar.ics")
    assert feed.status_code == 200, feed.text
    assert "X-WR-CALNAME:Community Calendar" in feed.text
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT60M" in feed.text
    assert feed.text.count("BEGIN:VEVENT") == 2

    filtered = client.get("/api/v1/calendar.ics", params={"group": "arts"}).text
    assert "X-WR-CALNAME:Community Calendar · Arts & Crafts" in filtered
    assert "SUMMARY:Tagged" in filtered and "SUMMARY:Untagged" not in filtered
    assert "CATEGORIES:Arts & Crafts" in filtered

    today = datetime.now(UTC).date().isoformat()
    assert client.get("/api/v1/calendar.ics", params={"start": today}).status_code == 422


def test_vtimezone_observances() -> None:
    tokyo = ical.vtimezone(
        "Asia/Tokyo", datetime(2026, 1, 1, tzinfo=UTC), datetime(2028, 1, 1, tzinfo=UTC)
    )
    assert tokyo.count("BEGIN:STANDARD") == 1 and "BEGIN:DAYLIGHT" not in tokyo
    assert "TZOFFSETTO:+0900" in tokyo

    new_york = _unfold(
        ical.vtimezone(
            "America/New_York",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
        )
    )
    # 2026: clocks spring forward at 2 AM on March 8 and fall back at 2 AM
    # on November 1.
    spring = new_york.index("DTSTART:20260308T020000")
    assert new_york[spring - 1] == "BEGIN:DAYLIGHT"
    assert new_york[spring + 1 : spring + 3] == ["TZOFFSETFROM:-0500", "TZOFFSETTO:-0400"]
    fall = new_york.index("DTSTART:20261101T020000")
    assert new_york[fall - 1] == "BEGIN:STANDARD"
    assert new_york[fall + 1 : fall + 3] == ["TZOFFSETFROM:-0400", "TZOFFSETTO:-0500"]


def test_frontend_offers_calendar_export(client: TestClient) -> None:
    shell = client.get("/").text
    for element in (
        'id="event-dialog-export"',
        'id="event-export-ics"',
        'id="event-export-google"',
        'name="event-export-scope"',
        'id="subscribe-button"',
        'id="subscribe-dialog"',
        'id="subscribe-webcal"',
    ):
        assert element in shell
    javascript = client.get("/static/app.js").text
    for symbol in (
        "function eventExportUrl(kind, eventId, occurrenceId)",
        "function renderEventExport(view)",
        "function calendarFeedUrl()",
        'url.searchParams.append("group", slug)',
        'feed.replace(/^https?:/, "webcal:")',
        "https://calendar.google.com/calendar/render?cid=",
    ):
        assert symbol in javascript
