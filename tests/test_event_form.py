from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi.testclient import TestClient

from calendar_api.app import create_app
from calendar_api.config import Settings
from calendar_api.database import create_database


def _client(tmp_path: Path) -> TestClient:
    url = f"sqlite:///{tmp_path / 'event-form.db'}"
    return TestClient(
        create_app(
            Settings(database_url=url, admin_api_key="test-admin-key"),
            create_database(url),
        )
    )


def _group(client: TestClient) -> dict:
    return client.post(
        "/api/v1/admin/groups",
        headers={"X-Admin-Key": "test-admin-key"},
        json={"slug": "neighbors", "name": "Neighbors"},
    ).json()


def _payload(group_id: str, title: str = "Block party") -> dict:
    first_day = datetime.now(UTC).date() + timedelta(days=5)
    return {
        "title": title,
        "description": "Meet your neighbors.",
        "location_name": "Community garden",
        "location_address": "10 Main Street",
        "event_url": "https://example.org/block-party",
        "is_all_day": True,
        "start_date": first_day.isoformat(),
        "end_date": (first_day + timedelta(days=1)).isoformat(),
        "timezone": "America/New_York",
        "recurrence_rule": None,
        "recurrence_dates": [],
        "group_ids": [group_id],
        "submitter": {
            "name": "Taylor",
            "channel": "email",
            "contact": "taylor@example.org",
        },
    }


def test_creator_token_controls_edit_access(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client)
        created = client.post("/api/v1/events", json=_payload(group["id"]))
        assert created.status_code == 202, created.text
        identity = created.json()
        assert identity["management_token"]

        admin_headers = {"X-Admin-Key": "test-admin-key"}
        manage_url = f"/api/v1/events/{identity['event_id']}/manage"
        assert client.get(manage_url).status_code == 401
        assert client.get(
            manage_url, headers={"X-Event-Management-Token": "wrong"}
        ).status_code == 401
        creator_headers = {
            "X-Event-Management-Token": identity["management_token"]
        }
        detail = client.get(manage_url, headers=creator_headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["title"] == "Block party"

        pending_edit = client.post(
            f"/api/v1/events/{identity['event_id']}/revisions",
            headers=creator_headers,
            json=_payload(group["id"], "Block party — corrected"),
        )
        assert pending_edit.status_code == 202, pending_edit.text
        assert pending_edit.json()["revision_id"] == identity["revision_id"]
        assert pending_edit.json()["updated_pending_revision"] is True

        # Review the corrected pending content, then verify the creator can
        # submit another edit as a new pending revision.
        approved = client.post(
            f"/api/v1/admin/events/{identity['event_id']}"
            f"/revisions/{identity['revision_id']}/approve",
            headers=admin_headers,
            json={"actor": "moderator"},
        )
        assert approved.status_code == 200, approved.text

        edited = client.post(
            f"/api/v1/events/{identity['event_id']}/revisions",
            headers=creator_headers,
            json=_payload(group["id"], "Block party — new details"),
        )
        assert edited.status_code == 202, edited.text
        assert edited.json()["approval_status"] == "pending"


def test_admin_form_route_creates_and_approves(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client)
        created = client.post(
            "/api/v1/admin/events",
            headers={"X-Admin-Key": "test-admin-key"},
            json=_payload(group["id"]),
        )
        assert created.status_code == 201, created.text
        assert created.json()["approval_status"] == "approved"
        assert created.json()["occurrence_count"] == 1

        detail = client.get(f"/api/v1/events/{created.json()['event_id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["title"] == "Block party"

        edited = client.post(
            f"/api/v1/admin/events/{created.json()['event_id']}/revisions",
            headers={"X-Admin-Key": "test-admin-key"},
            json=_payload(group["id"], "Block party — updated by admin"),
        )
        assert edited.status_code == 201, edited.text
        assert edited.json()["approval_status"] == "approved"
        public = client.get(f"/api/v1/events/{created.json()['event_id']}")
        assert public.json()["title"] == "Block party — updated by admin"


def test_calendar_shell_contains_complete_event_form(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        shell = client.get("/").text
        javascript = client.get("/static/app.js").text

    for marker in (
        'id="create-event-button"',
        'id="event-form-dialog"',
        'id="event-title"',
        'id="event-description"',
        'id="event-starts-at"',
        'id="event-start-date"',
        'id="event-recurrence-frequency"',
        'id="event-recurrence-interval"',
        'id="recurrence-weekdays"',
        'id="recurrence-positions"',
        'name="recurrence-end"',
        'id="event-recurrence-until"',
        'id="event-recurrence-count"',
        'id="recurrence-summary"',
        'id="event-groups"',
        'id="submitter-contact"',
        'id="admin-login-form"',
    ):
        assert marker in shell
    for symbol in (
        "buildEventPayload",
        "openEditEventForm",
        "zonedLocalToIso",
        "X-Event-Management-Token",
        "managementLink",
        "function recurrenceRuleFromSettings",
        "function recurrenceSettingsFromRule",
        "function recurrencePositions",
        "function appendRepeatIcon",
    ):
        assert symbol in javascript

    form = shell.split('<form id="event-form"', 1)[1].split("</form>", 1)[0]
    assert form.count(" required") == 2
    assert '<select id="event-timezone"' in form
    assert '<select id="event-recurrence-frequency"' in form
    assert '<span class="field-heading">Title <span aria-hidden="true">*</span>' in form
    assert '<span class="field-heading">Name <span aria-hidden="true">*</span>' in form


def test_optional_contact_and_groups_still_publish_unfiltered(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client)
        payload = _payload(group["id"], "Ungrouped event")
        payload["group_ids"] = []
        payload["submitter"].pop("contact")
        created = client.post(
            "/api/v1/admin/events",
            headers={"X-Admin-Key": "test-admin-key"},
            json=payload,
        )
        assert created.status_code == 201, created.text

        day = datetime.now(UTC).date() + timedelta(days=5)
        calendar = client.get(
            f"/api/v1/calendar?start={day.isoformat()}"
            f"&end={(day + timedelta(days=1)).isoformat()}&timezone=UTC"
        )
        assert calendar.status_code == 200, calendar.text
        assert [item["title"] for item in calendar.json()["items"]] == [
            "Ungrouped event"
        ]
        assert calendar.json()["items"][0]["groups"] == []


def test_weekly_series_built_by_the_form_appears_on_the_calendar(tmp_path: Path) -> None:
    # The form sends explicit weekdays and, for "Ends on", 23:59 local in UTC.
    zone = ZoneInfo("America/New_York")
    today = datetime.now(zone).date()
    first = today + timedelta(days=(1 - today.weekday()) % 7 + 7)  # a Tuesday
    last = first + timedelta(days=16)  # the Thursday two weeks later
    until = datetime.combine(last, datetime.min.time().replace(hour=23, minute=59), zone)
    starts_at = datetime.combine(first, datetime.min.time().replace(hour=18), zone)

    with _client(tmp_path) as client:
        group = _group(client)
        payload = _payload(group["id"], "Run club")
        payload.update(
            {
                "is_all_day": False,
                "start_date": None,
                "end_date": None,
                "starts_at": starts_at.isoformat(),
                "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
                "recurrence_rule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;UNTIL="
                + until.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
            }
        )
        created = client.post(
            "/api/v1/admin/events",
            headers={"X-Admin-Key": "test-admin-key"},
            json=payload,
        )
        assert created.status_code == 201, created.text
        assert created.json()["occurrence_count"] == 4

        calendar = client.get(
            f"/api/v1/calendar?start={first.isoformat()}"
            f"&end={(first + timedelta(days=28)).isoformat()}"
            "&timezone=America/New_York"
        )
        assert calendar.status_code == 200, calendar.text
        items = calendar.json()["items"]
        days = [
            datetime.fromisoformat(item["starts_at"]).astimezone(zone).date()
            for item in items
        ]
        assert days == [
            first,
            first + timedelta(days=2),
            first + timedelta(days=14),
            first + timedelta(days=16),
        ]
        assert all(item["recurrence_rule"].startswith("FREQ=WEEKLY") for item in items)
        assert all(
            datetime.fromisoformat(item["starts_at"]).astimezone(zone).hour == 18
            for item in items
        )


def test_series_starting_off_its_pattern_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        group = _group(client)
        payload = _payload(group["id"])
        start = date.fromisoformat(payload["start_date"])
        other_day = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][(start.weekday() + 1) % 7]
        payload["recurrence_rule"] = f"FREQ=WEEKLY;BYDAY={other_day};COUNT=3"
        response = client.post("/api/v1/events", json=payload)
        assert response.status_code == 422
        assert "first date of its repeating schedule" in response.text
